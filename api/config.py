"""
Configuration loaded from environment variables.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://postgres:bma_health_dev@localhost:5433/bma_health")

# v3 (2026-04-27) — separate read/write DSNs for least-privilege.
# Reader (api_user — bma_api_reader role): SELECT on public.* only.
# Writer (etl_user — bma_etl_writer role): INSERT/UPDATE on private.* +
#                                          EXECUTE public.refresh_all_mvs.
# Both fall back to DATABASE_URL so single-DSN dev setups keep working.
DATABASE_URL_READER: str = os.getenv("DATABASE_URL_READER", DATABASE_URL)
DATABASE_URL_WRITER: str = os.getenv("DATABASE_URL_WRITER", DATABASE_URL)

REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
API_KEY: str = os.getenv("API_KEY", "changeme-dev-key")
CORS_ORIGINS: list[str] = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")
    if o.strip()
]
RATE_LIMIT_PUBLIC: int = int(os.getenv("RATE_LIMIT_PUBLIC", "60"))      # requests per minute
RATE_LIMIT_ANALYST: int = int(os.getenv("RATE_LIMIT_ANALYST", "300"))   # requests per minute
ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "admin")
SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me-in-production-use-random-secret")

# --- LLM / AI Chat ---
LMSTUDIO_URL: str = os.getenv("LMSTUDIO_URL", "http://localhost:5555")
LLM_MODEL: str = os.getenv("LLM_MODEL", "google/gemma-4-26b-a4b")
LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "2000"))
LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "120"))
CIRCUIT_BREAKER_THRESHOLD: int = int(os.getenv("CIRCUIT_BREAKER_THRESHOLD", "3"))
CIRCUIT_BREAKER_RECOVERY: int = int(os.getenv("CIRCUIT_BREAKER_RECOVERY", "60"))

# --- JWT Auth ---
JWT_SECRET: str = os.getenv("JWT_SECRET", os.getenv("SECRET_KEY", "change-me-in-production-use-random-secret"))
JWT_EXPIRATION: int = int(os.getenv("JWT_EXPIRATION", "3600"))

# --- Report Generation ---
# TECTONIC_PATH: explicit path wins; otherwise auto-detect via PATH so the
# same config works on macOS (Homebrew), Linux (apt/snap), and Docker.
def _detect_tectonic_path() -> str:
    explicit = os.getenv("TECTONIC_PATH", "").strip()
    if explicit:
        return explicit
    import shutil
    found = shutil.which("tectonic")
    if found:
        return found
    # Last-resort fallback (macOS Homebrew default) — generation will fail
    # later with a clear FileNotFoundError that tells the operator what to set.
    return "/opt/homebrew/bin/tectonic"


TECTONIC_PATH: str = _detect_tectonic_path()
TECTONIC_TIMEOUT: int = int(os.getenv("TECTONIC_TIMEOUT", "120"))
REPORTS_DIR: str = os.getenv("REPORTS_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "reports"))

# --- Cache TTLs (used by new routers) ---
CACHE_TTL_HEALTH: int = int(os.getenv("CACHE_TTL_HEALTH", "300"))
CACHE_TTL_STATIC: int = int(os.getenv("CACHE_TTL_STATIC", "3600"))

# Known-insecure defaults — refused outright in production, warned in dev.
# Pattern matches: env var unset → falls through to one of these strings →
# we either fail (production) or log loudly (dev/test).
_INSECURE_DEFAULTS = {
    "API_KEY": {"changeme-dev-key"},
    "ADMIN_PASSWORD": {"admin", "password", "changeme"},
    "SECRET_KEY": {
        "change-me-in-production-use-random-secret",
        "change-me",
        "secret",
    },
    "JWT_SECRET": {
        "change-me-in-production-use-random-secret",
        "change-me",
        "default",
    },
}

# Minimum secret strength (bytes of entropy expected). 16 = ok for dev,
# 32+ recommended. Anything shorter is rejected in production.
_MIN_SECRET_LENGTH = 16


def _is_production() -> bool:
    """True iff ENVIRONMENT is explicitly 'production'.

    Note: we deliberately do NOT default ENVIRONMENT to anything safe-sounding.
    A typo or unset env in a real deployment must FAIL LOUDLY rather than
    silently fall through to dev mode. validate_production_config() honours
    that by treating an unset/empty ENVIRONMENT as production-like.
    """
    return os.getenv("ENVIRONMENT", "").strip().lower() == "production"


def _is_dev_or_test() -> bool:
    env = os.getenv("ENVIRONMENT", "").strip().lower()
    return env in ("development", "test", "dev", "testing")


def validate_production_config():
    """Refuse to start with insecure secrets in anything that isn't dev/test.

    Behaviour:
      * production / unset env: any default OR too-short secret raises.
      * dev/test (explicit): logged as a loud warning but allowed.

    Called from main.py at FastAPI startup. Importing config.py alone does
    NOT run validation, so unit-test imports stay cheap.
    """
    import logging
    log = logging.getLogger("config")

    strict = not _is_dev_or_test()  # production OR unset → strict
    problems: list[str] = []

    for key, bad_values in _INSECURE_DEFAULTS.items():
        current = globals().get(key) or ""
        if current in bad_values:
            problems.append(f"{key} is using a known-insecure default value")
        elif key in ("SECRET_KEY", "JWT_SECRET", "API_KEY") and len(current) < _MIN_SECRET_LENGTH:
            problems.append(
                f"{key} is too short ({len(current)} chars, need >= {_MIN_SECRET_LENGTH})"
            )

    if not problems:
        return

    # Pretty-formatted list for the operator
    bullet = "\n  - "
    msg = (
        f"\u26a0\ufe0f  Insecure secret configuration detected:{bullet}"
        + bullet.join(problems)
        + "\n\nGenerate a strong secret with:"
        + "\n  python3 -c \"import secrets; print(secrets.token_urlsafe(48))\""
        + "\n\nThen set the environment variables in your .env file."
    )

    if strict:
        # Production OR unset ENVIRONMENT — refuse to start
        raise RuntimeError("FATAL — refusing to start.\n" + msg)
    else:
        # Explicit dev/test — allow but log loudly so it's visible in CI logs
        log.error(msg)
