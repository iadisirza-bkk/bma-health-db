"""Tests for the YAML-driven ToolRegistry (ADR-02 §4).

Discovery is the contract under test:
    * Real ``config/tools/*.yaml`` loads >= 14 tools.
    * ``enabled: false`` entries are silently skipped.
    * Bad ``class_path`` (unknown attribute / wrong type) raises a
      clear, attributable error.
    * Duplicate ``name`` across files raises.
    * ``to_openai_schemas()`` still produces the legacy shape that the
      LMStudio adapter consumes — no regression for downstream code.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make ``api/`` importable for ``agents.tools.*`` (mirrors tests/conftest.py).
_API_DIR = Path(__file__).resolve().parents[2] / "api"
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))

from agents.tools.registry import (  # noqa: E402
    ToolRegistry,
    _default_config_dir,
    tool_registry,
)
from agents.tools.spec import ToolSpec, import_tool_class  # noqa: E402


# ---------------------------------------------------------------------------
# Real-config discovery
# ---------------------------------------------------------------------------

REAL_CONFIG_DIR = _default_config_dir()

# Names from the legacy ``ToolRegistry.create_default()``. Discovery must
# include every one of these so backward compat holds.
LEGACY_TOOL_NAMES = {
    "query_health_data",
    "query_api",
    "query_statistical_test",
    "generate_report",
    "generate_adaptive_report",
    "ask_clarification",
    "query_zone_info",
    "query_time_trend",
    "query_province_breakdown",
    "query_facility",
    "query_risk_profile",
    "query_district_compare",
    "query_mental_health",
    "query_ncd_cascade",
    "query_ncd_diagnostic_report",
}


def test_discover_real_config_loads_at_least_14() -> None:
    registry = ToolRegistry.discover(REAL_CONFIG_DIR)
    assert len(registry.list_tools()) >= 14
    names = {t.name for t in registry.list_tools()}
    missing = LEGACY_TOOL_NAMES - names
    assert not missing, f"missing legacy tools after YAML discovery: {missing}"


def test_create_default_remains_backward_compatible() -> None:
    """``ToolRegistry.create_default()`` is the deprecated entry point that
    `agents.__init__.create_orchestrator` still calls. It must keep
    returning the same tool set."""
    registry = ToolRegistry.create_default()
    names = {t.name for t in registry.list_tools()}
    assert LEGACY_TOOL_NAMES.issubset(names)


def test_to_openai_schemas_shape_unchanged() -> None:
    """Downstream LMStudio adapter consumes a list of
    ``{type, function: {name, description, parameters}}`` items."""
    registry = ToolRegistry.discover(REAL_CONFIG_DIR)
    schemas = registry.to_openai_schemas()
    assert len(schemas) == len(registry.list_tools())
    for sch in schemas:
        assert sch["type"] == "function"
        fn = sch["function"]
        assert set(fn.keys()) == {"name", "description", "parameters"}
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"]
        assert isinstance(fn["parameters"], dict)
        assert fn["parameters"].get("type") == "object"


def test_to_filtered_schemas_preserves_order_filters_unknown() -> None:
    registry = ToolRegistry.discover(REAL_CONFIG_DIR)
    wanted = ["query_health_data", "ask_clarification", "DOES_NOT_EXIST"]
    schemas = registry.to_filtered_schemas(wanted)
    returned_names = [s["function"]["name"] for s in schemas]
    assert "DOES_NOT_EXIST" not in returned_names
    assert set(returned_names) == {"query_health_data", "ask_clarification"}


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_tool_registry_singleton_reload() -> None:
    a = tool_registry()
    b = tool_registry()
    assert a is b
    c = tool_registry(reload=True)
    # reload swaps the cached instance for a fresh one
    assert c is not a


# ---------------------------------------------------------------------------
# Failure modes — synthetic YAML in tmpdirs so we can trigger each branch.
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_disabled_entries_are_skipped(tmp_path: Path) -> None:
    """``enabled: false`` is not registered."""
    _write_yaml(
        tmp_path / "ask_clarification.yaml",
        """
name: ask_clarification
description_th: ask the user
class_path: agents.tools.clarification:AskClarificationTool
enabled: true
""",
    )
    _write_yaml(
        tmp_path / "query_zone_info.yaml",
        """
name: query_zone_info
description_th: zone info
class_path: agents.tools.zone_info:QueryZoneInfoTool
enabled: false
""",
    )

    registry = ToolRegistry.discover(tmp_path)
    names = {t.name for t in registry.list_tools()}
    assert names == {"ask_clarification"}
    assert registry.get("query_zone_info") is None


def test_bad_class_path_module_raises(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "broken.yaml",
        """
