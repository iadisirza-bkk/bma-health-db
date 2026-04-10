"""
Structured error handling for the BMA Health API.
All business exceptions extend BMAException and are caught by the global handler.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
import logging

logger = logging.getLogger("bma.errors")


class BMAException(Exception):
    """Base exception for all BMA business errors."""
    def __init__(self, message: str, error_code: str, status_code: int = 400, detail: dict = None):
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.detail = detail or {}
        super().__init__(message)


class NotFoundError(BMAException):
    """Entity not found."""
    def __init__(self, entity: str, identifier: str):
        super().__init__(
            message=f"{entity} '{identifier}' not found",
            error_code="NOT_FOUND",
            status_code=404,
        )


class DataSuppressedError(BMAException):
    """Result suppressed due to k-anonymity."""
    def __init__(self, reason: str = "k-anonymity threshold"):
        super().__init__(
            message=f"Data suppressed for privacy: {reason}",
            error_code="K_ANONYMITY_SUPPRESSED",
            status_code=200,  # Not a real error — privacy protection
            detail={"k_anonymity_threshold": 5},
        )


class DataNotAvailableError(BMAException):
    """Requested data field is not populated."""
    def __init__(self, field: str, suggestion: str = ""):
        super().__init__(
            message=f"Data not available: {field}",
            error_code="DATA_NOT_AVAILABLE",
            status_code=200,
            detail={"field": field, "suggestion": suggestion or "ต้องรอข้อมูลจาก HDC"},
        )


class InvalidParameterError(BMAException):
    """Invalid query parameter."""
    def __init__(self, param: str, value: str, valid_values: list = None):
        detail = {"parameter": param, "provided": value}
        if valid_values:
            detail["valid_values"] = valid_values
        super().__init__(
            message=f"Invalid {param}: '{value}'",
            error_code="INVALID_PARAMETER",
            status_code=400,
            detail=detail,
        )


class ExternalAPIError(BMAException):
    """External service (ArcGIS, etc.) is unavailable."""
    def __init__(self, service: str, detail_msg: str = ""):
        super().__init__(
            message=f"External service '{service}' unavailable: {detail_msg}",
            error_code="EXTERNAL_API_ERROR",
            status_code=502,
        )


# ---------------------------------------------------------------------------
# Global exception handler — register on FastAPI app
# ---------------------------------------------------------------------------


async def bma_exception_handler(request: Request, exc: BMAException) -> JSONResponse:
    """Handle all BMAException subclasses with consistent JSON response."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": exc.error_code,
            "message": exc.message,
            **exc.detail,
        },
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unexpected errors — log full traceback, return safe message."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error_code": "INTERNAL_ERROR",
            "message": "An unexpected error occurred. Please try again later.",
        },
    )
