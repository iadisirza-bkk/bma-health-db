"""Caption-convention helpers for ``ContentBlock.render_*``.

Phase 0 of the report-generation overhaul: enforce in ONE place the
academic convention of *figures get caption BELOW, tables get caption
ABOVE*. New blocks SHOULD route every figure / table through these
helpers. Older blocks (``chart``, ``density_plot``, ``forest_plot``,
``table``, ``crosstab``, ...) embed similar logic inline and will be
migrated incrementally.

LaTeX side mirrors the ``\\bmafig`` / ``\\bmatab`` macros declared in
``templates/latex/bma_article_preamble.tex`` and ``bma_beamer_preamble.tex``.
HTML side uses ``<figure>/<figcaption>`` and ``<table><caption>`` —
both render the caption on the correct side by default in every
mainstream browser, no CSS required.
"""
from __future__ import annotations

from typing import Mapping, Optional

from services.latex_utils import latex_escape

__all__ = [
    "safe_label_part",
    "wrap_figure_latex",
    "wrap_table_latex",
    "wrap_figure_html",
    "wrap_table_html",
]


def safe_label_part(raw: str) -> str:
    """Sanitise a string for safe inclusion in a LaTeX ``\\label{...}``.

    Replaces every char that's neither alphanumeric nor underscore with
    ``_``. Callers compose final labels themselves, e.g.::

        label = "fig:chart:" + safe_label_part(spec_id)
    """
    return "".join(c if c.isalnum() or c == "_" else "_" for c in str(raw))


def _html_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# LaTeX wrappers
# ---------------------------------------------------------------------------


def wrap_figure_latex(
    body: str,
    caption: str,
    label: str,
    *,
    width: str = r"0.85\textwidth",
    centered: bool = True,
    post_caption: Optional[str] = None,
) -> str:
    """Wrap a figure body in a ``figure`` env with caption BELOW.

    ``body`` is the inner content (TikZ picture, ``\\includegraphics{...}``,
    etc.) and is inserted verbatim — caller is responsible for escaping
    any literal text inside it. ``caption`` IS LaTeX-escaped here.

    The ``width`` parameter is informational only; this helper does NOT
    inject ``\\includegraphics``. Pass it through to ``body`` yourself
    when you build the includegraphics call. The kwarg exists so the
    signature parallels the ``\\bmafig[width]`` macro for readability.

    ``post_caption`` is verbatim LaTeX inserted between ``\\label`` and
    ``\\end{figure}`` — used for truncation notes / sample-size lines
    that need to render INSIDE the figure float (so they don't drift to
    a different page than the figure itself). Caller pre-escapes any
    literal text inside it.
    """
    del width  # placeholder for signature symmetry; see docstring
    cap = latex_escape(caption) if caption else ""
    cap_block = f"\\caption{{{cap}}}\n\\label{{{label}}}\n" if cap else ""
    centering = "\\centering\n" if centered else ""
    extras = post_caption or ""
    return (
        "\\begin{figure}[H]\n"
        f"{centering}"
        f"{body}\n"
        f"{cap_block}"
        f"{extras}"
        "\\end{figure}\n"
    )


def wrap_table_latex(
    tabular_body: str,
    caption: str,
    label: str,
    *,
    centered: bool = True,
) -> str:
    """Wrap a tabular body in a ``table`` env with caption ABOVE.

    ``tabular_body`` should already be a complete ``\\begin{tabular}{...}
    ... \\end{tabular}`` (or ``longtable``) and is inserted verbatim.
    ``caption`` IS LaTeX-escaped here.

    The caption is emitted BEFORE the tabular so it visually sits above
    the table. ``\\caption`` inside ``\\begin{table}`` honours the source
    order with the standard ``caption`` package.
    """
    cap = latex_escape(caption) if caption else ""
    cap_block = f"\\caption{{{cap}}}\n\\label{{{label}}}\n" if cap else ""
    centering = "\\centering\n" if centered else ""
    return (
        "\\begin{table}[H]\n"
        f"{centering}"
        f"{cap_block}"
        f"{tabular_body}\n"
        "\\end{table}\n"
    )


# ---------------------------------------------------------------------------
# HTML wrappers
# ---------------------------------------------------------------------------


def wrap_figure_html(
    body: str,
    caption: str,
    *,
    label: Optional[str] = None,
    css_class: str = "figure",
    extra_attrs: Optional[Mapping[str, str]] = None,
    post_caption: Optional[str] = None,
) -> str:
    """Wrap an HTML figure body in ``<figure>`` with ``<figcaption>`` last.

    ``<figcaption>`` placed after the body matches the LaTeX convention —
    caption renders below the figure by default in every mainstream
    browser. ``caption`` IS HTML-escaped; ``body`` is inserted verbatim
    (caller responsible for escaping literal text inside SVG / IMG tags).

    ``extra_attrs`` lets callers attach informational attributes
    (``data-spec-id``, ``data-column``, ...) without each block having
    to re-implement the figure assembly. Both keys and values are
    HTML-escaped before insertion.

    ``post_caption`` is verbatim HTML inserted between ``</figcaption>``
    and ``</figure>`` — used for truncation notes that belong to the
    figure but render below the caption. Caller pre-escapes literal text.
    """
    cap_block = (
        f"<figcaption>{_html_escape(caption)}</figcaption>" if caption else ""
    )
    id_attr = f' id="{_html_escape(label)}"' if label else ""
    extras_attrs = ""
    if extra_attrs:
        for k, v in extra_attrs.items():
            extras_attrs += f' {_html_escape(k)}="{_html_escape(v)}"'
    post = post_caption or ""
    return (
        f'<figure class="{_html_escape(css_class)}"{id_attr}{extras_attrs}>'
        + body
        + cap_block
        + post
        + "</figure>"
    )


def wrap_table_html(
    inner: str,
    caption: str,
    *,
    label: Optional[str] = None,
    css_class: str = "data-table",
) -> str:
    """Build a ``<table>`` whose first child is ``<caption>``.

    ``inner`` is the table body markup *without* the outer ``<table>``
    wrapper — typically ``<thead>...</thead><tbody>...</tbody>``. The
    helper assembles ``<table class="..."><caption>...</caption>{inner}
    </table>`` so the HTML spec invariant (caption MUST be the first
    child of table) holds, and browsers render it above the rows.

    ``caption`` IS HTML-escaped; ``inner`` is inserted verbatim.
    """
    cap_html = (
        f"<caption>{_html_escape(caption)}</caption>" if caption else ""
    )
    id_attr = f' id="{_html_escape(label)}"' if label else ""
    return (
        f'<table class="{_html_escape(css_class)}"{id_attr}>'
        + cap_html
        + inner
        + "</table>"
    )
