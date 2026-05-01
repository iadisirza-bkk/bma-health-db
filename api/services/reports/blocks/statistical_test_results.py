"""``statistical_test_results`` block — variable-shape inferential-stats table.

Per ADR-03 §3 this block ports the legacy whitepaper Section 4
(``Inferential Statistics``) which branches on ``test.test_type`` and
emits a different ``tabular`` shape for each kind of test
(odds ratio, logistic regression, correlation, chi-square, t-test).

Source contract
---------------
``ctx.data_collector.data()[source_path]`` is expected to be a list of
test rows. Each row is a dict with at least:

    * ``test_type``: one of the ``include_types`` literals
    * ``name``:      a short identifier used by ``params.tests`` filtering
    * ``p_value``:   float; missing rows default to NaN and never bold

Per-type extra fields (filled in as available — missing fields render as
``"—"`` and emit a warning, never crash):

    * ``odds_ratio``:           ``factor``, ``or_value``, ``ci_lower``,
                                ``ci_upper``
    * ``logistic_regression``:  ``predictor``, ``beta``, ``se``,
                                ``or_adjusted``
    * ``correlation``:          ``pair`` (or ``pair_a`` + ``pair_b``),
                                ``r``, ``n``
    * ``chi_square``:           ``pair``, ``chi2``, ``df``
    * ``t_test``:               ``comparison``, ``mean1``, ``mean2``, ``t``

A row with p-value below ``params.sig_threshold`` renders bold in LaTeX
(``\\textbf{...}`` per cell) and gains ``class="significant"`` in HTML.

Why a single block instead of one per test type
-----------------------------------------------
The legacy template's Section 4 walks ``data.inferential_tests`` once
and switches on ``test.test_type`` inside the loop — a single block is
the literal port of that. Splitting per type would force descriptors to
hand-roll five SectionSpec entries every time inferential stats run.

LaTeX colour notes — see ``trend_table.py`` for the ``okgreen`` /
``errred`` rationale; this block reuses the brand palette through
``\\textbf`` only (significance highlighting), no extra colours required.
"""
from __future__ import annotations

import logging
import math
from typing import Any, ClassVar, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.latex_utils import latex_escape
from services.reports.blocks.base import ContentBlock
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.statistical_test_results")


# Sentinel for missing cells. The legacy template uses an em-dash for
# the same purpose; sticking with U+2014 keeps the rendered table
# visually identical to the existing whitepaper output.
_MISSING = "—"


# Order in which test-type subsections are emitted. The legacy template
# rendered odds_ratio before logistic_regression for clinical reading
# flow; we preserve that and slot the rest in their natural order.
_TYPE_ORDER: tuple[str, ...] = (
    "odds_ratio",
    "logistic_regression",
    "correlation",
    "chi_square",
    "t_test",
)


# Per-test-type human-readable section titles (Thai). The legacy template
# left these to i18n keys; this block hard-codes Thai because every
# call site so far is Thai-only and the descriptor doesn't yet thread
# language into block rendering for sub-headings.
_TYPE_TITLE_TH: Dict[str, str] = {
    "odds_ratio": "Odds Ratio (OR) ของปัจจัยเสี่ยง",
    "logistic_regression": "การถดถอยโลจิสติก (Logistic Regression)",
    "correlation": "ค่าสหสัมพันธ์ (Correlation)",
    "chi_square": "การทดสอบไค-สแควร์ (Chi-Square)",
    "t_test": "การทดสอบที (t-Test)",
}


class StatTestParams(BaseModel):
    """Parameters for the ``statistical_test_results`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    source_path: str = "statistical_tests"
    """Dotted path into ``data_collector.data()``. Defaults match the
    canonical ``statistical_tests`` key the report data collector emits."""

    tests: Optional[List[str]] = None
    """Optional whitelist filter on the row's ``name`` field. ``None``
    means "no filter — keep every row"."""

    include_types: List[
        Literal[
            "odds_ratio",
            "logistic_regression",
            "correlation",
            "chi_square",
            "t_test",
        ]
    ] = Field(
        default_factory=lambda: [  # type: ignore[arg-type]  # mypy can't narrow lambda return to Literal
            "odds_ratio",
            "logistic_regression",
            "correlation",
        ]
    )
    """Which test types to render. Order is normalised to ``_TYPE_ORDER``
    on output regardless of caller input."""

    sig_threshold: float = 0.05
    """p-value cutoff for the ``\\textbf{...}`` / ``class="significant"``
    highlight. Strictly less-than: a row with p == 0.05 renders normal."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_dotted(data: Any, path: str) -> Any:
    """Walk a dotted ``path`` into ``data``; ``None`` if any segment misses.

    Same shape as ``trend_table._resolve_dotted`` — duplicated rather than
    imported because its semantics are tiny and pulling it across blocks
    would couple unrelated modules.
    """
    cur: Any = data
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif hasattr(cur, seg):
            cur = getattr(cur, seg)
        else:
            return None
    return cur


