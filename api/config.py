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

_INSECURE_DEFAULTS = {
    "API_KEY": "changeme-dev-key",
    "ADMIN_PASSWORD": "admin",
    "SECRET_KEY": "change-me-in-production-use-random-secret",
}

def validate_production_config():
    """Warn loudly if default credentials are in use."""
    import warnings
    for key, default_val in _INSECURE_DEFAULTS.items():
        current = globals().get(key)
        if current == default_val:
            warnings.warn(
                f"\u26a0\ufe0f  SECURITY WARNING: {key} is using the default value. "
                f"Set the {key} environment variable before deploying to production.",
                stacklevel=2,
            )
