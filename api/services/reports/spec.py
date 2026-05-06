"""Pydantic v2 models + dataclass holders for the report-descriptor contract.

These models implement the contract spelled out in ADR-03 §2.

Design notes:
    * Pydantic v2 syntax — ``model_config = ConfigDict(...)``.
    * ``extra="forbid"`` everywhere so a YAML typo fails loud at startup
      (mirrors the chart-spec rule from ADR-01 §2).
    * No mutating defaults — ``Field(default_factory=...)`` for ``dict`` /
      ``list`` per Pydantic v2 best practice.
    * ``RenderContext`` and ``RenderedSection`` are intentionally NOT
      Pydantic models. They are mutable holders that travel through the
      orchestrator + each block; a ``@dataclass`` is the right primitive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, TYPE_CHECKING, Union, cast

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover — only used for type hints
    # ``ReportDataCollector`` does not exist yet (ULTRAPLAN S4.2 introduces
    # it); avoid the import at runtime so this module is import-safe today.
    from services.reports.data_collector import ReportDataCollector  # noqa: F401


# Permitted output formats. New formats land here once a ``ReportRenderer``
# subclass is registered; ADR-03 §4 ships ``latex`` + ``html`` in S4 with
# ``pptx`` stubbed.
#
# S7 NOTE: ``latex`` was renamed to ``pdf`` in S7 (the renderer always
# compiles to PDF via tectonic, so calling the format ``latex`` was
# misleading). ``latex`` remains a valid Literal value for one sprint to
# keep YAMLs / programmatic callers / tests written against the old name
# loading without churn — see :mod:`services.reports.format_alias` for
# the alias resolution used at render time. New descriptors should write
# ``pdf``.
ReportFormat = Literal["pdf", "latex", "html", "pptx"]

# Permitted descriptor audience tags. Mirrors ``Audience`` from
# ``agents/tools/spec.py`` plus the ``msd`` bucket needed for the legacy
# MSD slide deck flow that S4.4 will port.
ReportAudience = Literal["public", "clinician", "admin", "msd"]

# Cache invalidation triggers — the orchestrator (S4.5) consults these
# when deciding whether a cached artefact is still fresh.
CacheInvalidator = Literal["data_hash", "time", "manual"]


class StyleSpec(BaseModel):
    """Visual / typographic knobs shared across renderers.

    The defaults match the existing whitepaper LaTeX templates: Sarabun
    (Thai-friendly font) and the BMA brand green ``#00744B``.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    font_family: str = "Sarabun"
    primary_color: str = "#00744B"
    logo_path: Optional[str] = None


class CacheSpec(BaseModel):
    """Caching policy for the rendered artefact (orchestrator consumes)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    enabled: bool = True
    ttl_seconds: Optional[int] = None
    invalidate_on: List[CacheInvalidator] = Field(
        default_factory=lambda: cast("list[CacheInvalidator]", ["data_hash"])
    )


class ParameterOption(BaseModel):
    """One enum option in a typed report parameter (S7 dropdown UI).

    Used only when ``ParameterSpec.type == "enum"``. The frontend renders
    one ``<option>`` per entry; the value goes back to the backend on
    submit and feeds ``str.format(**params)`` substitution at render time.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    value: str
    label_th: str
    label_en: Optional[str] = None


class ParameterSpec(BaseModel):
    """Typed input the frontend renders as a dropdown / textbox / date.

    The backend's existing ``str.format(**params)`` substitution layer
    consumes these values verbatim — see ``ReportService._resolve_descriptor``.
    Adding a parameter here lets the UI show a proper dropdown instead of
    a free-form ``key=value`` text input.

    Validation:
        * ``type == "enum"`` MUST come with a non-empty ``options`` list.
        * ``type`` ∈ {"text", "date"} must NOT supply ``options``.
        * The ``key`` is the same name used for ``{placeholder}``
          substitution in section params (e.g. ``zone_code`` for
          ``"{zone_code}"``).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    key: str
    type: Literal["enum", "text", "date"]
    label_th: str
    label_en: Optional[str] = None
    required: bool = True
    default: Optional[str] = None
    options: List[ParameterOption] = Field(default_factory=list)


class SectionSpec(BaseModel):
    """One ordered section in a report descriptor.

    ``block`` is the registry key of a ``ContentBlock`` subclass, e.g.
    ``"kpi_grid"``. ``params`` are forwarded as-is to the block; each
    block's ``Parameters`` Pydantic model is responsible for validating
    its own slice (the orchestrator parses ``params`` against that model).

    ``visible_in`` is an opt-in filter — when set, the section is rendered
    only for the listed formats. ``None`` means "all formats this report
    declares".
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    block: str
    params: Dict[str, Any] = Field(default_factory=dict)
    title_th: Optional[str] = None
    visible_in: Optional[List[ReportFormat]] = None


