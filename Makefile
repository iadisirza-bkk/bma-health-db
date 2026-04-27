# ============================================================================
# BMA Health — One-Stop Backend Makefile
# ============================================================================
#
# Usage:
#   make start            — Start API + Cloudflare tunnel (background)
#   make stop             — Stop API + tunnel
#   make dev              — Start API with hot-reload (foreground)
#   make status           — Show all service status
#   make test             — Run test suite
#   make help             — Show all targets
#
# ============================================================================

.DEFAULT_GOAL := help
SHELL := /bin/bash

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_DIR       := api
API_PORT      := 9002
API_HOST      := 0.0.0.0
DOCKER_API_PORT := 8001
VENV_DIR      := $(API_DIR)/.venv
PYTHON        := python3
PIP           := $(VENV_DIR)/bin/pip
PYTEST        := $(VENV_DIR)/bin/pytest
UVICORN       := $(VENV_DIR)/bin/uvicorn
DB_URL        := postgresql://postgres:bma_health_dev@localhost:5433/bma_health

# Load .env if exists
-include .env
export

# ---------------------------------------------------------------------------
# Service Management (start/stop all)
# ---------------------------------------------------------------------------

API_LOG       := /tmp/bma-api.log
TUNNEL_LOG    := /tmp/cloudflared.log
API_PID       = $(shell lsof -ti :$(API_PORT) 2>/dev/null)
TUNNEL_PID    = $(shell pgrep -f "cloudflared tunnel run" 2>/dev/null)

.PHONY: start
start: ## Start everything: API + Cloudflare tunnel (background, persistent)
	@# --- API ---
	@if lsof -i :$(API_PORT) -P -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "  API already running on :$(API_PORT)"; \
	else \
		echo "  Starting API on :$(API_PORT)..."; \
		nohup bash -c 'cd $(API_DIR) && $(PYTHON) -m uvicorn main:app --host $(API_HOST) --port $(API_PORT)' > $(API_LOG) 2>&1 & \
		sleep 4; \
		if curl -s http://localhost:$(API_PORT)/health >/dev/null 2>&1; then \
			echo "  API: OK"; \
		else \
			echo "  API: FAILED — check $(API_LOG)"; \
		fi; \
	fi
	@# --- Cloudflare Tunnel ---
	@if pgrep -f "cloudflared tunnel run" >/dev/null 2>&1; then \
		echo "  Tunnel already running"; \
	else \
		if command -v cloudflared >/dev/null 2>&1; then \
			echo "  Starting Cloudflare tunnel..."; \
			nohup cloudflared tunnel run bma-health > $(TUNNEL_LOG) 2>&1 & \
			sleep 3; \
			echo "  Tunnel: started"; \
		else \
			echo "  Tunnel: cloudflared not installed — skipped"; \
		fi; \
	fi
	@echo ""
	@$(MAKE) --no-print-directory status

.PHONY: stop
stop: ## Stop API + Cloudflare tunnel
	@echo "Stopping services..."
	@if [ -n "$(API_PID)" ]; then \
		kill $(API_PID) 2>/dev/null; \
		echo "  API: stopped"; \
	else \
		echo "  API: not running"; \
	fi
	@if [ -n "$(TUNNEL_PID)" ]; then \
		kill $(TUNNEL_PID) 2>/dev/null; \
		echo "  Tunnel: stopped"; \
	else \
		echo "  Tunnel: not running"; \
	fi

.PHONY: restart-all
restart-all: stop start ## Restart API + tunnel

# ---------------------------------------------------------------------------
# Development
# ---------------------------------------------------------------------------

.PHONY: dev
dev: ## Start API server locally with hot-reload (port 9002)
	cd $(API_DIR) && $(PYTHON) -m uvicorn main:app \
		--host $(API_HOST) \
		--port $(API_PORT) \
		--reload

.PHONY: dev-venv
dev-venv: venv ## Start API using venv Python (port 9002)
	cd $(API_DIR) && ../$(UVICORN) main:app \
		--host $(API_HOST) \
		--port $(API_PORT) \
		--reload