def _safe_float(raw: Any) -> Optional[float]:
    """Parse ``raw`` as float; return ``None`` on missing / unparseable."""
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(v):
        return None
    return v


def _fmt_num(raw: Any, *, places: int = 3) -> str:
    """Format a numeric cell at ``places`` decimals; ``_MISSING`` if absent."""
    v = _safe_float(raw)
    if v is None:
        return _MISSING
    return f"{v:.{places}f}"


def _fmt_p(raw: Any) -> str:
    """Format a p-value: ``< 0.001`` for very small, else 3 decimals."""
    v = _safe_float(raw)
    if v is None:
        return _MISSING
    if v < 0.001:
        return "< 0.001"
    return f"{v:.3f}"


def _fmt_int(raw: Any) -> str:
    """Format an integer-style cell (n, df). Falls back to string repr."""
    v = _safe_float(raw)
    if v is None:
        return _MISSING
    return f"{int(round(v))}"


def _fmt_ci(lower: Any, upper: Any) -> str:
    """Format a 95% CI as ``(lo, hi)``; ``_MISSING`` if either side absent."""
    lo = _safe_float(lower)
    hi = _safe_float(upper)
    if lo is None or hi is None:
        return _MISSING
    return f"({lo:.2f}, {hi:.2f})"


def _is_significant(row: Dict[str, Any], threshold: float) -> bool:
    """True iff the row's p-value is strictly below ``threshold``."""
    p = _safe_float(row.get("p_value"))
    if p is None:
        return False
    return p < threshold


def _warn_missing(test_type: str, key: str, row_name: str) -> None:
    """Log once per (test_type, key) — never crash on missing fields."""
    logger.warning(
        "statistical_test_results: %s row %r missing field %r — using %s",
        test_type,
        row_name,
        key,
        _MISSING,
    )


def _row_cells_for_type(
    test_type: str, row: Dict[str, Any]
) -> List[str]:
    """Project a row into its type-specific column tuple.

    Returns formatted **string cells** so render_latex / render_html only
    need to bold / wrap; numeric formatting decisions live here in one
    place. Missing fields surface as ``_MISSING`` and emit a warning.
    """
    name = str(row.get("name", row.get("factor", row.get("predictor", "?"))))

    if test_type == "odds_ratio":
        cells: List[str] = []
        factor = row.get("factor")
        if factor is None:
            _warn_missing(test_type, "factor", name)
            cells.append(_MISSING)
        else:
            cells.append(str(factor))
        cells.append(_fmt_num(row.get("or_value"), places=2))
        cells.append(_fmt_ci(row.get("ci_lower"), row.get("ci_upper")))
        cells.append(_fmt_p(row.get("p_value")))
        cells.append("✓" if _is_significant(row, 0.05) else "")
        return cells

    if test_type == "logistic_regression":
        cells = []
        predictor = row.get("predictor")
        if predictor is None:
            _warn_missing(test_type, "predictor", name)
            cells.append(_MISSING)
        else:
            cells.append(str(predictor))
        cells.append(_fmt_num(row.get("beta"), places=3))
        cells.append(_fmt_num(row.get("se"), places=3))
        cells.append(_fmt_p(row.get("p_value")))
        cells.append(_fmt_num(row.get("or_adjusted"), places=2))
        return cells

    if test_type == "correlation":
        cells = []
        # The legacy collector emits ``pair`` as a single string OR a
        # ``pair_a`` / ``pair_b`` pair — accept either.
        pair = row.get("pair")
        if pair is None:
            a, b = row.get("pair_a"), row.get("pair_b")
            if a is not None and b is not None:
                pair = f"{a} ↔ {b}"
        if pair is None:
            _warn_missing(test_type, "pair", name)
            cells.append(_MISSING)
        else:
            cells.append(str(pair))
        cells.append(_fmt_num(row.get("r"), places=3))
        cells.append(_fmt_p(row.get("p_value")))
        cells.append(_fmt_int(row.get("n")))
        return cells

    if test_type == "chi_square":
        cells = []
        pair = row.get("pair")
        if pair is None:
            _warn_missing(test_type, "pair", name)
            cells.append(_MISSING)
        else:
            cells.append(str(pair))
        cells.append(_fmt_num(row.get("chi2"), places=3))
        cells.append(_fmt_int(row.get("df")))
        cells.append(_fmt_p(row.get("p_value")))
        return cells

    if test_type == "t_test":
        cells = []
        comparison = row.get("comparison")
        if comparison is None:
            _warn_missing(test_type, "comparison", name)
            cells.append(_MISSING)
        else:
            cells.append(str(comparison))
        cells.append(_fmt_num(row.get("mean1"), places=2))
        cells.append(_fmt_num(row.get("mean2"), places=2))
        cells.append(_fmt_num(row.get("t"), places=3))
        cells.append(_fmt_p(row.get("p_value")))
        return cells

    # Defensive — should never reach here because the param model
    # constrains test_type to the literal set.
    return [_MISSING]  # pragma: no cover


