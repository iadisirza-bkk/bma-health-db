"""Module B: multivariable regression + odds-ratio endpoint (S1 stub).

In S3 these endpoints will run logistic / linear regression against the
analytics-role DB connection. The /analytics/odds_ratio endpoint exists for
the meeting-style "OR across three variables" use case.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from bma_med.security.audit import audit_event
from bma_med.security.k_anon import assert_no_individual_fields

logger = logging.getLogger("analytics.regression")

router = APIRouter(tags=["analytics"])

SPEC_URL = "https://docs/ULTRAPLAN.md#module-b"


class RegressionRequest(BaseModel):
    outcome: str = Field(..., description="Outcome variable name.")
    predictors: List[str] = Field(...,
                                  description="One or more predictor names.")
    family: Literal["logistic", "linear"] = Field(
        ..., description="Model family.")
    adjust_for: List[str] = Field(default_factory=list,
                                  description="Optional covariates.")
    group_by: List[str] = Field(default_factory=list,
                                description="Optional stratification keys.")


class RegressionResponse(BaseModel):
    status: str
    planned_in_sprint: str
    spec_url: str
    n: Optional[int] = None
    coefficients: Optional[List[Dict[str, Any]]] = None
    rows: Optional[List[Dict[str, Any]]] = None


class OddsRatioRequest(BaseModel):
    outcome: str = Field(..., description="Binary outcome variable name.")
    exposure: str = Field(..., description="Binary exposure variable name.")
    adjust_for: List[str] = Field(default_factory=list,
                                  description="Optional covariates.")
    group_by: List[str] = Field(default_factory=list,
                                description="Optional stratification keys.")


class OddsRatioResponse(BaseModel):
    status: str
    planned_in_sprint: str
    spec_url: str
    n: Optional[int] = None
    odds_ratio: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    p_value: Optional[float] = None
    rows: Optional[List[Dict[str, Any]]] = None


@router.post("/analytics/regression", status_code=501,
             response_model=RegressionResponse)
def regression(req: RegressionRequest) -> RegressionResponse:
    """Multivariable logistic / linear regression. Will be filled in S3."""
    resp = RegressionResponse(
        status="not_implemented",
        planned_in_sprint="S3",
        spec_url=SPEC_URL,
    )
    if resp.rows is not None:
        assert_no_individual_fields(resp.rows)
    event = audit_event(
        operator="analytics-skeleton",
        operation="STUB_REGRESSION",
        resource="bma_med.analytics.regression",
        params=req.model_dump(),
        detail={"sprint": "S1", "planned_in": "S3", "status": 501,
                "family": req.family},
    )
    logger.info("audit_event %s", event)
    return resp


@router.post("/analytics/odds_ratio", status_code=501,
             response_model=OddsRatioResponse)
def odds_ratio(req: OddsRatioRequest) -> OddsRatioResponse:
    """OR with optional adjustment / stratification. Will be filled in S3."""
    resp = OddsRatioResponse(
        status="not_implemented",
        planned_in_sprint="S3",
        spec_url=SPEC_URL,
    )
    if resp.rows is not None:
        assert_no_individual_fields(resp.rows)
    event = audit_event(
        operator="analytics-skeleton",
        operation="STUB_ODDS_RATIO",
        resource="bma_med.analytics.odds_ratio",
        params=req.model_dump(),
        detail={"sprint": "S1", "planned_in": "S3", "status": 501},
    )
    logger.info("audit_event %s", event)
    return resp
