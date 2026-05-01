"""ChartRegistry — filesystem-backed loader for chart YAML configs.

Discovery rules (per ADR-01 §2):
    * Location: `bma-health-db/config/charts/<spec_id>.yaml`.
    * Filename stem becomes the canonical `spec_id` and MUST match the
      `spec_id` field inside the YAML.
    * Server reads all `*.yaml` at startup. Fail loud on any parse /
      validation error — never silent-skip.
    * Hot-reload is NOT in S2: server restart required to pick up new
      YAML.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

import yaml  # type: ignore[import-untyped]

from .spec import ChartSpec

logger = logging.getLogger("api.services.charts.registry")


class ChartRegistry:
    """In-memory map of spec_id → ChartSpec, loaded from YAML at boot."""

    def __init__(self, specs: Optional[Dict[str, ChartSpec]] = None) -> None:
        self._specs: Dict[str, ChartSpec] = dict(specs) if specs else {}

    @classmethod
    def discover(cls, config_dir: Path) -> "ChartRegistry":
        """Scan ``config_dir`` for ``*.yaml`` and load every file.

        Fails loud on:
            - missing directory
            - YAML parse error
            - Pydantic validation error
            - filename-stem ≠ spec_id mismatch
            - duplicate spec_id within the directory
        """
        config_dir = Path(config_dir)
        if not config_dir.is_dir():
            raise FileNotFoundError(
                f"chart config_dir does not exist or is not a directory: "
                f"{config_dir}"
            )

        specs: Dict[str, ChartSpec] = {}
        yaml_paths = sorted(config_dir.glob("*.yaml"))
        for path in yaml_paths:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh)
            except yaml.YAMLError as exc:
                raise ValueError(
                    f"failed to parse YAML for chart spec {path}: {exc}"
                ) from exc

            if not isinstance(raw, dict):
                raise ValueError(
                    f"chart spec {path} must be a YAML mapping, got "
                    f"{type(raw).__name__}"
                )

            spec = ChartSpec(**raw)  # raises ValidationError on bad input

            stem = path.stem
            if spec.spec_id != stem:
                raise ValueError(
                    f"chart spec {path}: filename stem {stem!r} does not "
                    f"match spec_id {spec.spec_id!r}"
                )
            if spec.spec_id in specs:
                raise ValueError(
                    f"duplicate chart spec_id {spec.spec_id!r} (second copy "
                    f"at {path})"
                )
            specs[spec.spec_id] = spec

        logger.info(
            "ChartRegistry.discover: loaded %d spec(s) from %s",
            len(specs),
            config_dir,
        )
        return cls(specs)

    def get(self, spec_id: str) -> ChartSpec:
        """Return the spec or raise ``KeyError`` if unknown."""
        try:
            return self._specs[spec_id]
        except KeyError as exc:
            raise KeyError(f"unknown chart spec_id: {spec_id!r}") from exc

    def list_ids(self) -> List[str]:
        """Return all known spec ids in stable sorted order."""
        return sorted(self._specs)

    def __contains__(self, spec_id: object) -> bool:
        return spec_id in self._specs

    def __len__(self) -> int:
        return len(self._specs)


# ---------------------------------------------------------------------------
# Lazy module-level singleton. The first call discovers; subsequent calls
# return the same instance. The dependency factory in S2.5 will use this.
# ---------------------------------------------------------------------------
_REGISTRY: Optional[ChartRegistry] = None


def _default_config_dir() -> Path:
    """Default config dir is `<repo>/config/charts/` — three levels up
    from this module (api/services/charts/registry.py)."""
    return Path(__file__).resolve().parents[3] / "config" / "charts"


def chart_registry(
    config_dir: Optional[Path] = None,
    *,
    reload: bool = False,
) -> ChartRegistry:
    """Lazy singleton getter.

    Parameters
    ----------
    config_dir:
        Override the default `<repo>/config/charts/` directory. Mostly
        useful for tests.
    reload:
        If True, rebuild the registry from disk even if a cached copy
        exists. Hot-reload is NOT a production feature (ADR-01 §2) — this
        flag is for tests / dev only.
    """
    global _REGISTRY
    if _REGISTRY is None or reload:
        _REGISTRY = ChartRegistry.discover(config_dir or _default_config_dir())
    return _REGISTRY
