"""
Pytest fixtures for BMA Health API integration tests.
Uses the real Docker Compose PostgreSQL — tests are read-only.
"""
import os
import sys

import pytest

# Add api/ to path so we can import the FastAPI app
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from httpx import ASGITransport, AsyncClient


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def api_key():
    return os.getenv("API_KEY", "dev-api-key")


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