# ---------------------------------------------------------------------------
# Docker Compose
# ---------------------------------------------------------------------------

.PHONY: up
up: ## Start all services (PostgreSQL + Redis + API) via Docker Compose
	docker compose up -d
	@echo ""
	@echo "  API:        http://localhost:$(DOCKER_API_PORT)"
	@echo "  Swagger:    http://localhost:$(DOCKER_API_PORT)/docs"
	@echo "  ReDoc:      http://localhost:$(DOCKER_API_PORT)/redoc"
	@echo "  PostgreSQL: localhost:5433"
	@echo "  Redis:      localhost:6379"
	@echo ""

.PHONY: down
down: ## Stop all Docker services
	docker compose down

.PHONY: infra
infra: ## Start only PostgreSQL + Redis (for local API dev)
	docker compose up -d postgres redis
	@echo ""
	@echo "  PostgreSQL: localhost:5433"
	@echo "  Redis:      localhost:6379"
	@echo ""
	@echo "  Run 'make dev' to start the API locally."

.PHONY: restart
restart: ## Restart all Docker services
	docker compose restart

.PHONY: logs
logs: ## Tail Docker Compose logs
	docker compose logs -f --tail=50

.PHONY: logs-api
logs-api: ## Tail API container logs only
	docker compose logs -f --tail=50 api

# ---------------------------------------------------------------------------
# Setup & Dependencies
# ---------------------------------------------------------------------------

.PHONY: install
install: ## Install Python dependencies (system-wide)
	cd $(API_DIR) && $(PYTHON) -m pip install -r requirements.txt

.PHONY: venv
venv: $(VENV_DIR)/bin/activate ## Create virtual environment and install deps

$(VENV_DIR)/bin/activate: $(API_DIR)/requirements.txt
	$(PYTHON) -m venv $(VENV_DIR)
	$(PIP) install --upgrade pip
	$(PIP) install -r $(API_DIR)/requirements.txt
	touch $(VENV_DIR)/bin/activate

.PHONY: env
env: ## Copy .env.example to .env (if not exists)
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "Created .env from .env.example"; \
	else \
		echo ".env already exists — skipping"; \
	fi

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