class ReportDescriptor(BaseModel):
    """The single source of truth for a report — see ADR-03 §2.

    One YAML file at ``config/reports/<report_id>.yaml`` deserialises
    into one ``ReportDescriptor``. Cross-registry validation (every
    ``section.block`` resolves to a registered ``ContentBlock``) is the
    ``ReportRegistry``'s job, not this model's.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    report_id: str
    title_th: str
    title_en: Optional[str] = None
    description_th: Optional[str] = None
    description_en: Optional[str] = None
    formats: List[ReportFormat]
    languages: List[str] = Field(default_factory=lambda: ["th"])
    audience: List[ReportAudience] = Field(default_factory=lambda: cast("list[ReportAudience]", ["public"]))
    parameters: List[ParameterSpec] = Field(default_factory=list)
    sections: List[SectionSpec]
    style: StyleSpec = Field(default_factory=StyleSpec)
    cache: CacheSpec = Field(default_factory=CacheSpec)
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Mutable holders — dataclasses, not Pydantic models. They travel through
# the orchestrator and each block sees the SAME instance, so a frozen /
# validated model would get in the way (blocks may stash side data on
# ``extra``).
# ---------------------------------------------------------------------------


@dataclass
class RenderContext:
    """Per-render state passed to every block + renderer.

    ``data_collector`` is the ADR-03 §7 single-source-of-data handle. It
    is typed as ``Any`` here because ``ReportDataCollector`` is introduced
    in S4.2; once that lands, callers can rely on the ``TYPE_CHECKING``
    import for static analysis without dragging the symbol into runtime.

    ``recursion_depth`` tracks how deeply the orchestrator has descended
    into nested ``SectionSpec`` trees (S6 addendum to ADR-03). The
    top-level call is depth 0; container blocks like ``two_column_layout``
    re-enter the orchestrator at depth 1. Depth > 1 is rejected — the
    layout primitive is not reentrant. See ADR-03 "S6 addendum: container
    blocks" for the rationale.

    ``feature_flags`` (S9) is an arbitrary dict that backends populate
    from query-string flags. Today only ``polish_prose: bool`` is honoured
    — see :mod:`services.reports.polish`. Adding a new flag here does
    NOT require touching this dataclass; the field is intentionally a
    free-form dict so the surface stays stable.

    ``polish_service`` (S9) is the optional handle to a
    :class:`services.reports.polish.TextPolishService`. ``None`` means
    "no polish" (status quo). Wiring is the orchestrator's job — blocks
    must NOT instantiate one themselves (the SQLite cache file would be
    re-opened per call otherwise).
    """

    data_collector: Any
    lang: str
    fmt: str
    descriptor: ReportDescriptor
    requested_at: datetime
    extra: Dict[str, Any] = field(default_factory=dict)
    recursion_depth: int = 0
    feature_flags: Dict[str, Any] = field(default_factory=dict)
    polish_service: Optional[Any] = None


@dataclass
class RenderedSection:
    """One section after ``block.collect`` + ``block.render_<fmt>``.

    ``markup`` is the format-specific output: ``str`` for LaTeX/HTML, a
    ``dict`` for PPTX (per ADR-03 §4). ``data`` keeps the raw collected
    payload so renderers / tests / debug endpoints can inspect what the
    block computed independently of how it was rendered.
    """

    section_id: str
    block_id: str
    markup: Union[str, Dict[str, Any]]
    data: Dict[str, Any]
    params: BaseModel
