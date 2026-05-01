"""Jinja2 filter functions for the LaTeX renderer.

Extracted from the legacy ``services/report_generator.py`` so both the
new ``LaTeXRenderer`` and the legacy ``ReportGenerator`` share one
implementation. DO NOT mutate these signatures or the legacy renderer
will diverge from the new one.

Per ADR-03, the legacy module stays callable through S4 and is
decommissioned in S5; both paths must produce byte-identical escaping
during the transition.
"""
from __future__ import annotations

from typing import Any


def number_format(value: Any) -> str:
    """Render an integer with thousands separators, e.g. 12345 -> "12,345"."""
    try:
        return f"{int(value):,}"
    except (ValueError, TypeError):
        return str(value)


def pct_format(value: Any) -> str:
    """Render a percentage with one decimal, no '%' suffix, e.g. 12.345 -> "12.3"."""
    try:
        return f"{float(value):.1f}"
    except (ValueError, TypeError):
        return str(value)


def pct2_format(value: Any) -> str:
    """Render a percentage with two decimals, no '%' suffix, e.g. 12.345 -> "12.35"."""
    try:
        return f"{float(value):.2f}"
    except (ValueError, TypeError):
        return str(value)


def significance_stars(p_value: Any) -> str:
    """APA-style significance markers from a p-value."""
    try:
        p = float(p_value)
    except (ValueError, TypeError):
        return ""
    if p < 0.001:
        return "***"
    elif p < 0.01:
        return "**"
    elif p < 0.05:
        return "*"
    return "n.s."


# LaTeX special characters that need escaping in user-supplied strings.
# Order matters: escape backslash first, then everything else. The Jinja2
# delimiter pair (<<, >>) is also escaped so user-supplied text containing
# those literals doesn't get parsed as a Jinja directive when the value
# itself is interpolated into a downstream template (rare but possible).
_LATEX_ESCAPES: tuple[tuple[str, str], ...] = (
    ("\\", r"\textbackslash{}"),
    ("&", r"\&"),
    ("%", r"\%"),
    ("$", r"\$"),
    ("#", r"\#"),
    ("_", r"\_"),
    ("{", r"\{"),
    ("}", r"\}"),
    ("~", r"\textasciitilde{}"),
    ("^", r"\textasciicircum{}"),
    # Jinja2 delimiter conflict — protect against `<<` / `>>` in user content
    ("<<", r"<\,<"),
    (">>", r">\,>"),
)


def latex_safe(value: Any) -> str:
    """Escape LaTeX special chars in a user-supplied string.

    Use ``<< value | latex_safe >>`` in templates whenever the value comes
    from user input or external data (not from our own code constants),
    so a ``&``/``$``/``%``/``<<`` in the value can't break the LaTeX compile.
    """
    if value is None:
        return ""
    s = str(value)
    for src, repl in _LATEX_ESCAPES:
        s = s.replace(src, repl)
    return s


def latex_escape(text: Any) -> str:
    """Older variant of ``latex_safe`` kept for filter-name compatibility.

    Differs from ``latex_safe`` only in that it does NOT escape the Jinja2
    delimiter pair. Templates that use ``| latex_escape`` are happy with
    that since they don't run their output through a second Jinja pass.
    """
    s = str(text)
    replacements = (
        ("\\", "\\textbackslash{}"),
        ("&", "\\&"),
        ("%", "\\%"),
        ("$", "\\$"),
        ("#", "\\#"),
        ("_", "\\_"),
        ("{", "\\{"),
        ("}", "\\}"),
        ("~", "\\textasciitilde{}"),
        ("^", "\\textasciicircum{}"),
    )
    for old, new in replacements:
        s = s.replace(old, new)
    return s


# ---------------------------------------------------------------------------
# Aggregate filter map — used by both the legacy ReportGenerator and the
# new LaTeXRenderer to install filters with one call.
# ---------------------------------------------------------------------------

LATEX_FILTERS = {
    "number_format": number_format,
    "pct": pct_format,
    "latex_safe": latex_safe,
    "pct2": pct2_format,
    "sig_stars": significance_stars,
    "latex_escape": latex_escape,
}
