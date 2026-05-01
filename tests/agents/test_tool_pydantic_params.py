"""Pydantic v2 tool-parameter validation tests (ADR-02 §5).

Each tool ships a ``Parameters: type[BaseModel]`` Pydantic model. The model:
  * exposes a JSON Schema via ``model_json_schema()`` whose top-level
    ``type`` is ``"object"`` (consumed by LLM tool-calling APIs),
  * forbids extra fields (``extra="forbid"``) so typos in tool-call args
    fail loudly at the boundary, and
  * raises ``pydantic.ValidationError`` on bad value types (e.g. an int
    where a string Literal is expected).

A "good" call (with the smallest set of valid fields) must succeed without
raising — execute() bodies are NOT exercised here so the tests don't need
DB / network mocks.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

# Make ``api/`` importable for ``agents.tools.*`` (mirrors tests/conftest.py).
_API_DIR = Path(__file__).resolve().parents[2] / "api"
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))

from agents.tools.adaptive_report import GenerateAdaptiveReportTool  # noqa: E402
from agents.tools.clarification import AskClarificationTool  # noqa: E402
from agents.tools.health_data import QueryHealthDataTool  # noqa: E402
from agents.tools.insights import (  # noqa: E402
    DistrictCompareTool,
    FacilityLookupTool,
    MentalHealthCompareTool,
    NCDCascadeTool,
    ProvinceBreakdownTool,
    RiskProfileTool,
    TimeTrendTool,
)
from agents.tools.ncd_report import NcdDiagnosticReportTool  # noqa: E402
from agents.tools.query_api import QueryAPITool  # noqa: E402
from agents.tools.report import GenerateReportTool  # noqa: E402
from agents.tools.statistical import QueryStatisticalTestTool  # noqa: E402
from agents.tools.zone_info import QueryZoneInfoTool  # noqa: E402


# ---------------------------------------------------------------------------
# Per-tool fixtures: (Tool class, valid args, bad-type args).
# ``valid``     — minimal kwargs that satisfy the Pydantic model.
# ``bad_type``  — kwargs with at least one wrong-type field; must raise.
# ---------------------------------------------------------------------------

ToolCase = tuple[type, dict[str, Any], dict[str, Any]]

TOOL_CASES: list[ToolCase] = [
    (
        QueryHealthDataTool,
        {"group_by": "district"},
        # group_by must be a string Literal — int is invalid.
        {"group_by": 123},
    ),
    (
        QueryAPITool,
        {"endpoint": "overview"},
        # endpoint must be one of the catalog keys.
        {"endpoint": "this_endpoint_does_not_exist"},
    ),
    (
        QueryStatisticalTestTool,
        {"test": "chi_square"},
        # test must be a known statistical test.
        {"test": "not_a_real_test"},
    ),
    (
        GenerateReportTool,
        {"report_type": "comprehensive"},
        # report_type must be a string from the enum.
        {"report_type": 42},
    ),
    (
        GenerateAdaptiveReportTool,
        {"title": "รายงาน", "topic": "เบาหวาน"},
        # title is required and must be str-ish.
        {"title": 123, "topic": "x"},
    ),
    (
        AskClarificationTool,
        {"questions": []},
        # questions must be a list, not a string.
        {"questions": "not a list"},
    ),
    (
        QueryZoneInfoTool,
        {"query_type": "all_zones"},
        # query_type must be a known enum value.
        {"query_type": "not_a_real_query_type"},
    ),
    (
        TimeTrendTool,
        {"disease": "diabetes"},
        # period must be 'month' or 'quarter'.
        {"period": "weekly"},
    ),
    (
        ProvinceBreakdownTool,
        {"top_n": 5},
        # top_n must be an integer (str fails).
        {"top_n": "not a number"},
    ),
    (
        FacilityLookupTool,
        {"zone_code": "01"},
        # list_count must be an integer.
        {"list_count": "five"},
    ),
    (
        RiskProfileTool,
        {"dimension": "all"},
        # dimension is restricted to a literal set.
        {"dimension": "unknown"},
    ),
    (
        DistrictCompareTool,
        {"metric": "diabetes"},
        # metric is restricted to known metrics.
        {"metric": "not_a_metric"},
    ),
    (
        MentalHealthCompareTool,
        {"zone_code": "03"},
        # metric must be one of the named values.
        {"metric": "happiness"},
    ),
    (
        NCDCascadeTool,
        {"disease": "diabetes"},
        # disease must be one of the cascade fields.
        {"disease": "fictional_disease"},
    ),
    (
        NcdDiagnosticReportTool,
        {},
        # Tool takes no params — extra fields must be rejected.
        {"unexpected": True},
    ),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_cls,_valid,_bad", TOOL_CASES, ids=lambda c: getattr(c, "__name__", str(c)))
def test_parameters_is_pydantic_model(tool_cls: type, _valid: dict, _bad: dict) -> None:
    """Every tool exposes a Pydantic v2 model under ``Parameters``."""
    tool = tool_cls()
    assert tool.Parameters is not None, f"{tool_cls.__name__}.Parameters is None"
    assert issubclass(tool.Parameters, BaseModel), (
        f"{tool_cls.__name__}.Parameters must be a Pydantic BaseModel, "
        f"got {tool.Parameters!r}"
    )


@pytest.mark.parametrize("tool_cls,_valid,_bad", TOOL_CASES, ids=lambda c: getattr(c, "__name__", str(c)))
def test_to_openai_schema_has_object_type(tool_cls: type, _valid: dict, _bad: dict) -> None:
    """``to_openai_schema()['function']['parameters']['type']`` must be ``object``."""
    tool = tool_cls()
    schema = tool.to_openai_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == tool.name
    assert isinstance(fn["description"], str) and fn["description"]
    params = fn["parameters"]
    assert params["type"] == "object"
    # Pydantic emits ``additionalProperties: False`` when ``extra="forbid"``.
    assert params.get("additionalProperties") is False, (
        f"{tool_cls.__name__}: Parameters must use ConfigDict(extra='forbid')"
    )


@pytest.mark.parametrize("tool_cls,valid,_bad", TOOL_CASES, ids=lambda c: getattr(c, "__name__", str(c)))
def test_valid_args_parse_cleanly(tool_cls: type, valid: dict, _bad: dict) -> None:
    """Construct the model with valid args. No execute() — pure validation."""
    tool = tool_cls()
    instance = tool.Parameters(**valid)
    # Round-trip dump/reload to ensure ``model_dump()`` is well-defined.
    dumped = instance.model_dump(exclude_none=True)
    tool.Parameters(**dumped)


@pytest.mark.parametrize("tool_cls,_valid,bad", TOOL_CASES, ids=lambda c: getattr(c, "__name__", str(c)))
def test_bad_args_raise_validation_error(tool_cls: type, _valid: dict, bad: dict) -> None:
    """Wrong-typed or unknown-enum args raise ``pydantic.ValidationError``."""
    tool = tool_cls()
    with pytest.raises(ValidationError):
        tool.Parameters(**bad)


@pytest.mark.parametrize("tool_cls,_valid,_bad", TOOL_CASES, ids=lambda c: getattr(c, "__name__", str(c)))
def test_extra_fields_rejected(tool_cls: type, _valid: dict, _bad: dict) -> None:
    """``extra="forbid"`` — a stray field must be a hard error."""
    tool = tool_cls()
    with pytest.raises(ValidationError):
        tool.Parameters(__definitely_not_a_real_field__="x")


def test_validate_args_returns_model_when_parameters_set() -> None:
    """``BaseTool.validate_args`` returns a parsed model for migrated tools."""
    tool = QueryHealthDataTool()
    parsed = tool.validate_args({"group_by": "district"})
    assert isinstance(parsed, BaseModel)
    assert parsed.group_by == "district"


def test_validate_args_raises_on_bad_input() -> None:
    """``BaseTool.validate_args`` propagates ``ValidationError``."""
    tool = QueryHealthDataTool()
    with pytest.raises(ValidationError):
        tool.validate_args({"group_by": "this_value_is_not_in_the_enum"})


def test_to_openai_schema_uses_parameters_when_no_yaml_override() -> None:
    """Without a YAML override, ``to_openai_schema()`` derives the schema
    from ``Parameters.model_json_schema()`` (not the legacy class-level dict)."""
    tool = QueryHealthDataTool()
    schema = tool.to_openai_schema()["function"]["parameters"]
    # The Pydantic-derived schema includes a ``title`` and a ``$defs`` /
    # ``additionalProperties: False`` shape that the hand-written legacy
    # dict does not. We pin the strictness invariant.
    assert schema.get("additionalProperties") is False
    assert schema.get("title") == "QueryHealthDataParams"


def test_to_openai_schema_yaml_override_wins() -> None:
    """When the registry stamps a YAML-supplied dict onto the instance's
    ``parameters_schema`` attribute, ``to_openai_schema()`` returns that
    override verbatim (escape hatch for hand-tuned schemas)."""
    tool = QueryHealthDataTool()
    custom = {"type": "object", "properties": {"override_only": {"type": "string"}}}
    tool.parameters_schema = custom  # instance-level — same as registry does
    assert tool.to_openai_schema()["function"]["parameters"] == custom
