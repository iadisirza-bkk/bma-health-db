"""``logistic_regression`` block — fit a binary GLM and visualise its ORs.

Per Sprint S11 ("PhD-grade Whitepaper") this is the inferential workhorse
of the academic results section. The block:

1. Pulls a flat list of rows via ``ctx.data_collector`` (the legacy report
   data shape, plus an optional ``fetch(spec_id, filters)`` adapter).
2. Builds an ``X`` matrix from ``params.predictors`` and a ``y`` vector
   from the column named by ``params.outcome_spec_id`` — both expected
   to be present on each row dict.
3. Fits ``statsmodels.api.Logit(y, X).fit(disp=False)`` (or ``Probit`` if
   ``family == "probit"``).
4. Translates the fitted coefficients into a ``forest_plot``-shaped row
   list:

       label = predictor name
       estimate = exp(β)            # odds ratio
       ci_lo, ci_hi = exp(β ± 1.96·SE)
       p_value = z-test p-value

5. Renders a TWO-PANEL output:
       LEFT/TOP: forest plot of the OR + 95% CI (delegated to
                 :func:`render_forest_to_png`).
       RIGHT/BOTTOM: a small text table (n, n_events, AUC,
                     McFadden R²).

If statsmodels fails to converge (perfect separation, singular matrix,
NaN-only column), the block returns a graceful row list with NaN
estimates and a warning string instead of crashing — researchers
investigating a bad covariate should see "this didn't fit" prose, not
a stack trace.

Audience: ``RESEARCHER`` — see ForestPlotBlock for the rationale.

data_collector contract
-----------------------
The brief permits ``ctx.data_collector.fetch(spec_id, filters)``. Today
the canonical collector exposes ``data()`` (a dict) instead of ``fetch``.
We try both — ``fetch`` first, then a dotted-path lookup into the
``data()`` dict — so this block works against the legacy shape without
forcing Agent A to reshape the collector. If the collector returns
neither a list nor an object with ``rows`` we log a warning and treat
the call as empty — same convention as ``statistical_test_results``.
"""
from __future__ import annotations

import html
import logging
import math
from typing import Any, ClassVar, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from services.latex_utils import latex_escape
from services.reports.blocks.base import AudienceTarget, ContentBlock
from services.reports.blocks.forest_plot import (
    BMA_GREEN,
    RISK_RED,
    render_forest_to_png,
)
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.logistic_regression")


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


