"""Module A: 2-way contingency / chi-squared test (S1 stub).

Will be filled in S2 with real DB-backed cross-tabulation and chi-squared
inference. For S1 every endpoint returns HTTP 501 with a typed envelope so
the frontend contract is stable while the privilege layer is being wired.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from bma_med.security.audit import audit_event
from bma_med.security.k_anon import assert_no_individual_fields

logger = logging.getLogger("analytics.contingency")

router = APIRouter(tags=["analytics"])

SPEC_URL = "https://docs/ULTRAPLAN.md#module-a"


class ContingencyRequest(BaseModel):
    variable_a: str = Field(..., description="First categorical variable name.")
    variable_b: str = Field(..., description="Second categorical variable name.")
    group_by: List[str] = Field(default_factory=list,
                                description="Optional stratification keys.")
    adjust_for: List[str] = Field(default_factory=list,
                                  description="Optional covariates (S2+).")


class ContingencyResponse(BaseModel):
    status: str
    planned_in_sprint: str
    spec_url: str
    n: Optional[int] = None
    table: Optional[List[Dict[str, Any]]] = None
    chi_square: Optional[float] = None
    p_value: Optional[float] = None
    rows: Optional[List[Dict[str, Any]]] = None


@router.post("/analytics/contingency", status_code=501,
             response_model=ContingencyResponse)
def contingency(req: ContingencyRequest) -> ContingencyResponse:
    """2-way contingency / chi-squared test. Will be filled in S2."""
    resp = ContingencyResponse(
        status="not_implemented",
        planned_in_sprint="S2",
        spec_url=SPEC_URL,
    )
    # Privacy invariant: aggregate-only. If a 'rows' field is ever populated,
    # gate it through assert_no_individual_fields BEFORE serializing.
    if resp.rows is not None:
        assert_no_individual_fields(resp.rows)

    # Stub audit so the audit-log pipe is verifiable end-to-end in S1.
    event = audit_event(
        operator="analytics-skeleton",
        operation="STUB_CONTINGENCY",
        resource="bma_med.analytics.contingency",
        params=req.model_dump(),
        detail={"sprint": "S1", "planned_in": "S2", "status": 501},
    )
    logger.info("audit_event %s", event)
    return resp
