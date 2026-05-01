"""Format-agnostic Jinja2 filters shared by every :class:`ReportRenderer`.

History
-------
These helpers used to live as private module-level functions in
``services/report_generator.py`` (``_number_format``, ``_pct_format``,
``_pct2_format``). When the LaTeX path moved into a dedicated renderer
(S4.2) and the HTML path joined it (S4.3), keeping two copies of the same
formatters would have meant the LaTeX PDF and the HTML output could
silently drift on rounding / thousands-separator behaviour. This module
is the one place where the formatters live — the LaTeX renderer's
``_latex_filters`` helper imports from here, and so does
``HTMLRenderer``.

Scope
-----
Only format-agnostic helpers belong here. LaTeX-only escapers
(``latex_safe`` / ``latex_escape``) stay in ``_latex_filters.py`` because
they hard-code LaTeX special characters; an HTML version would need a
totally different escape table (and Jinja2's ``autoescape`` already covers
the HTML side).
"""
from __future__ import annotations

from typing import Any


def number_format(value: Any) -> str:
    """Render an integer-ish value with a thousands separator.

    Falls back to ``str(value)`` for values that can't be coerced to int —
    matches the legacy ``_number_format`` in ``report_generator.py`` so
    cached LaTeX output stays byte-identical after the cutover.
    """
    try:
        return f"{int(value):,}"
    except (ValueError, TypeError):
        return str(value)


def pct(value: Any) -> str:
    """One-decimal percentage formatter ("63.4")."""
    try:
        return f"{float(value):.1f}"
    except (ValueError, TypeError):
        return str(value)


def pct2(value: Any) -> str:
    """Two-decimal percentage formatter ("63.42")."""
    try:
        return f"{float(value):.2f}"
    except (ValueError, TypeError):
        return str(value)


# ---------------------------------------------------------------------------
# Convenience: a single dict that any Jinja2 environment can splat into
# ``env.filters``. Avoids repeating the same three lines in every renderer.
# ---------------------------------------------------------------------------
SHARED_FILTERS = {
    "number_format": number_format,
    "pct": pct,
    "pct2": pct2,
}


__all__ = [
    "number_format",
    "pct",
    "pct2",
    "SHARED_FILTERS",
]
