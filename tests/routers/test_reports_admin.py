"""Sprint S9 Task 2.5 — admin /api/v2/admin/reports/* router tests.

Verifies:
    * POST /rebuild enqueues + dispatches a background drain
    * GET /cache/stats returns the cache aggregate dict
    * GET /cache/manifest filters by report_id
    * DELETE /cache/{hash} returns 204 / 404 appropriately
    * Auth: missing creds → 401

The router is mounted on a minimal FastAPI app + the cache and worker
dependencies are monkey-patched so the test never touches tectonic, the
live DB, or any real worker.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

# Set ADMIN env vars BEFORE importing modules that read them at import time.
os.environ.setdefault(
    "BMA_ADMIN_TOKEN", "test-only-admin-token-not-for-production"
)
os.environ.setdefault(
    "ADMIN_PASSWORD", "test-only-admin-password-not-for-production"
)
os.environ.setdefault(
    "SECRET_KEY", "test-only-secret-key-not-for-production-32+chars"
)

from routers import reports_admin as ra  # noqa: E402
from services.reports.pdf_cache import ManifestRow, PdfCache  # noqa: E402


# ---------------------------------------------------------------------------
# Fake worker / cache
# ---------------------------------------------------------------------------


class _FakeWorker:
    def __init__(self):
        self.enqueued: list[str] = []
        self.queue: list = []
        self._force = False
        self.last_run: dict = {}

    def enqueue_popular_set(self, report_id: str) -> int:
        self.enqueued.append(report_id)
        return 5

    async def run_pending(self) -> dict:
        return {
            "built": 5,
            "skipped_cached": 0,
            "failed": 0,
            "duration_s": 0.01,
            "locked": False,
            "queue_size": 0,
        }


class _FakeRegistry:
    def list_ids(self) -> List[str]:
        return ["zone", "whitepaper"]


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def admin_token():
    return os.environ["BMA_ADMIN_TOKEN"]


@pytest.fixture()
def app_and_fakes(tmp_path, monkeypatch):
    fake_worker = _FakeWorker()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    real_cache = PdfCache(cache_root)

    monkeypatch.setattr(ra, "_get_worker", lambda: fake_worker)
    monkeypatch.setattr(ra, "_get_cache", lambda: real_cache)
    monkeypatch.setattr(ra, "_get_registry", lambda: _FakeRegistry())

    app = FastAPI()
    app.include_router(ra.router)
    return app, fake_worker, real_cache


@pytest.fixture()
def client(app_and_fakes):
    app, _w, _c = app_and_fakes
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_rebuild_requires_auth(client):
    resp = client.post("/api/v2/admin/reports/rebuild", json={"force": False})
    assert resp.status_code in (401, 503)


def test_rebuild_enqueues_all_reports(client, admin_token, app_and_fakes):
    _app, worker, _cache = app_and_fakes
    resp = client.post(
        "/api/v2/admin/reports/rebuild",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"report_ids": None, "force": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enqueued"] == 10  # 5 per report × 2 reports
    assert "job_id" in body
    assert worker.enqueued == ["zone", "whitepaper"]


def test_rebuild_specific_report(client, admin_token, app_and_fakes):
    _app, worker, _cache = app_and_fakes
    resp = client.post(
        "/api/v2/admin/reports/rebuild",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"report_ids": ["zone"], "force": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enqueued"] == 5
    assert worker.enqueued == ["zone"]
    assert body["force"] is True


def test_cache_stats(client, admin_token, app_and_fakes):
    _app, _worker, cache = app_and_fakes
    # Seed the cache with a single put so stats has something to report.
    src = Path(cache.root) / "src.pdf"
    src.write_bytes(b"%PDF-1.4 minimal\n")
    cache.put(
        "abc123",
        src,
        report_id="zone",
        fmt="pdf",
        lang="th",
        data_version="v1",
    )
    resp = client.get(
        "/api/v2/admin/reports/cache/stats",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_files"] == 1
    assert body["total_bytes"] > 0
    assert "zone" in body["by_report"]


def test_cache_manifest_filters(client, admin_token, app_and_fakes):
    _app, _worker, cache = app_and_fakes
    src = Path(cache.root) / "src.pdf"
    src.write_bytes(b"%PDF-fake")
    cache.put("zhash", src, report_id="zone", fmt="pdf", lang="th", data_version="v")
    cache.put("whash", src, report_id="whitepaper", fmt="pdf", lang="th", data_version="v")

    resp = client.get(
        "/api/v2/admin/reports/cache/manifest?report_id=zone",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["hash"] == "zhash"
    assert rows[0]["report_id"] == "zone"


def test_cache_delete(client, admin_token, app_and_fakes):
    _app, _worker, cache = app_and_fakes
    src = Path(cache.root) / "src.pdf"
    src.write_bytes(b"%PDF-fake")
    cache.put("delh", src, report_id="zone", fmt="pdf", lang="th", data_version="v")

    resp = client.delete(
        "/api/v2/admin/reports/cache/delh",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 204
    # Second delete: 404
    resp = client.delete(
        "/api/v2/admin/reports/cache/delh",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404