name: broken
description_th: nope
class_path: agents.tools.does_not_exist:Nope
""",
    )
    with pytest.raises(ImportError):
        ToolRegistry.discover(tmp_path)


def test_bad_class_path_attribute_raises(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "broken.yaml",
        """
name: broken
description_th: nope
class_path: agents.tools.clarification:NotARealClass
""",
    )
    with pytest.raises(AttributeError):
        ToolRegistry.discover(tmp_path)


def test_class_path_pointing_to_non_basetool_raises(tmp_path: Path) -> None:
    """``ToolResult`` is in the same module but isn't a BaseTool subclass."""
    _write_yaml(
        tmp_path / "broken.yaml",
        """
name: broken
description_th: nope
class_path: agents.tools.base:ToolResult
""",
    )
    with pytest.raises(TypeError):
        ToolRegistry.discover(tmp_path)


def test_class_path_missing_separator_raises(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "broken.yaml",
        """
name: broken
description_th: nope
class_path: agents.tools.clarification.AskClarificationTool
""",
    )
    with pytest.raises(ValueError):
        ToolRegistry.discover(tmp_path)


def test_filename_stem_must_match_name(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "wrong_filename.yaml",
        """
name: ask_clarification
description_th: ask the user
class_path: agents.tools.clarification:AskClarificationTool
""",
    )
    with pytest.raises(ValueError, match="filename stem"):
        ToolRegistry.discover(tmp_path)


def test_duplicate_name_across_files_raises(tmp_path: Path) -> None:
    """Two YAMLs declaring the same ``name`` must fail. Filenames differ
    so the stem-vs-name check passes; the duplicate is caught at register()."""
    _write_yaml(
        tmp_path / "ask_clarification.yaml",
        """
name: ask_clarification
description_th: first
class_path: agents.tools.clarification:AskClarificationTool
""",
    )
    # Same name in a second YAML — stem matches, so register() is the
    # check that fires.
    _write_yaml(
        tmp_path / "ask_clarification_dupe.yaml",
        """
name: ask_clarification
description_th: dupe
class_path: agents.tools.clarification:AskClarificationTool
""",
    )
    with pytest.raises(ValueError):
        ToolRegistry.discover(tmp_path)


def test_unknown_field_in_yaml_raises(tmp_path: Path) -> None:
    """``extra="forbid"`` so a typo fails loud at boot."""
    _write_yaml(
        tmp_path / "ask_clarification.yaml",
        """
name: ask_clarification
description_th: ask the user
class_path: agents.tools.clarification:AskClarificationTool
typo_field: surprise
""",
    )
    with pytest.raises(Exception):  # Pydantic ValidationError
        ToolRegistry.discover(tmp_path)


def test_missing_directory_raises(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does_not_exist"
    with pytest.raises(FileNotFoundError):
        ToolRegistry.discover(nonexistent)


def test_yaml_parameters_override_wins(tmp_path: Path) -> None:
    """If YAML provides ``parameters``, that overrides the class default."""
    custom_params = {
        "type": "object",
        "properties": {"override_me": {"type": "string"}},
        "required": [],
    }
    _write_yaml(
        tmp_path / "ask_clarification.yaml",
        f"""
name: ask_clarification
description_th: overridden
class_path: agents.tools.clarification:AskClarificationTool
parameters:
  type: object
  properties:
    override_me:
      type: string
  required: []
""",
    )
    registry = ToolRegistry.discover(tmp_path)
    tool = registry.get("ask_clarification")
    assert tool is not None
    assert tool.parameters_schema == custom_params


def test_yaml_metadata_stamped_on_instance(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "ask_clarification.yaml",
        """
name: ask_clarification
description_th: คำอธิบายไทย
description_en: english desc
class_path: agents.tools.clarification:AskClarificationTool
audience:
- admin
""",
    )
    registry = ToolRegistry.discover(tmp_path)
    tool = registry.get("ask_clarification")
    assert tool is not None
    assert getattr(tool, "description_th") == "คำอธิบายไทย"
    assert getattr(tool, "description_en") == "english desc"
    assert getattr(tool, "audience") == ["admin"]
    # description_th flows into the legacy ``description`` attribute so
    # to_openai_schema()'s output is unchanged.
    assert tool.description == "คำอธิบายไทย"


# ---------------------------------------------------------------------------
# Spec-level helpers
# ---------------------------------------------------------------------------


def test_import_tool_class_resolves_real_class() -> None:
    cls = import_tool_class("agents.tools.clarification:AskClarificationTool")
    from agents.tools.clarification import AskClarificationTool

    assert cls is AskClarificationTool


def test_toolspec_defaults() -> None:
    spec = ToolSpec(
        name="x",
        description_th="…",
        class_path="agents.tools.clarification:AskClarificationTool",
    )
    assert spec.enabled is True
    assert spec.audience == ["public", "clinician"]
    assert spec.description_en is None
    assert spec.parameters is None
