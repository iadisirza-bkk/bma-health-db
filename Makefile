# ============================================================================
# BMA Health — One-Stop Backend Makefile
# ============================================================================
#
# Usage:
#   make dev              — Start API locally with hot-reload (port 9002)
#   make up               — Start all services via Docker Compose
#   make down             — Stop all Docker services
#   make infra            — Start only PostgreSQL + Redis
#   make test             — Run test suite
#   make generate-reports — Trigger PDF report generation
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
migrate: ## Run all SQL migrations against local PostgreSQL
	@for f in db/migrations/*.sql; do \
		echo "Applying $$f ..."; \
		psql "$(DB_URL)" -f "$$f"; \
	done
	@echo "Migrations complete."

.PHONY: seed
seed: ## Run seed data against local PostgreSQL
	@for f in db/seeds/*.sql; do \
		echo "Seeding $$f ..."; \
		psql "$(DB_URL)" -f "$$f"; \
	done
	@echo "Seeding complete."

.PHONY: db-reset
db-reset: ## Drop and recreate database (DESTRUCTIVE)
	@echo "WARNING: This will destroy all data in bma_health database."
	@read -p "Continue? [y/N] " confirm && [ "$$confirm" = "y" ] || exit 1
	psql "postgresql://postgres:bma_health_dev@localhost:5433/postgres" \
		-c "DROP DATABASE IF EXISTS bma_health;" \
		-c "CREATE DATABASE bma_health;"
	$(MAKE) migrate
	$(MAKE) seed
	@echo "Database reset complete."

# ---------------------------------------------------------------------------
# Testing & Quality
# ---------------------------------------------------------------------------

.PHONY: test
test: ## Run test suite (68 tests)
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
