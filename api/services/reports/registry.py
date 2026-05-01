"""ReportRegistry — filesystem-backed loader for report descriptor YAMLs.

Discovery rules (per ADR-03 §2 and the ChartRegistry precedent in ADR-01 §2):
    * Location: ``bma-health-db/config/reports/<report_id>.yaml``.
    * Filename stem becomes the canonical ``report_id`` and MUST match
      the ``report_id`` field inside the YAML.
    * Server reads all ``*.yaml`` at startup. Fail loud on any parse /
      validation error — never silent-skip.
    * Cross-registry validation: every ``section.block`` is resolved
      against a ``BlockRegistry`` (default: the module singleton).
      Unknown ``block`` references fail at load time, not at render time.
    * Hot-reload is NOT production-grade: server restart required to
      pick up new YAML.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from services.reports.blocks import BlockRegistry, block_registry
from services.reports.spec import ReportDescriptor

logger = logging.getLogger("api.services.reports.registry")


class ReportRegistry:
    """In-memory map of ``report_id`` → ``ReportDescriptor``."""

    def __init__(
        self,
        descriptors: Optional[Dict[str, ReportDescriptor]] = None,
    ) -> None:
        self._descriptors: Dict[str, ReportDescriptor] = (
            dict(descriptors) if descriptors else {}
        )

    @classmethod
    def discover(
        cls,
        config_dir: Path,
        *,
        blocks: Optional[BlockRegistry] = None,
    ) -> "ReportRegistry":
        """Scan ``config_dir`` for ``*.yaml`` and load every descriptor.

        Fails loud on:
            * missing directory
            * YAML parse error
            * Pydantic validation error
            * filename-stem ≠ report_id mismatch
            * duplicate report_id
            * unknown ``section.block`` reference (cross-registry check)

        ``blocks`` defaults to the ``block_registry()`` singleton; tests
        can pass a hand-built ``BlockRegistry`` to keep the report and
        block surfaces decoupled.
        """
        config_dir = Path(config_dir)
        if not config_dir.is_dir():
            raise FileNotFoundError(
                f"report config_dir does not exist or is not a directory: "
                f"{config_dir}"
            )
        block_reg = blocks if blocks is not None else block_registry()

        descriptors: Dict[str, ReportDescriptor] = {}
        for path in sorted(config_dir.glob("*.yaml")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh)
            except yaml.YAMLError as exc:
                raise ValueError(
                    f"failed to parse YAML for report descriptor {path}: {exc}"
                ) from exc

            if not isinstance(raw, dict):
                raise ValueError(
                    f"report descriptor {path} must be a YAML mapping, got "
                    f"{type(raw).__name__}"
                )

            descriptor = ReportDescriptor(**raw)

            stem = path.stem
            if descriptor.report_id != stem:
                raise ValueError(
                    f"report descriptor {path}: filename stem {stem!r} "
                    f"does not match report_id {descriptor.report_id!r}"
                )
            if descriptor.report_id in descriptors:
                raise ValueError(
                    f"duplicate report_id {descriptor.report_id!r} "
                    f"(second copy at {path})"
                )

            # Cross-registry validation — every block must already be
            # registered. Catching this at boot beats discovering it
            # mid-render.
            for section in descriptor.sections:
                if section.block not in block_reg:
                    raise ValueError(
                        f"report descriptor {path}: section "
                        f"{section.id!r} references unknown "
                        f"block {section.block!r}"
                    )

            descriptors[descriptor.report_id] = descriptor

        logger.info(
            "ReportRegistry.discover: loaded %d descriptor(s) from %s",
            len(descriptors),
            config_dir,
        )
        return cls(descriptors)

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get(self, report_id: str) -> ReportDescriptor:
        """Return the descriptor or raise ``KeyError`` if unknown."""
        try:
            return self._descriptors[report_id]
        except KeyError as exc:
            raise KeyError(f"unknown report_id: {report_id!r}") from exc

    def list_ids(self) -> List[str]:
        """Return all known report ids in stable sorted order."""
        return sorted(self._descriptors)

    def __contains__(self, report_id: object) -> bool:
        return report_id in self._descriptors

    def __len__(self) -> int:
        return len(self._descriptors)


# ---------------------------------------------------------------------------
# Lazy module-level singleton.
# ---------------------------------------------------------------------------


_REGISTRY: Optional[ReportRegistry] = None


def _default_config_dir() -> Path:
    """Default config dir: ``<repo>/config/reports/`` — three levels up
    from this module (``api/services/reports/registry.py``)."""
    return Path(__file__).resolve().parents[3] / "config" / "reports"


def report_registry(
    config_dir: Optional[Path] = None,
    *,
    reload: bool = False,
    blocks: Optional[BlockRegistry] = None,
) -> ReportRegistry:
    """Lazy singleton getter — mirrors ``chart_registry()``.

    Parameters
    ----------
    config_dir:
        Override the default ``<repo>/config/reports/`` directory.
        Mostly useful for tests.
    reload:
        If True, rebuild the registry from disk even if a cached copy
        exists.
    blocks:
        Override the ``BlockRegistry`` used for cross-registry
        validation. Defaults to the ``block_registry()`` singleton.
    """
    global _REGISTRY
    if _REGISTRY is None or reload:
        _REGISTRY = ReportRegistry.discover(
            config_dir or _default_config_dir(),
            blocks=blocks,
        )
    return _REGISTRY
