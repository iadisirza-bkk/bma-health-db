"""``disease_district_grid`` block — per-disease × per-district breakdown table.

Per ADR-03 §3, this block ports Section 2 of the legacy whitepaper
template (``api/templates/latex/report_whitepaper.tex.j2`` lines 133-154):
one ``\\subsection*{...}`` heading + one ``\\begin{longtable}`` per
disease, each table listing every district's risk + found counts and
percentages for that disease.

The block runs ``MVRepository.run_query("district_disease_counts", {})``
ONCE — even when multiple instances of the block appear in a report (e.g.
one per audience) the result is cached on ``ctx.extra`` so the k-anon
layer is not re-traversed. Each disease is then projected from that one
row set into a ``(district_code, district_name, risk_count, risk_pct,
found_count, found_pct)`` view.

Notes on column-name translation
--------------------------------
The MV ``summary_district_disease`` exposes wide columns
(``risk_dm_count``, ``pct_risk_dm``, ``found_dm_count``, ``pct_found_dm``,
…). We map them to the per-disease ``(risk_count, risk_pct, found_count,
found_pct)`` shape using the ``_DISEASE_COLS`` table below. Diseases
whose corresponding columns are NOT present in the MV (e.g. ``ckd``,
``mental``) are reported as ``None`` cells and rendered as ``"—"`` —
this matches the legacy template's behaviour of showing dashes when the
column wasn't available, rather than crashing.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.reports.blocks.base import ContentBlock
from services.reports.renderers._filters import number_format, pct as pct_fmt, pct2 as pct2_fmt
from services.reports.renderers._latex_filters import latex_safe
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.disease_district_grid")


# ---------------------------------------------------------------------------
# Disease label + column map. ``(risk_count, risk_pct, found_count, found_pct)``
# tuples are MV column names; ``None`` means "not in the MV — render as '—'".
#
# The MV ``summary_district_disease`` does not expose a ``district_name``
# column (the FK is ``district_code``); see ``DistrictDiseaseRow`` in
# ``api/repositories/rows.py``. The ``district_name`` value below is
# resolved via ``ctx.data_collector.data()["district_data"]`` if present
# (legacy whitepaper code path), otherwise falls back to the code itself.
# ---------------------------------------------------------------------------

_DISEASE_LABEL_TH: Dict[str, str] = {
    "dm": "เบาหวาน",
    "hpt": "ความดัน",
    "cvd": "หัวใจ-หลอดเลือด",
    "stroke": "หลอดเลือดสมอง",
    "obesity": "อ้วน",
    "dyslipidemia": "ไขมัน",
    "ckd": "ไต",
    "mental": "สุขภาพจิต",
}


# Column tuple shape: (risk_count, risk_pct, found_count, found_pct).
# ``None`` entries map to "—" cells (MV doesn't expose them).
_DISEASE_COLS: Dict[str, tuple[Optional[str], Optional[str], Optional[str], Optional[str]]] = {
    "dm": ("risk_dm_count", "pct_risk_dm", "found_dm_count", "pct_found_dm"),
    "hpt": ("risk_hpt_count", "pct_risk_hpt", "found_hpt_count", "pct_found_hpt"),
    "cvd": ("risk_cvd_count", "pct_risk_cvd", "found_cvd_count", "pct_found_cvd"),
    "stroke": ("risk_stroke_count", None, "found_stroke_count", None),
    # ``risk_bmi_count`` is the closest analogue for "obesity at-risk".
    "obesity": ("risk_bmi_count", None, "found_obesity_count", None),
    "dyslipidemia": (None, None, "found_dyslipidemia_count", None),
    "ckd": (None, None, None, None),
    "mental": (None, None, None, None),
}

# Allow-list for the ``metrics`` field of params. Centralised so the
# Pydantic Literal stays in sync with the renderers below.
_METRIC_KEYS = ("risk_count", "risk_pct", "found_count", "found_pct")
_METRIC_HEADER_TH: Dict[str, str] = {
    "risk_count": "จำนวนเสี่ยง",
    "risk_pct": "%เสี่ยง",
    "found_count": "จำนวนพบโรค",
    "found_pct": "%พบโรค",
}


class DiseaseDistrictGridParams(BaseModel):
    """Parameters for the ``disease_district_grid`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    diseases: List[str] = Field(
        default_factory=lambda: [
            "dm",
            "hpt",
            "cvd",
            "stroke",
            "obesity",
            "dyslipidemia",
            "ckd",
            "mental",
        ]
    )
    metrics: List[Literal["risk_count", "risk_pct", "found_count", "found_pct"]] = Field(
        default_factory=lambda: ["risk_count", "risk_pct", "found_count", "found_pct"]  # type: ignore[arg-type]  # mypy can't narrow lambda return to Literal
    )
    max_districts_per_disease: int = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _resolve_repo(ctx: RenderContext) -> Any:
    """Return an object exposing ``async run_query(query_id, params)``.

    Honors a context-injected mock first (tests), then the global
    ``MVRepository``.
    """
    pre = ctx.extra.get("mv_repository") if ctx.extra else None
    if pre is not None:
        return pre
    from repositories.mv_repository import MVRepository
    return MVRepository()


def _row_to_dict(row: Any) -> Dict[str, Any]:
    """Coerce a Pydantic row / dict / vars()-able object to a plain dict."""
    if hasattr(row, "model_dump"):
        dumped: Dict[str, Any] = row.model_dump()
        return dumped
    if isinstance(row, dict):
        return dict(row)
    return dict(vars(row))  # pragma: no cover — defensive


def _district_name(
    ctx: RenderContext, district_code: str
) -> str:
    """Look up the Thai district name for a given code via the data collector.

    Falls back to the bare code if the data collector doesn't expose a
    ``district_data`` map (e.g. unit tests with a thin fake collector).
    """
    getter = getattr(ctx.data_collector, "data", None)
    if not callable(getter):
        return district_code
    try:
        bag = getter()
    except Exception:
        return district_code
    district_data = bag.get("district_data") if isinstance(bag, dict) else None
    if not isinstance(district_data, dict):
        return district_code
    entry = district_data.get(district_code)
    if isinstance(entry, dict):
        # ``data_adapter._fetch_district_summary`` exposes both ``name_th``
        # and ``district_name``; tolerate either.
        for key in ("name_th", "district_name", "name"):
            v = entry.get(key)
            if v:
                return str(v)
    return district_code


async def _cached_district_rows(
    ctx: RenderContext,
) -> List[Dict[str, Any]]:
    """Run ``district_disease_counts`` once per render, cache on ``ctx.extra``.

    Multiple instances of ``DiseaseDistrictGridBlock`` (or other blocks
    with the same query) reuse the cached row set rather than re-running
    the SQL — same idiom as the legacy whitepaper data collector.
    """
    cache_key = "__district_disease_cache"
    if ctx.extra is None:
        # ``RenderContext.extra`` defaults to ``{}`` via dataclass field —
        # this branch is purely defensive for older test harnesses.
        return await _run_once(ctx)  # type: ignore[unreachable]
    cached = ctx.extra.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    rows = await _run_once(ctx)
    ctx.extra[cache_key] = rows
    return rows


async def _run_once(ctx: RenderContext) -> List[Dict[str, Any]]:
    repo = _resolve_repo(ctx)
    raw = await repo.run_query("district_disease_counts", {})
    return [_row_to_dict(r) for r in raw]


def _project_for_disease(
    rows: List[Dict[str, Any]],
    disease_key: str,
    ctx: RenderContext,
    max_rows: int,
) -> List[Dict[str, Any]]:
    """Project the wide MV rows into the per-disease (district × metrics) view.

    The MV is keyed by ``(data_source, district_code)``. Multiple data
    sources can produce two rows for the same district; we sum the
    counts and weight-average the percentages so a district appears
    exactly once per disease in the rendered table.
    """
    cols = _DISEASE_COLS.get(disease_key, (None, None, None, None))
    risk_col, risk_pct_col, found_col, found_pct_col = cols

    # First pass: aggregate across data_source per district.
    by_dist: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        code = str(r.get("district_code", ""))
        if not code:
            continue
        agg = by_dist.setdefault(
            code,
            {
                "district_code": code,
                "total_screened": 0,
                "risk_count_acc": 0,
                "found_count_acc": 0,
                "risk_pct_weighted": 0.0,
                "risk_pct_weight": 0,
                "found_pct_weighted": 0.0,
                "found_pct_weight": 0,
            },
        )
        ts = int(r.get("total_screened") or 0)
        agg["total_screened"] += ts
        if risk_col is not None:
            agg["risk_count_acc"] += int(r.get(risk_col) or 0)
        if found_col is not None:
            agg["found_count_acc"] += int(r.get(found_col) or 0)
        if risk_pct_col is not None:
            v = r.get(risk_pct_col)
            if v is not None and ts > 0:
                agg["risk_pct_weighted"] += float(v) * ts
                agg["risk_pct_weight"] += ts
        if found_pct_col is not None:
            v = r.get(found_pct_col)
            if v is not None and ts > 0:
                agg["found_pct_weighted"] += float(v) * ts
                agg["found_pct_weight"] += ts

    # Second pass: produce display rows in the canonical
    # ``(risk_count, risk_pct, found_count, found_pct)`` order.
    out: List[Dict[str, Any]] = []
    for code, agg in by_dist.items():
        out.append(
            {
                "district_code": code,
                "district_name": _district_name(ctx, code),
                "risk_count": agg["risk_count_acc"] if risk_col else None,
                "risk_pct": (
                    agg["risk_pct_weighted"] / agg["risk_pct_weight"]
                    if agg["risk_pct_weight"] > 0
                    else None
                ),
                "found_count": agg["found_count_acc"] if found_col else None,
                "found_pct": (
                    agg["found_pct_weighted"] / agg["found_pct_weight"]
                    if agg["found_pct_weight"] > 0
                    else None
                ),
            }
        )
    # Stable ordering: by district_code so the same row order shows in
    # both LaTeX + HTML.
    out.sort(key=lambda d: d["district_code"])
    if max_rows > 0:
        out = out[:max_rows]
    return out


def _format_metric(raw: Any, metric: str) -> str:
    """Format one metric cell per its ``metric`` kind."""
    if raw is None:
        return "—"
    if metric in ("risk_count", "found_count"):
        try:
            return number_format(int(raw))
        except (TypeError, ValueError):  # pragma: no cover
            return str(raw)
    if metric in ("risk_pct", "found_pct"):
        try:
            return f"{float(raw):.1f}%"
        except (TypeError, ValueError):  # pragma: no cover
            return str(raw)
    return str(raw)  # pragma: no cover — Literal validates metric


# Suppress linter complaint about unused import in some checkers — we
# expose ``pct_fmt`` / ``pct2_fmt`` for any future per-metric override.
_ = (pct_fmt, pct2_fmt)


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


class DiseaseDistrictGridBlock(ContentBlock):
    """One ``longtable`` per disease — replaces Section 2 of the whitepaper."""

    block_id: ClassVar[str] = "disease_district_grid"
    Parameters: ClassVar[type[BaseModel]] = DiseaseDistrictGridParams

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, DiseaseDistrictGridParams)
        rows = await _cached_district_rows(ctx)
        tables: List[Dict[str, Any]] = []
        for disease_key in params.diseases:
            label_th = _DISEASE_LABEL_TH.get(disease_key, disease_key)
            projected = _project_for_disease(
                rows, disease_key, ctx, params.max_districts_per_disease
            )
            tables.append(
                {
                    "disease_key": disease_key,
                    "disease_label_th": label_th,
                    "rows": projected,
                }
            )
        return {"tables": tables, "metrics": list(params.metrics)}

    # ------------------------------------------------------------------
    # LaTeX — one ``longtable`` per disease, with a ``\\subsection*{}`` heading.
    # ------------------------------------------------------------------

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        tables: List[Dict[str, Any]] = data.get("tables", [])
        metrics: List[str] = data.get("metrics", list(_METRIC_KEYS))
        if not tables:
            return ""
        out: List[str] = []
        # ``l`` for the district name + N right-aligned numeric columns.
        col_spec = "l" + "r" * len(metrics)
        # S7 carryover (S8 fix): the ``%`` in headers like ``%เสี่ยง`` /
        # ``%พบโรค`` is LaTeX's comment marker and silently swallowed
        # the closing brace of ``\textbf{...}``, producing an unterminated
        # ``\textbf`` and a "File ended while scanning use of \textbf"
        # compile error. Run every header through ``latex_safe`` so the
        # ``%`` (and any other future hot character) is properly escaped.
        header_cells = [r"\textbf{เขต}"] + [
            r"\textbf{" + latex_safe(_METRIC_HEADER_TH.get(m, m)) + "}"
            for m in metrics
        ]
        header_line = " & ".join(header_cells) + r" \\"
        for tbl in tables:
            out.append(
                r"\subsection*{" + latex_safe(tbl["disease_label_th"]) + "}"
            )
            out.append(r"\begin{longtable}{" + col_spec + "}")
            out.append(r"\toprule")
            out.append(header_line)
            out.append(r"\midrule")
            out.append(r"\endhead")
            rows: List[Dict[str, Any]] = tbl.get("rows", [])
            if not rows:
                # Empty-state row so the longtable still compiles.
                empty_cells = [r"\textit{(ไม่มีข้อมูล)}"] + [
                    "—" for _ in metrics
                ]
                out.append(" & ".join(empty_cells) + r" \\")
            else:
                for r in rows:
                    cells = [latex_safe(r["district_name"])]
                    for m in metrics:
                        # ``_format_metric`` returns strings like "23.0%"
                        # for pct metrics; the raw ``%`` is a LaTeX
                        # comment marker and would silently swallow the
                        # rest of the line. Run every cell through
                        # ``latex_safe`` so ``%`` (and any future hot
                        # char) is properly escaped.
                        cells.append(latex_safe(_format_metric(r.get(m), m)))
                    out.append(" & ".join(cells) + r" \\")
            out.append(r"\bottomrule")
            out.append(r"\end{longtable}")
            out.append("")
        return "\n".join(out) + "\n"

    # ------------------------------------------------------------------
    # HTML — one ``<table class="disease-grid">`` per disease, separated
    # by ``<hr>`` so they read as a stack.
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        tables: List[Dict[str, Any]] = data.get("tables", [])
        metrics: List[str] = data.get("metrics", list(_METRIC_KEYS))
        if not tables:
            return ""
        parts: List[str] = []
        for i, tbl in enumerate(tables):
            if i > 0:
                parts.append("<hr>")
            parts.append(
                f"<h3>{_html_escape(str(tbl['disease_label_th']))}</h3>"
            )
            head_cells = ["<th>เขต</th>"] + [
                f"<th>{_html_escape(_METRIC_HEADER_TH.get(m, m))}</th>"
                for m in metrics
            ]
            thead = "<thead><tr>" + "".join(head_cells) + "</tr></thead>"
            rows = tbl.get("rows", [])
            body_rows: List[str] = []
            if not rows:
                empty = (
                    "<td><em>(ไม่มีข้อมูล)</em></td>"
                    + "<td>—</td>" * len(metrics)
                )
                body_rows.append(f"<tr>{empty}</tr>")
            else:
                for r in rows:
                    cells = [
                        f"<td>{_html_escape(str(r['district_name']))}</td>"
                    ]
                    for m in metrics:
                        cells.append(
                            f"<td>{_html_escape(_format_metric(r.get(m), m))}</td>"
                        )
                    body_rows.append("<tr>" + "".join(cells) + "</tr>")
            tbody = "<tbody>" + "".join(body_rows) + "</tbody>"
            parts.append(
                f'<table class="disease-grid">{thead}{tbody}</table>'
            )
        return "".join(parts)