.PHONY: migrate
migrate: ## Run all SQL migrations (001-015) via Docker
	@for f in db/migrations/*.sql; do \
		echo "Applying $$f ..."; \
		docker exec -i bma-health-db psql -U postgres -d bma_health < "$$f"; \
	done
	@echo "Migrations complete (11 files)."

.PHONY: seed
seed: ## Run seed data via Docker
	@for f in db/seeds/*.sql; do \
		echo "Seeding $$f ..."; \
		docker exec -i bma-health-db psql -U postgres -d bma_health < "$$f"; \
	done
	@echo "Seeding complete."

.PHONY: refresh-views
refresh-views: ## Refresh all materialized views (auto-discovered from pg_matviews)
	@echo "Refreshing all materialized views (CONCURRENTLY)..."
	@docker exec bma-health-db psql -U postgres -d bma_health -c "\
		DO \$$\$$ DECLARE v RECORD; BEGIN \
			FOR v IN SELECT matviewname FROM pg_matviews WHERE schemaname='public' LOOP \
				EXECUTE format('REFRESH MATERIALIZED VIEW CONCURRENTLY %I', v.matviewname); \
				RAISE NOTICE 'Refreshed %', v.matviewname; \
			END LOOP; \
		END \$$\$$; \
	"
	@echo "All views refreshed."

# To schedule view refresh in production, install pg_cron and run:
#   SELECT cron.schedule('refresh-views', '15 * * * *',
#       'DO $$ DECLARE v RECORD; BEGIN FOR v IN SELECT matviewname FROM pg_matviews WHERE schemaname=''public'' LOOP EXECUTE format(''REFRESH MATERIALIZED VIEW CONCURRENTLY %I'', v.matviewname); END LOOP; END $$;'
#   );
# This refreshes every hour at :15. For lower-frequency data, use '0 3 * * *' (daily 3am).

.PHONY: db-stats
db-stats: ## Show row counts for all tables and views
	@docker exec bma-health-db psql -U postgres -d bma_health -c "\
		SELECT 'raw_patients' AS name, COUNT(*) FROM raw_patients \
		UNION ALL SELECT 'raw_vitalsigns', COUNT(*) FROM raw_vitalsigns \
		UNION ALL SELECT 'raw_visits', COUNT(*) FROM raw_visits \
		UNION ALL SELECT 'raw_homevisit', COUNT(*) FROM raw_homevisit \
		UNION ALL SELECT 'raw_homehealth', COUNT(*) FROM raw_homehealth \
		UNION ALL SELECT 'raw_lab_results', COUNT(*) FROM raw_lab_results \
		UNION ALL SELECT 'raw_lab_extended', COUNT(*) FROM raw_lab_extended \
		UNION ALL SELECT '---VIEWS---', 0 \
		UNION ALL SELECT 'summary_district_disease', COUNT(*) FROM summary_district_disease \
		UNION ALL SELECT 'summary_screening_tests', COUNT(*) FROM summary_screening_tests \
		UNION ALL SELECT 'summary_chronic_history', COUNT(*) FROM summary_chronic_history \
		UNION ALL SELECT 'summary_family_history', COUNT(*) FROM summary_family_history \
		UNION ALL SELECT '---COMPUTED---', 0 \
		UNION ALL SELECT 'patients_with_age', COUNT(*) FROM raw_patients WHERE age IS NOT NULL \
		UNION ALL SELECT 'vitalsigns_with_bmi', COUNT(*) FROM raw_vitalsigns WHERE bmi IS NOT NULL \
		ORDER BY 1; \
	"

.PHONY: db-reset
db-reset: ## Drop and recreate database (DESTRUCTIVE)
	@echo "WARNING: This will destroy all data in bma_health database."
	@read -p "Continue? [y/N] " confirm && [ "$$confirm" = "y" ] || exit 1
	docker exec bma-health-db psql -U postgres \
		-c "DROP DATABASE IF EXISTS bma_health;" \
		-c "CREATE DATABASE bma_health;"
	$(MAKE) migrate
	$(MAKE) seed
	@echo "Database reset complete."

# ---------------------------------------------------------------------------
# Testing & Quality
# ---------------------------------------------------------------------------

.PHONY: test
test: ## Run full test suite (225 tests)
	cd $(API_DIR) && $(PYTHON) -m pytest -v

.PHONY: test-cov
test-cov: ## Run tests with coverage report
	cd $(API_DIR) && $(PYTHON) -m pytest -v --cov=. --cov-report=term-missing

.PHONY: lint
lint: ## Run linter (ruff)
	cd $(API_DIR) && $(PYTHON) -m ruff check .

.PHONY: format
format: ## Auto-format code (ruff)
	cd $(API_DIR) && $(PYTHON) -m ruff format .

# ---------------------------------------------------------------------------
# ETL & Data Import
# ---------------------------------------------------------------------------

.PHONY: etl
etl: ## Run full ETL import from minimal_data/portal_top CSVs
	cd $(API_DIR) && $(PYTHON) ../etl/import_csv.py \
		--data-dir ../minimal_data/portal_top \
		--db-url "$(DB_URL)"

.PHONY: etl-backfill
etl-backfill: ## Backfill district_code + refresh views (no re-import)
	@echo "Backfilling district codes and refreshing views..."
	@cd $(API_DIR) && $(PYTHON) -c "\
	import sys; sys.path.insert(0,'.'); \
	import psycopg2, importlib.util; \
	spec = importlib.util.spec_from_file_location('etl','../etl/import_csv.py'); \
	etl = importlib.util.module_from_spec(spec); spec.loader.exec_module(etl); \
	conn = psycopg2.connect('$(DB_URL)'); conn.autocommit = False; \
	cur = conn.cursor(); \
	etl.refresh_all_summaries(cur); conn.commit(); conn.close(); \
	from services.data_adapter import invalidate_cache; invalidate_cache(); \
	print('Done.')"

# ---------------------------------------------------------------------------
# Chat / Agent
# ---------------------------------------------------------------------------

.PHONY: chat-test
chat-test: ## Test LLM chat with a sample question
	@echo "Testing chat endpoint..."
	@curl -s -H "X-API-Key: $${API_KEY:-dev-api-key}" \
		"http://localhost:$(API_PORT)/api/health/chat?message=%E0%B8%A0%E0%B8%B2%E0%B8%9E%E0%B8%A3%E0%B8%A7%E0%B8%A1%E0%B8%AA%E0%B8%B8%E0%B8%82%E0%B8%A0%E0%B8%B2%E0%B8%9E" \
		| python3 -m json.tool 2>/dev/null || echo "Chat unavailable (LMStudio not running?)"

.PHONY: agent-tools
agent-tools: ## List registered agent tools
	@cd $(API_DIR) && $(PYTHON) -c "\
	from agents.tools.registry import ToolRegistry; \
	reg = ToolRegistry.create_default(); \
	print(f'{len(reg.list_tools())} tools registered:'); \
	[print(f'  - {t.name}') for t in reg.list_tools()]"

# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

.PHONY: generate-reports
generate-reports: ## Trigger PDF report generation for all languages
	@echo "Triggering report generation..."
	@curl -s -X POST -H "X-API-Key: $${API_KEY:-dev-api-key}" \
		http://localhost:$(API_PORT)/api/reports/generate | python3 -m json.tool 2>/dev/null || \
	echo "API is not running or reports endpoint not available."

.PHONY: report-status
report-status: ## Check report generation progress
	@curl -s -H "X-API-Key: $${API_KEY:-dev-api-key}" \
		http://localhost:$(API_PORT)/api/reports/generation-progress | python3 -m json.tool 2>/dev/null || \
	echo "API is not running."

.PHONY: report-dashboard
report-dashboard: ## Show unified report dashboard (progress %, catalog, scheduler)
	@curl -s -H "X-API-Key: $${API_KEY:-dev-api-key}" \
		http://localhost:$(API_PORT)/api/reports/dashboard | python3 -m json.tool 2>/dev/null || \
	echo "API is not running."

.PHONY: report-catalog
report-catalog: ## List all available reports with cache status
	@curl -s -H "X-API-Key: $${API_KEY:-dev-api-key}" \
		http://localhost:$(API_PORT)/api/reports/catalog | python3 -m json.tool 2>/dev/null || \
	echo "API is not running."

# ---------------------------------------------------------------------------
# Health & Status
# ---------------------------------------------------------------------------

.PHONY: health
health: ## Check API health endpoint
	@curl -s http://localhost:$(API_PORT)/health | python3 -m json.tool 2>/dev/null || \
	curl -s http://localhost:$(DOCKER_API_PORT)/health | python3 -m json.tool 2>/dev/null || \
	echo "API is not running. Start with 'make dev' or 'make up'."

.PHONY: docs
docs: ## Open Swagger UI in browser (150 endpoints)
	@open http://localhost:$(API_PORT)/docs 2>/dev/null || \
	xdg-open http://localhost:$(API_PORT)/docs 2>/dev/null || \
	echo "Open http://localhost:$(API_PORT)/docs in your browser"

.PHONY: redoc
redoc: ## Open ReDoc in browser
	@open http://localhost:$(API_PORT)/redoc 2>/dev/null || \
	xdg-open http://localhost:$(API_PORT)/redoc 2>/dev/null || \
	echo "Open http://localhost:$(API_PORT)/redoc in your browser"

.PHONY: status
status: ## Show status of all services
	@echo "=== Docker Services ==="
	@docker compose ps 2>/dev/null || echo "  Docker Compose not running"
	@echo ""
	@echo "=== Port Status ==="
	@echo -n "  API ($(API_PORT)):       " && (lsof -i :$(API_PORT) -P -sTCP:LISTEN | grep -q LISTEN && echo "RUNNING") || echo "STOPPED"
	@echo -n "  Docker API ($(DOCKER_API_PORT)):  " && (lsof -i :$(DOCKER_API_PORT) -P -sTCP:LISTEN | grep -q LISTEN && echo "RUNNING") || echo "STOPPED"
	@echo -n "  PostgreSQL (5433): " && (lsof -i :5433 -P -sTCP:LISTEN | grep -q LISTEN && echo "RUNNING") || echo "STOPPED"
	@echo -n "  Redis (6379):      " && (lsof -i :6379 -P -sTCP:LISTEN | grep -q LISTEN && echo "RUNNING") || echo "STOPPED"
	@echo -n "  LMStudio (5555):   " && (lsof -i :5555 -P -sTCP:LISTEN | grep -q LISTEN && echo "RUNNING") || echo "STOPPED (chat will return 503)"
	@echo ""
	@echo "=== Endpoint Count ==="
	@curl -s http://localhost:$(API_PORT)/openapi.json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'  {len(d[\"paths\"])} endpoints registered')" 2>/dev/null || echo "  API not reachable"

.PHONY: endpoints
endpoints: ## List all endpoint groups with counts
	@curl -s http://localhost:$(API_PORT)/openapi.json 2>/dev/null | python3 -c "\
	import sys,json;\
	d=json.load(sys.stdin);\
	groups={};\
	[groups.setdefault('/'.join(p.split('/')[:3]),0) for p in d['paths']];\
	[groups.__setitem__('/'.join(p.split('/')[:3]), groups['/'.join(p.split('/')[:3])]+1) for p in d['paths']];\
	[print(f'  {g}: {c} endpoints') for g,c in sorted(groups.items())];\
	print(f'\n  TOTAL: {len(d[\"paths\"])} endpoints')" 2>/dev/null || echo "  API not reachable"

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

.PHONY: clean
clean: ## Stop services and remove Docker volumes (DESTRUCTIVE)
	@echo "WARNING: This will remove all Docker volumes (database data)."
	@read -p "Continue? [y/N] " confirm && [ "$$confirm" = "y" ] || exit 1
	docker compose down -v
	@echo "Cleaned up."

.PHONY: prune
prune: ## Reclaim Docker disk space (images/build cache/stopped containers)
	@echo "Pruning Docker disk space..."
	@docker system prune -f
	@docker builder prune -f
	@echo ""
	@echo "Disk usage after prune:"
	@docker exec bma-health-db df -h / 2>/dev/null | head -3 || echo "  (postgres container not running)"

.PHONY: install-prune-cron
install-prune-cron: ## Install weekly LaunchAgent that runs `docker system prune` (macOS, Sun 03:00)
	@bash scripts/install-prune-cron.sh

.PHONY: uninstall-prune-cron
uninstall-prune-cron: ## Remove the weekly Docker prune LaunchAgent
	@launchctl unload $$HOME/Library/LaunchAgents/com.bma.docker-prune.plist 2>/dev/null || true
	@rm -f $$HOME/Library/LaunchAgents/com.bma.docker-prune.plist
	@echo "Uninstalled"

.PHONY: clean-venv
clean-venv: ## Remove virtual environment
	rm -rf $(VENV_DIR)

.PHONY: clean-pyc
clean-pyc: ## Remove Python cache files
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

.PHONY: clean-reports
clean-reports: ## Remove all generated PDF reports
	find data/reports -name "*.pdf" -delete 2>/dev/null || true
	find data/reports -name "*.hash" -delete 2>/dev/null || true
	@echo "Report cache cleared."

# ---------------------------------------------------------------------------
# Production
# ---------------------------------------------------------------------------

.PHONY: prod
prod: ## Start all services in production mode
	docker compose -f docker-compose.yml up -d --build
	@echo ""
	@echo "  Production API: http://localhost:$(DOCKER_API_PORT)"
	@echo ""

.PHONY: build
build: ## Build Docker images
	docker compose build

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help message
	@echo ""
	@echo "  BMA Health — One-Stop Backend"
	@echo "  =============================="
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' Makefile | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  API Docs:  http://localhost:$(API_PORT)/docs"
	@echo "  ReDoc:     http://localhost:$(API_PORT)/redoc"
	@echo ""