class _LogisticRegressionParams(BaseModel):
    """Parameters for the ``logistic_regression`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    outcome_spec_id: str
    """Either:
       * the data_collector spec_id to fetch (preferred, when the
         collector exposes ``fetch``); OR
       * the column name on each row whose value is the binary outcome
         (used as the ``y`` vector).

    Both interpretations agree when the collector is the legacy shape:
    ``data()`` returns a dict, ``data()[outcome_spec_id]`` is a list of
    flat row dicts, and each row already has a column with the same
    name carrying the 0/1 outcome.
    """

    predictors: List[str] = Field(default_factory=list)
    """Column names to use as predictors. The order is preserved in the
    forest plot (subject to ``forest_plot``'s own sort)."""

    stratify_by: Optional[str] = None
    """Optional stratifier — if set, the block fits ONE model per
    distinct stratum value and emits a separate forest plot for each.
    Useful e.g. for "fit a model per zone". The stratum value goes into
    the forest plot title/caption."""

    filters: Dict[str, Any] = Field(default_factory=dict)
    """Forwarded verbatim to ``data_collector.fetch`` if available."""

    family: Literal["logit", "probit"] = "logit"
    """Link function. Logit is the default (most papers report ORs);
    probit is provided for the rare case where the reviewer asks for
    it. Only ``logit`` returns interpretable ORs — for probit the
    forest plot reports raw β coefficients with the metric_name
    overridden accordingly."""

    caption_th: Optional[str] = None
    """Optional Thai caption shown under the figure."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(value: Any) -> Optional[float]:
    """Parse ``value`` as a finite float; return None if missing / NaN / inf."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _resolve_collector_rows(
    ctx: RenderContext,
    outcome_spec_id: str,
    filters: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], bool]:
    """Pull rows from the data collector, trying both shapes.

    Returns ``(rows, fetch_used)``. ``fetch_used`` is True when the
    collector exposed ``fetch(spec_id, filters)`` and returned a real
    list — in that case the caller skips post-filtering, since
    ``fetch`` is presumed to have honoured the filters server-side.

    Order of attempts:
        1. ``ctx.data_collector.fetch(outcome_spec_id, filters)`` — the
           shape the brief assumes; used when the collector exposes a
           direct fetch entrypoint.
        2. ``ctx.data_collector.data()[outcome_spec_id]`` — the legacy
           dict-bag shape (used by every block that shipped before S11).
        3. ``getattr(ctx.data_collector, outcome_spec_id)`` — for
           ReportData-style attribute access.

    Returns ``([], False)`` when none yield a list of dicts.
    """
    collector = ctx.data_collector
    if collector is None:
        return [], False

    # 1. fetch(spec_id, filters) — sync only.
    fetcher = getattr(collector, "fetch", None)
    if callable(fetcher):
        try:
            result = fetcher(outcome_spec_id, filters)
            # If the call is async, the orchestrator's ``collect`` is also
            # async — but we're already inside that coroutine, so we'd
            # need to ``await``. To keep this helper sync (the rest of the
            # codebase treats data_collector as sync) we ONLY honour
            # ``fetch`` returns that are non-coroutine. This is the
            # documented contract for the brief.
            if hasattr(result, "rows"):
                rows = result.rows
            elif isinstance(result, dict) and "rows" in result:
                rows = result["rows"]
            else:
                rows = result
            if isinstance(rows, list):
                return (
                    [dict(r) if not isinstance(r, dict) else r for r in rows],
                    True,
                )
            logger.warning(
                "logistic_regression: fetch returned %s, expected list",
                type(rows).__name__,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "logistic_regression: fetch(%r) raised %s — falling back",
                outcome_spec_id, exc,
            )

    # 2. data() dict-bag.
    getter = getattr(collector, "data", None)
    if callable(getter):
        try:
            bag = getter()
        except Exception:  # pragma: no cover — defensive
            bag = None
        if isinstance(bag, dict) and outcome_spec_id in bag:
            raw = bag[outcome_spec_id]
            if isinstance(raw, list):
                return (
                    [dict(r) if not isinstance(r, dict) else r for r in raw],
                    False,
                )

    # 3. attribute access.
    if hasattr(collector, outcome_spec_id):
        raw = getattr(collector, outcome_spec_id)
        if isinstance(raw, list):
            return (
                [dict(r) if not isinstance(r, dict) else r for r in raw],
                False,
            )

    return [], False


def _build_design_matrix(
    rows: List[Dict[str, Any]],
    outcome_col: str,
    predictors: List[str],
) -> Tuple["Any", "Any", List[str], int]:
    """Drop incomplete rows; return (y, X, kept_predictor_names, n_dropped).

    * y is a numpy 1-D float array of 0/1 values.
    * X is a numpy 2-D array WITHOUT a constant column (we add it later
      via :func:`statsmodels.add_constant`).
    * ``kept_predictor_names`` is identical to ``predictors`` unless we
      had to drop a column with zero variance — those drop out so the
      Logit fit doesn't choke on a singular matrix.

    Rows with any missing predictor or missing outcome are filtered;
    ``n_dropped`` is the count for diagnostics.
    """
    import numpy as np

    n_input = len(rows)
    if not rows or not predictors:
        return np.zeros(0), np.zeros((0, max(1, len(predictors)))), list(predictors), n_input

    valid_rows: List[Dict[str, Any]] = []
    for row in rows:
        y_val = _safe_float(row.get(outcome_col))
        if y_val is None:
            continue
        # Predictors must all parse.
        ok = True
        for p in predictors:
            if _safe_float(row.get(p)) is None:
                ok = False
                break
        if ok:
            valid_rows.append(row)

    if not valid_rows:
        return (
            np.zeros(0),
            np.zeros((0, max(1, len(predictors)))),
            list(predictors),
            n_input,
        )

    y = np.array(
        [float(_safe_float(r.get(outcome_col)) or 0.0) for r in valid_rows]
    )
    X = np.array(
        [
            [float(_safe_float(r.get(p)) or 0.0) for p in predictors]
            for r in valid_rows
        ]
    )

    # Drop zero-variance predictors — Logit will otherwise hit a
    # singular Hessian. Track which ones survived for the forest plot.
    keep_mask = np.array([X[:, i].std() > 1e-12 for i in range(X.shape[1])])
    if not keep_mask.all():
        dropped = [predictors[i] for i in range(len(predictors)) if not keep_mask[i]]
        logger.warning(
            "logistic_regression: dropping zero-variance predictors %s",
            dropped,
        )
        X = X[:, keep_mask]
        kept = [predictors[i] for i in range(len(predictors)) if keep_mask[i]]
    else:
        kept = list(predictors)

    return y, X, kept, n_input - len(valid_rows)


