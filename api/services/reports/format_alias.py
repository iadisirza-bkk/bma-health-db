"""Format alias resolution — S7 ``latex`` → ``pdf`` rename.

The descriptor format ``latex`` was renamed to ``pdf`` in S7 because the
LaTeX renderer always returns a compiled PDF via tectonic — calling it
``latex`` was misleading to FE developers reading
``GET /api/v2/reports`` catalog output.

This module is the single source of truth for the rename:

    * ``CANONICAL_FORMAT[fmt]``  — collapse legacy ``latex`` to canonical
      ``pdf``. Idempotent: every other key maps to itself.
    * ``ALIASES[fmt]``           — set of every alias for a canonical fmt
      (``{"pdf", "latex"}`` for the renamed pair). Used when looking up
      a renderer that may have been registered under either key.
    * ``format_matches(requested, declared)`` — alias-aware ``in`` check
      that lets ``?fmt=latex`` succeed against ``formats: [pdf, html]``
      (and vice versa) so we can flip YAMLs without breaking callers.

Backward-compat horizon: alias kept for one sprint (S7). Drop after
ADR-03 §6 cutover when no caller writes ``latex`` to YAML / query string.
"""
from __future__ import annotations

import logging
import warnings
from typing import Iterable, Mapping, Set

logger = logging.getLogger("api.services.reports.format_alias")


# Canonical form: ``latex`` collapses to ``pdf``. All other formats are
# their own canonical form. Used as a one-way normalisation step at any
# place that wants to compare formats without caring about the alias.
CANONICAL_FORMAT: Mapping[str, str] = {
    "latex": "pdf",
}


# Alias buckets keyed by canonical fmt. ``aliases_for("pdf")`` returns
# both ``{"pdf", "latex"}`` so a renderer registered under either key
# can be located. Self-aliasing for unknown formats keeps callers simple.
_ALIAS_BUCKETS: Mapping[str, Set[str]] = {
    "pdf": {"pdf", "latex"},
}


def canonicalize(fmt: str) -> str:
    """Return the canonical fmt for ``fmt``; idempotent for non-aliased keys."""
    return CANONICAL_FORMAT.get(fmt, fmt)


def aliases_for(fmt: str) -> Set[str]:
    """Every alias for ``fmt`` (including ``fmt`` itself).

    Examples
    --------
    >>> sorted(aliases_for("pdf"))
    ['latex', 'pdf']
    >>> sorted(aliases_for("latex"))
    ['latex', 'pdf']
    >>> sorted(aliases_for("html"))
    ['html']
    """
    canonical = canonicalize(fmt)
    bucket = _ALIAS_BUCKETS.get(canonical)
    if bucket:
        return set(bucket)
    return {fmt}


def format_matches(requested: str, declared: Iterable[str]) -> bool:
    """True iff ``requested`` (or one of its aliases) is in ``declared``.

    Lets ``?fmt=latex`` succeed against ``formats: [pdf, html]`` so we
    can rename YAMLs without breaking older callers.
    """
    declared_set = set(declared)
    return bool(aliases_for(requested) & declared_set)


def warn_if_legacy(fmt: str, *, source: str = "request") -> None:
    """Emit a one-shot deprecation log + warning when ``fmt == "latex"``.

    ``source`` is a free-form context tag (``"yaml"``, ``"request"``,
    ``"renderer-registration"``) so the log line is grep-able.
    """
    if fmt == "latex":
        msg = (
            f"format 'latex' is deprecated; use 'pdf' (source={source}). "
            f"Backward-compat alias will be removed after S7."
        )
        logger.warning(msg)
        warnings.warn(msg, DeprecationWarning, stacklevel=2)


__all__ = [
    "CANONICAL_FORMAT",
    "aliases_for",
    "canonicalize",
    "format_matches",
    "warn_if_legacy",
]