# Header tuples per type, in the same order as the cells produced
# above. LaTeX uses ``$\beta$`` for the logistic-regression slope and
# ``$\chi^2$`` for chi-square; HTML uses Unicode equivalents so the
# rendered HTML is self-contained (no MathJax required).
_LATEX_HEADERS: Dict[str, List[str]] = {
    "odds_ratio": ["factor", "OR", r"95\% CI", "p-value", "sig"],
    "logistic_regression": [
        "predictor",
        r"$\beta$",
        "SE",
        "p",
        "OR(adjusted)",
    ],
    "correlation": ["pair", "r", "p", "n"],
    "chi_square": ["pair", r"$\chi^2$", "df", "p"],
    "t_test": ["comparison", "mean1", "mean2", "t", "p"],
}


_HTML_HEADERS: Dict[str, List[str]] = {
    "odds_ratio": ["factor", "OR", "95% CI", "p-value", "sig"],
    "logistic_regression": [
        "predictor",
        "β",
        "SE",
        "p",
        "OR(adjusted)",
    ],
    "correlation": ["pair", "r", "p", "n"],
    "chi_square": ["pair", "χ²", "df", "p"],
    "t_test": ["comparison", "mean1", "mean2", "t", "p"],
}


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


class StatisticalTestResultsBlock(ContentBlock):
    """Variable-shape inferential-statistics table block (ADR-03 §3)."""

    block_id: ClassVar[str] = "statistical_test_results"
    Parameters: ClassVar[type[BaseModel]] = StatTestParams

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, StatTestParams)
        getter = getattr(ctx.data_collector, "data", None)
        bag: Any = getter() if callable(getter) else (getter or {})
        raw = _resolve_dotted(bag, params.source_path)

        # Tolerate missing source — return the empty bucket map so
        # render_* can short-circuit cleanly.
        if not isinstance(raw, list):
            if raw is not None:
                logger.warning(
                    "statistical_test_results: source %r is %s, expected list",
                    params.source_path,
                    type(raw).__name__,
                )
            return {"by_type": {}}

        # Normalise rows to plain dicts (Pydantic models / dataclasses
        # both quack via ``model_dump`` / ``vars``).
        rows: List[Dict[str, Any]] = []
        for r in raw:
            if isinstance(r, dict):
                rows.append(dict(r))
            elif hasattr(r, "model_dump"):
                rows.append(r.model_dump())  # pragma: no cover
            else:
                rows.append(dict(vars(r)))  # pragma: no cover

        # Apply name filter first — cheaper than the type filter when
        # the caller wants a specific test by name.
        if params.tests is not None:
            wanted = set(params.tests)
            rows = [r for r in rows if r.get("name") in wanted]

        # Bucket by test_type, dropping types not in include_types.
        wanted_types = set(params.include_types)
        by_type: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            tt = r.get("test_type")
            if not isinstance(tt, str) or tt not in wanted_types:
                continue
            by_type.setdefault(tt, []).append(r)

        # Sort rows within each bucket by p-value ascending so the most
        # significant row is at the top. NaN p-values sort to the end.
        def _pkey(row: Dict[str, Any]) -> float:
            p = _safe_float(row.get("p_value"))
            return float("inf") if p is None else p

        for tt in by_type:
            by_type[tt].sort(key=_pkey)

        return {"by_type": by_type, "sig_threshold": params.sig_threshold}

    # ------------------------------------------------------------------
    # LaTeX
    # ------------------------------------------------------------------

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        assert isinstance(params, StatTestParams)
        by_type: Dict[str, List[Dict[str, Any]]] = data.get("by_type", {})
        threshold = float(data.get("sig_threshold", params.sig_threshold))
        if not by_type:
            return r"\textit{ไม่มีผลการทดสอบทางสถิติในรายงานนี้}" + "\n"

        out: List[str] = []
        for test_type in _TYPE_ORDER:
            rows = by_type.get(test_type)
            if not rows:
                continue
            title = _TYPE_TITLE_TH.get(test_type, test_type)
            # Subsection header — escape the Thai title (it can't contain
            # LaTeX specials in our hard-coded map but ``latex_escape`` is
            # cheap insurance for future translation tweaks).
            out.append(r"\subsection*{" + latex_escape(title) + "}")
            headers = _LATEX_HEADERS[test_type]
            ncols = len(headers)
            # All non-first columns right-align; first column is the
            # qualitative label so left-align it.
            col_spec = "|l|" + "r|" * (ncols - 1)
            head_line = " & ".join(r"\textbf{" + h + "}" for h in headers)
            body_lines: List[str] = []
            for row in rows:
                cells = _row_cells_for_type(test_type, row)
                # Pad / truncate defensively — _row_cells_for_type
                # always returns the right count, but a future test type
                # could drift.
                while len(cells) < ncols:
                    cells.append(_MISSING)
                cells = cells[:ncols]
                escaped = [latex_escape(c) for c in cells]
                if _is_significant(row, threshold):
                    escaped = [r"\textbf{" + c + "}" for c in escaped]
                body_lines.append(" & ".join(escaped) + r" \\ \hline")
            body = "\n".join(body_lines)
            out.append(
                r"\begin{tabular}{" + col_spec + "}"
            )
            out.append(r"\hline")
            out.append(head_line + r" \\ \hline")
            if body:
                out.append(body)
            out.append(r"\end{tabular}")
            out.append("")  # blank line between subsections
        return "\n".join(out) + "\n"

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        assert isinstance(params, StatTestParams)
        by_type: Dict[str, List[Dict[str, Any]]] = data.get("by_type", {})
        threshold = float(data.get("sig_threshold", params.sig_threshold))
        if not by_type:
            return (
                '<p class="stat-test-empty"><em>'
                "ไม่มีผลการทดสอบทางสถิติในรายงานนี้</em></p>"
            )

        parts: List[str] = []
        for test_type in _TYPE_ORDER:
            rows = by_type.get(test_type)
            if not rows:
                continue
            title = _TYPE_TITLE_TH.get(test_type, test_type)
            parts.append(f"<h3>{_html_escape(title)}</h3>")
            headers = _HTML_HEADERS[test_type]
            ncols = len(headers)
            thead_cells = "".join(
                f"<th>{_html_escape(h)}</th>" for h in headers
            )
            thead = f"<thead><tr>{thead_cells}</tr></thead>"
            body_rows: List[str] = []
            for row in rows:
                cells = _row_cells_for_type(test_type, row)
                while len(cells) < ncols:
                    cells.append(_MISSING)
                cells = cells[:ncols]
                tds = "".join(
                    f"<td>{_html_escape(c)}</td>" for c in cells
                )
                cls = ' class="significant"' if _is_significant(row, threshold) else ""
                body_rows.append(f"<tr{cls}>{tds}</tr>")
            tbody = "<tbody>" + "".join(body_rows) + "</tbody>"
            parts.append(
                f'<table class="stat-test stat-{test_type}">'
                f"{thead}{tbody}</table>"
            )
        return "\n".join(parts)