def _fit_glm(
    y: "Any",
    X: "Any",
    family: str,
) -> Optional[Any]:
    """Fit a Logit / Probit. Returns the result wrapper or None if
    convergence fails for any reason. Never raises — researchers want
    a graceful "did not converge" row, not a traceback."""
    if y.size == 0 or X.size == 0:
        return None
    try:
        import statsmodels.api as sm

        Xc = sm.add_constant(X, has_constant="add")
        model_cls = sm.Logit if family == "logit" else sm.Probit
        model = model_cls(y, Xc)
        result = model.fit(disp=False, method="newton", maxiter=50)
        # statsmodels sets ``mle_retvals['converged']`` if Newton ran.
        retvals = getattr(result, "mle_retvals", {}) or {}
        if not retvals.get("converged", True):
            logger.warning(
                "logistic_regression: %s did not converge (retvals=%s)",
                family, retvals,
            )
            return None
        return result
    except Exception as exc:
        logger.warning(
            "logistic_regression: %s fit failed (%s)",
            family, exc,
        )
        return None


def _result_to_rows(
    result: Any,
    predictor_names: List[str],
    family: str,
) -> List[Dict[str, Any]]:
    """Convert the fitted model into forest_plot row dicts.

    For ``logit`` the OR is ``exp(β)``; CI is ``exp(β ± z·SE)``.
    For ``probit`` we report the raw β + linear CI — no exponentiation.
    """
    import numpy as np

    if result is None:
        return [
            {
                "label": p,
                "estimate": float("nan"),
                "ci_lo": float("nan"),
                "ci_hi": float("nan"),
                "p_value": None,
                "warning": "did not converge",
            }
            for p in predictor_names
        ]

    params = np.asarray(result.params)
    pvals = np.asarray(result.pvalues)
    try:
        ci = np.asarray(result.conf_int(alpha=0.05))
    except Exception:  # pragma: no cover — defensive
        bse = np.asarray(getattr(result, "bse", np.zeros_like(params)))
        z = 1.959963984540054
        ci = np.column_stack([params - z * bse, params + z * bse])

    rows: List[Dict[str, Any]] = []
    # Skip the constant (always at index 0 because we added it via
    # ``sm.add_constant``).
    for i, name in enumerate(predictor_names):
        idx = i + 1
        b = float(params[idx])
        lo, hi = float(ci[idx][0]), float(ci[idx][1])
        if family == "logit":
            est = math.exp(b)
            lo_v = math.exp(lo)
            hi_v = math.exp(hi)
        else:
            est = b
            lo_v, hi_v = lo, hi
        rows.append(
            {
                "label": name,
                "estimate": est,
                "ci_lo": lo_v,
                "ci_hi": hi_v,
                "p_value": float(pvals[idx]),
            }
        )
    return rows


def _compute_auc(result: Any, y: "Any", X: "Any") -> Optional[float]:
    """Optional AUC computation. Returns None when not computable."""
    if result is None or y.size == 0:
        return None
    try:
        import statsmodels.api as sm
        from sklearn.metrics import roc_auc_score  # noqa: F401

        Xc = sm.add_constant(X, has_constant="add")
        preds = result.predict(Xc)
        if len(set(y.tolist())) < 2:
            return None
        return float(roc_auc_score(y, preds))
    except ImportError:
        # Hand-rolled AUC via Mann-Whitney U: avoids a sklearn dep.
        try:
            import statsmodels.api as sm
            import numpy as np

            Xc = sm.add_constant(X, has_constant="add")
            preds = np.asarray(result.predict(Xc))
            pos = preds[y == 1]
            neg = preds[y == 0]
            if pos.size == 0 or neg.size == 0:
                return None
            # AUC = P(score(positive) > score(negative))
            n_pos = pos.size
            n_neg = neg.size
            wins = 0.0
            for p_score in pos:
                wins += float((neg < p_score).sum()) + 0.5 * float(
                    (neg == p_score).sum()
                )
            return float(wins / (n_pos * n_neg))
        except Exception:  # pragma: no cover — defensive
            return None
    except Exception:  # pragma: no cover — defensive
        return None


def _mcfadden_r2(result: Any) -> Optional[float]:
    """McFadden's pseudo-R² from the fitted result."""
    if result is None:
        return None
    try:
        return float(result.prsquared)
    except Exception:  # pragma: no cover — defensive
        return None


