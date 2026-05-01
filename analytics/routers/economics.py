"""Module C: health economics for the MSD audience (S1 stub).

S4 will implement cost-per-positive, drop-the-test simulation, and ICER
between two screening strategies. Each endpoint returns aggregate-only
shapes; never per-patient projections.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from bma_med.security.audit import audit_event
from bma_med.security.k_anon import assert_no_individual_fields

logger = logging.getLogger("analytics.economics")

router = APIRouter(tags=["economics"])

SPEC_URL = "https://docs/ULTRAPLAN.md#module-c"


class CostPerPositiveRequest(BaseModel):
    test_name: str = Field(..., description="Screening test identifier.")
    prevalence: Optional[float] = Field(
        default=None,
        description="Optional prevalence override (computed if absent).")
    test_cost: float = Field(..., description="Per-test cost in THB.")


class CostPerPositiveResponse(BaseModel):
    status: str
    planned_in_sprint: str
    spec_url: str
    n: Optional[int] = None
    cost_per_positive: Optional[float] = None
    rows: Optional[List[Dict[str, Any]]] = None


class DropTheTestRequest(BaseModel):
    test_name: str = Field(..., description="Test to simulate dropping.")
    scenario_changes: Dict[str, Any] = Field(
        default_factory=dict,
        description="Counterfactual parameter overrides.")


class DropTheTestResponse(BaseModel):
    status: str
    planned_in_sprint: str
    spec_url: str
    n: Optional[int] = None
    delta_cost: Optional[float] = None
    delta_positives: Optional[float] = None
    rows: Optional[List[Dict[str, Any]]] = None


class ICERRequest(BaseModel):
    strategy_a: Dict[str, Any] = Field(..., description="Strategy A spec.")
    strategy_b: Dict[str, Any] = Field(..., description="Strategy B spec.")


class ICERResponse(BaseModel):
    status: str
    planned_in_sprint: str
    spec_url: str
    n: Optional[int] = None
    icer: Optional[float] = None
    rows: Optional[List[Dict[str, Any]]] = None


@router.post("/economics/cost-per-positive", status_code=501,
             response_model=CostPerPositiveResponse)
def cost_per_positive(req: CostPerPositiveRequest) -> CostPerPositiveResponse:
    """Cost per positive case detected. Will be filled in S4."""
    resp = CostPerPositiveResponse(
        status="not_implemented",
        planned_in_sprint="S4",
        spec_url=SPEC_URL,
    )
    if resp.rows is not None:
        assert_no_individual_fields(resp.rows)
    event = audit_event(
        operator="analytics-skeleton",
        operation="STUB_COST_PER_POSITIVE",
        resource="bma_med.economics.cost_per_positive",
        params=req.model_dump(),
        detail={"sprint": "S1", "planned_in": "S4", "status": 501},
    )
    logger.info("audit_event %s", event)
    return resp


@router.post("/economics/drop-the-test", status_code=501,
             response_model=DropTheTestResponse)
def drop_the_test(req: DropTheTestRequest) -> DropTheTestResponse:
    """Counterfactual: what if we drop this test? Will be filled in S4."""
    resp = DropTheTestResponse(
        status="not_implemented",
        planned_in_sprint="S4",
        spec_url=SPEC_URL,
    )
    if resp.rows is not None:
        assert_no_individual_fields(resp.rows)
    event = audit_event(
        operator="analytics-skeleton",
        operation="STUB_DROP_THE_TEST",
        resource="bma_med.economics.drop_the_test",
        params=req.model_dump(),
        detail={"sprint": "S1", "planned_in": "S4", "status": 501},
    )
    logger.info("audit_event %s", event)
    return resp


@router.post("/economics/icer", status_code=501,
             response_model=ICERResponse)
def icer(req: ICERRequest) -> ICERResponse:
    """Incremental cost-effectiveness ratio. Will be filled in S4."""
    resp = ICERResponse(
        status="not_implemented",
        planned_in_sprint="S4",
        spec_url=SPEC_URL,
    )
    if resp.rows is not None:
        assert_no_individual_fields(resp.rows)
    event = audit_event(
        operator="analytics-skeleton",
        operation="STUB_ICER",
        resource="bma_med.economics.icer",
        params=req.model_dump(),
        detail={"sprint": "S1", "planned_in": "S4", "status": 501},
    )
    logger.info("audit_event %s", event)
    return resp
