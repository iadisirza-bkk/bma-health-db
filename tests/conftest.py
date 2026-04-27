"""
Pytest fixtures for BMA Health API integration tests.
Uses the real Docker Compose PostgreSQL — tests are read-only.
"""
import os
import sys

import pytest

# Add api/ to path so we can import the FastAPI app
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

# Mark this run as test environment so config.validate_production_config()
# treats failures as warnings instead of fatal errors. The values below are
# intentionally fixed so test runs are reproducible.
os.environ.setdefault("ENVIRONMENT", "test")

# Raise rate limit for test suite (218+ tests hit the 60/min default)
os.environ.setdefault("RATE_LIMIT_PUBLIC", "5000")

# IDCARD hashing secret for tests — value is intentionally fixed and only used
# in the test environment so hash outputs are reproducible across runs.
os.environ.setdefault(
    "IDCARD_HASH_SECRET",
    "test-only-not-for-production-fixed-for-reproducibility",
)
# Test-only credentials. Long enough to pass MIN_SECRET_LENGTH validation
# but recognizably non-production so they can never be confused with real keys.
os.environ.setdefault("API_KEY", "dev-api-key-for-tests-only")
os.environ.setdefault("ADMIN_PASSWORD", "test-only-admin-password-not-for-production")
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-production-32+chars")
os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-not-for-production-32+chars")

from httpx import ASGITransport, AsyncClient


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def api_key():
    return os.getenv("API_KEY", "dev-api-key")


@pytest.fixture(scope="session")
def admin_auth_header():
    """Authorization header value for admin-protected endpoints.

    Reads ADMIN_PASSWORD from env (set above to a long test-only string)
    so tests stay correct even when the password changes.
    """
    return {"Authorization": f"Bearer {os.environ['ADMIN_PASSWORD']}"}


@pytest.fixture(scope="session")
def app():
    """Import the FastAPI app once per test session."""
    from main import app as _app
    return _app


@pytest.fixture()
async def client(app, api_key):
    """Async HTTP client that talks directly to the ASGI app (no server needed)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.headers["X-API-Key"] = api_key
        yield ac


@pytest.fixture()
async def public_client(app):
    """Client without API key — for public / health endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
