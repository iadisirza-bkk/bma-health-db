"""BMA-MED Analytics Privilege Layer (S1 skeleton).

This is the FastAPI app that — in S2-S4 — will hold the only DB role grant
for row-level reads against bma_med.* fact / dim tables. In S1 this module
just establishes the contract: every endpoint returns HTTP 501 with a typed
JSON envelope describing what it WILL do once filled in.

Boot sequence:
  1. Bootstrap `bma_med.security` import (the security helpers live in the
     sibling /Users/dev/bma-med repo as a flat `security` package; we alias
     it under the `bma_med.security` namespace so the routers can import
     `bma_med.security.k_anon` and `bma_med.security.audit` cleanly).
  2. Mount the three router modules.
  3. Expose /health and a startup-event audit log.
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
import types

# ---------------------------------------------------------------------------
# Step 1: make `bma_med.security` importable.
# ---------------------------------------------------------------------------
_BMA_MED_PATH = "/Users/dev/bma-med"
if _BMA_MED_PATH not in sys.path and os.path.isdir(_BMA_MED_PATH):
    sys.path.insert(0, _BMA_MED_PATH)

# After path insertion, `security` is importable as a top-level package.
# Alias it as `bma_med.security` so callers can use the canonical dotted name.
if "bma_med" not in sys.modules:
    _bma_med_pkg = types.ModuleType("bma_med")
    _bma_med_pkg.__path__ = [_BMA_MED_PATH]  # type: ignore[attr-defined]
    sys.modules["bma_med"] = _bma_med_pkg
if "bma_med.security" not in sys.modules:
    _security_pkg = importlib.import_module("security")
    sys.modules["bma_med.security"] = _security_pkg
    sys.modules["bma_med.security.audit"] = importlib.import_module(
        "security.audit")
    sys.modules["bma_med.security.k_anon"] = importlib.import_module(
        "security.k_anon")

# ---------------------------------------------------------------------------
# Step 2: now that the alias is in place, import FastAPI bits + routers.
# ---------------------------------------------------------------------------
from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from analytics.routers.contingency import router as contingency_router  # noqa: E402
from analytics.routers.regression import router as regression_router  # noqa: E402
from analytics.routers.economics import router as economics_router  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("analytics.main")

VERSION = "s1-skeleton"

app = FastAPI(
    title="BMA-MED Analytics Privilege Layer",
    version=VERSION,
    description=("S1 skeleton. Endpoints return HTTP 501 with typed envelopes "
                 "describing what they will do in S2-S4."),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(contingency_router)
app.include_router(regression_router)
app.include_router(economics_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": VERSION}


@app.on_event("startup")
def _startup() -> None:
    # Confirm the bma_med.security alias actually works at runtime.
    try:
        from bma_med.security import audit as _audit  # noqa: F401
        from bma_med.security import k_anon as _k_anon  # noqa: F401
        sec_ok = True
    except Exception as exc:  # pragma: no cover - defensive
        sec_ok = False
        logger.error("failed to import bma_med.security: %s", exc)
    logger.info("analytics-skeleton ready (bma_med.security import: %s)",
                "OK" if sec_ok else "FAIL")