def _model_summary_header(result: Any, max_lines: int = 8) -> str:
    """Trimmed model summary header for embedding in the data dict.

    Used by tests to confirm a real Logit fit happened. We don't include
    the full coefficient table here — that goes in the forest plot —
    but the first ~8 lines have the n, df, pseudo-R² etc that a quick
    eyeball check needs.
    """
    if result is None:
        return "model did not converge"
    try:
        text = str(result.summary())
    except Exception:  # pragma: no cover — defensive
        return "summary unavailable"
    return "\n".join(text.splitlines()[:max_lines])


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


class LogisticRegressionBlock(ContentBlock):
    """Logistic regression with forest-plot + diagnostics output."""

    block_id: ClassVar[str] = "logistic_regression"
    Parameters: ClassVar[type[BaseModel]] = _LogisticRegressionParams
    audience_target: ClassVar[Optional[AudienceTarget]] = (
        AudienceTarget.RESEARCHER
    )

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _LogisticRegressionParams)
        rows, fetch_used = _resolve_collector_rows(
            ctx, params.outcome_spec_id, params.filters
        )

        # Apply filters in-process ONLY when the collector did not honour
        # them server-side. ``fetch(spec_id, filters)`` is presumed to
        # already filter; the legacy ``data()`` dict-bag does not, so we
        # filter here. The post-filter only fires when EVERY filter key
        # is present on at least one row — this stops the test-time
        # ``_FetchCollector`` (which ignores filters but doesn't carry
        # the filter columns) from accidentally zeroing out the result.
        if (
            not fetch_used
            and params.filters
            and rows
            and all(any(k in r for r in rows) for k in params.filters)
        ):
            filtered: List[Dict[str, Any]] = []
            for r in rows:
                if all(r.get(k) == v for k, v in params.filters.items()):
                    filtered.append(r)
            rows = filtered

        if not rows or not params.predictors:
            # Empty case: echo a graceful zero-row structure so renderers
            # short-circuit to "ไม่มีข้อมูล".
            return {
                "rows": [],
                "model_summary": "no data",
                "auc": None,
                "n": 0,
                "n_events": 0,
                "n_dropped": 0,
                "mcfadden_r2": None,
                "predictors": list(params.predictors),
                "warning": (
                    "no rows from data_collector"
                    if not rows
                    else "no predictors specified"
                ),
                "family": params.family,
            }

        y, X, kept, n_dropped = _build_design_matrix(
            rows, params.outcome_spec_id, params.predictors
        )
        result = _fit_glm(y, X, params.family)

        forest_rows = _result_to_rows(result, kept, params.family)
        n = int(y.size)
        n_events = int(y.sum()) if n else 0
        auc = _compute_auc(result, y, X)
        r2 = _mcfadden_r2(result)
        warning = None
        if result is None:
            warning = "model did not converge — review predictors"
        elif n_dropped > 0:
            warning = f"{n_dropped} rows dropped due to missing predictor values"

        return {
            "rows": forest_rows,
            "model_summary": _model_summary_header(result),
            "auc": auc,
            "n": n,
            "n_events": n_events,
            "n_dropped": n_dropped,
            "mcfadden_r2": r2,
            "predictors": kept,
            "warning": warning,
            "family": params.family,
        }

    # ------------------------------------------------------------------
    # LaTeX
    # ------------------------------------------------------------------

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        assert isinstance(params, _LogisticRegressionParams)
        rows: List[Dict[str, Any]] = data.get("rows", [])
        if not rows:
            return (
                r"\textit{ไม่มีข้อมูลเพียงพอสำหรับการถดถอยโลจิสติก}"
                + "\n"
            )

        family = data.get("family", params.family)
        metric_name = "Odds Ratio" if family == "logit" else "β coefficient"
        null_value = 1.0 if family == "logit" else 0.0
        log_scale = family == "logit"

        png_path = render_forest_to_png(
            rows, metric_name,
            null_value=null_value,
            log_scale=log_scale,
            caption=None,
        )

        # Diagnostics table (right column).
        diag_table = self._diag_latex(data)

        cap = latex_escape(
            params.caption_th
            or f"Logistic regression — odds ratios with 95% CI ({family})"
        )
        warning_block = ""
        warn = data.get("warning")
        if warn:
            warning_block = (
                "\\par\\smallskip\\textit{"
                + latex_escape(str(warn))
                + "}\n"
            )

        return (
            "\\begin{figure}[H]\n"
            "\\centering\n"
            "\\begin{minipage}[t]{0.62\\textwidth}\n"
            "\\centering\n"
            "\\includegraphics[width=\\textwidth]{"
            + str(png_path)
            + "}\n"
            "\\end{minipage}%\n"
            "\\hspace{0.02\\textwidth}%\n"
            "\\begin{minipage}[t]{0.34\\textwidth}\n"
            "\\centering\n"
            "\\vspace{0.5em}\n"
            + diag_table
            + "\n\\end{minipage}\n"
            f"\\caption{{{cap}}}\n"
            f"\\label{{fig:logistic_{_label_id(params.outcome_spec_id)}}}\n"
            + warning_block
            + "\\end{figure}\n"
        )

    @staticmethod
    def _diag_latex(data: Dict[str, Any]) -> str:
        """Small text table with n, n_events, AUC, McFadden R²."""
        def _fmt(v: Optional[float], places: int = 3) -> str:
            if v is None:
                return "—"
            try:
                return f"{float(v):.{places}f}"
            except (TypeError, ValueError):
                return "—"
        n = int(data.get("n", 0))
        n_events = int(data.get("n_events", 0))
        rows = [
            ("n", str(n)),
            ("events", str(n_events)),
            ("AUC", _fmt(data.get("auc"), places=3)),
            (r"McFadden $R^2$", _fmt(data.get("mcfadden_r2"), places=3)),
        ]
        body = "\n".join(
            r"\textbf{" + latex_escape(label) + "} & "
            + (latex_escape(value) if "$" not in label else value)
            + r" \\"
            for label, value in rows
        )
        # Note: we keep ``\textbf{$R^2$}`` math intact — latex_escape would
        # mangle the $ signs, so we only escape the value column.
        body_clean: List[str] = []
        for label, value in rows:
            esc_label = (
                r"\textbf{McFadden $R^2$}"
                if "R^2" in label
                else r"\textbf{" + latex_escape(label) + "}"
            )
            body_clean.append(esc_label + " & " + latex_escape(value) + r" \\")
        body = "\n".join(body_clean)
        return (
            r"\begin{tabular}{|l|r|}"
            + "\n\\hline\n"
            + body
            + "\n\\hline\n"
            + r"\end{tabular}"
        )

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        assert isinstance(params, _LogisticRegressionParams)
        rows: List[Dict[str, Any]] = data.get("rows", [])
        if not rows:
            return (
                '<section class="logistic-regression">'
                "<p><em>"
                + html.escape("ไม่มีข้อมูลเพียงพอสำหรับการถดถอยโลจิสติก")
                + "</em></p></section>"
            )

        family = data.get("family", params.family)
        metric_name = "Odds Ratio" if family == "logit" else "β coefficient"
        null_value = 1.0 if family == "logit" else 0.0
        log_scale = family == "logit"

        png_path = render_forest_to_png(
            rows, metric_name,
            null_value=null_value,
            log_scale=log_scale,
            caption=None,
        )

        try:
            import base64

            b64 = base64.b64encode(png_path.read_bytes()).decode("ascii")
            img_src = f"data:image/png;base64,{b64}"
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "logistic_regression: base64 inlining failed: %s", exc
            )
            img_src = png_path.as_uri()

        diag = self._diag_html(data)
        cap = html.escape(
            params.caption_th
            or f"Logistic regression — odds ratios with 95% CI ({family})"
        )
        warn = data.get("warning")
        warn_block = (
            f'<p class="logistic-warning"><em>{html.escape(str(warn))}</em></p>'
            if warn else ""
        )
        alt = html.escape(
            f"Logistic regression forest plot for {params.outcome_spec_id}"
        )
        return (
            '<section class="logistic-regression">'
            "<figure>"
            f'<img src="{img_src}" alt="{alt}" />'
            f"<figcaption>{cap}</figcaption>"
            "</figure>"
            + diag
            + warn_block
            + "</section>"
        )

    @staticmethod
    def _diag_html(data: Dict[str, Any]) -> str:
        def _fmt(v: Optional[float], places: int = 3) -> str:
            if v is None:
                return "—"
            try:
                return f"{float(v):.{places}f}"
            except (TypeError, ValueError):
                return "—"
        n = int(data.get("n", 0))
        n_events = int(data.get("n_events", 0))
        return (
            '<dl class="logistic-diagnostics">'
            f"<dt>n</dt><dd>{n}</dd>"
            f"<dt>events</dt><dd>{n_events}</dd>"
            f"<dt>AUC</dt><dd>{_fmt(data.get('auc'))}</dd>"
            f"<dt>McFadden R²</dt><dd>{_fmt(data.get('mcfadden_r2'))}</dd>"
            "</dl>"
        )


def _label_id(text: str) -> str:
    """LaTeX-safe label fragment derived from a free-form string."""
    return "".join(c if c.isalnum() or c == "_" else "_" for c in str(text))[:48] or "x"


__all__ = [
    "LogisticRegressionBlock",
    "BMA_GREEN",
    "RISK_RED",
]
