"""
Configuration loaded from environment variables.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://postgres:bma_health_dev@localhost:5433/bma_health")
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
TECTONIC_PATH: str = os.getenv("TECTONIC_PATH", "/opt/homebrew/bin/tectonic")
TECTONIC_TIMEOUT: int = int(os.getenv("TECTONIC_TIMEOUT", "120"))
REPORTS_DIR: str = os.getenv("REPORTS_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "reports"))

# --- Cache TTLs (used by new routers) ---
CACHE_TTL_HEALTH: int = int(os.getenv("CACHE_TTL_HEALTH", "300"))
CACHE_TTL_STATIC: int = int(os.getenv("CACHE_TTL_STATIC", "3600"))

_INSECURE_DEFAULTS = {
    "API_KEY": "changeme-dev-key",
    "ADMIN_PASSWORD": "admin",
    "SECRET_KEY": "change-me-in-production-use-random-secret",
}

def validate_production_config():
    """Warn or fail if default credentials are in use."""
    import warnings
    is_prod = os.getenv("ENVIRONMENT", "development") == "production"

    for key, default_val in _INSECURE_DEFAULTS.items():
        current = globals().get(key)
        if current == default_val:
            msg = (
                f"SECURITY: {key} is using the default value. "
                f"Set the {key} environment variable before deploying."
            )
            if is_prod:
                raise RuntimeError(f"FATAL — {msg}")
            else:
                warnings.warn(f"\u26a0\ufe0f  {msg}", stacklevel=2)
