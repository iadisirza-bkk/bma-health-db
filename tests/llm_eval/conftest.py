"""LLM eval fixtures — only run when LMStudio is reachable.

These tests hit the *real* orchestrator with the real LMStudio backend
and the real DB. They are slow (30–90s each) and non-deterministic, so
they are skipped automatically when:

  - LMSTUDIO_URL is unreachable, OR
  - the API health endpoint is not 200, OR
  - env var `BMA_RUN_LLM_EVAL=1` is not set.

Set `BMA_RUN_LLM_EVAL=1` to opt in:

    BMA_RUN_LLM_EVAL=1 python3 -m pytest tests/llm_eval -v
"""
from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import pytest


def _reachable(url: str, timeout: float = 2.0) -> bool:
    """Cheap TCP probe — does NOT require auth or a specific endpoint."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    """Skip the entire eval module unless explicitly opted-in AND services are up."""
    if not os.environ.get("BMA_RUN_LLM_EVAL"):
        skip = pytest.mark.skip(reason="set BMA_RUN_LLM_EVAL=1 to run LLM evals")
        for item in items:
            item.add_marker(skip)
        return

    lm_url = os.environ.get("LMSTUDIO_URL", "http://localhost:5555")
    api_url = os.environ.get("BMA_API_URL", "http://localhost:9002")

    if not _reachable(lm_url):
        skip = pytest.mark.skip(reason=f"LMStudio unreachable at {lm_url}")
        for item in items:
            item.add_marker(skip)
    elif not _reachable(api_url):
        skip = pytest.mark.skip(reason=f"BMA API unreachable at {api_url}")
        for item in items:
            item.add_marker(skip)
