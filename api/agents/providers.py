"""Provider registry — config-driven LLM adapter construction (ADR-02 §2).

Loads ``config/llm/providers.yaml`` at boot and produces concrete
``LLMAdapter`` instances on demand. Adding a new provider is a YAML edit
plus a new ``<Provider>Adapter`` class — zero churn anywhere else.

Discovery semantics mirror ``ChartRegistry`` from ADR-01 / S2:
    * Filesystem-backed YAML.
    * Pydantic v2 validation with ``extra="forbid"`` so a typo in the
      config file fails loud at startup.
    * ``${VAR}`` env-var interpolation in any string value.
    * Fail loud on:
        - missing config file
        - duplicate provider name
        - unknown adapter type
        - missing env var when ``api_key_env`` is set
        - invalid pydantic shape

Built-in adapter map: ``{"lmstudio": LMStudioAdapter}``. S3.2 will add
``"anthropic": AnthropicAdapter`` as a sibling registration.
"""
from __future__ import annotations

import importlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from agents.adapters.base import AdapterConfig, LLMAdapter
from agents.adapters.lmstudio import LMStudioAdapter

# Importing the strategies package (not just the registry module) is
# load-bearing: the package __init__ is what runs the @register
# decorators that populate StrategyRegistry. Without this import
# `for_model()` would always raise LookupError.
import agents.strategies  # noqa: F401  (side-effect import)
from agents.strategies.registry import StrategyRegistry

logger = logging.getLogger("agents.providers")


# ---------------------------------------------------------------------------
# Pydantic v2 config models
# ---------------------------------------------------------------------------

class ProviderConfig(BaseModel):
    """Single ``providers[]`` entry from ``providers.yaml``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    adapter: str
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    timeout: int = 120
    extra: Dict[str, Any] = Field(default_factory=dict)


class _DefaultBinding(BaseModel):
    """A ``{provider, model}`` pair used to wire analyst / synthesizer."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str


class DefaultConfig(BaseModel):
    """``defaults:`` block mapping role → provider+model."""

    model_config = ConfigDict(extra="forbid")

    analyst: _DefaultBinding
    synthesizer: _DefaultBinding


class ProvidersFile(BaseModel):
    """Top-level ``providers.yaml`` schema."""

    model_config = ConfigDict(extra="forbid")

    providers: List[ProviderConfig]
    defaults: DefaultConfig


# ---------------------------------------------------------------------------
# Built-in adapter type → class map. New adapters get added here
# (or via ``ProviderRegistry.register`` for plugin-style extensions).
# ---------------------------------------------------------------------------

_BUILTIN_ADAPTERS: Dict[str, type[LLMAdapter]] = {
    "lmstudio": LMStudioAdapter,
}


# ---------------------------------------------------------------------------
# ${VAR} interpolation
# ---------------------------------------------------------------------------

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(node: Any) -> Any:
    """Walk ``node`` and replace every ``${VAR}`` substring inside a string
    leaf with ``os.environ[VAR]``. Missing env vars raise ``KeyError`` so
    the operator sees the failure at boot rather than at first request.
    """
    if isinstance(node, str):
        def _sub(match: re.Match[str]) -> str:
            var = match.group(1)
            if var not in os.environ:
                raise KeyError(
                    f"providers.yaml references env var ${{{var}}} but it is not set"
                )
            return os.environ[var]
        return _ENV_PATTERN.sub(_sub, node)
    if isinstance(node, dict):
        return {k: _expand_env(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_env(v) for v in node]
    return node


# ---------------------------------------------------------------------------
# ProviderRegistry
# ---------------------------------------------------------------------------

class ProviderRegistry:
    """Registry of adapter classes + per-provider wire config from YAML.

    Two-stage construction:
        * ``register(name, adapter_cls)`` — class registration. Tests and
          plugins call this directly.
        * ``discover(config_path)`` — class registration plus per-name
          config row, loaded from YAML. Production code calls this.

    ``build(name, model, **overrides)`` returns a ready ``LLMAdapter`` by
    combining the registered class with the YAML config row.
    """

    def __init__(
        self,
        adapters: Optional[Dict[str, type[LLMAdapter]]] = None,
        configs: Optional[Dict[str, ProviderConfig]] = None,
        defaults: Optional[DefaultConfig] = None,
    ) -> None:
        # Adapter-type → class. Built-ins seeded so tests that use only
        # ``register`` still work without going through YAML.
        self._adapters: Dict[str, type[LLMAdapter]] = dict(_BUILTIN_ADAPTERS)
        if adapters:
            self._adapters.update(adapters)
        # provider name → ProviderConfig (the YAML row)
        self._configs: Dict[str, ProviderConfig] = dict(configs) if configs else {}
        self._defaults: Optional[DefaultConfig] = defaults

    # -- registration ------------------------------------------------------

    def register(self, name: str, adapter_cls: type[LLMAdapter]) -> None:
        """Register an adapter type. ``name`` is the ``adapter:`` value
        used in ``providers.yaml`` (e.g. ``"lmstudio"``, ``"anthropic"``)."""
        if name in self._adapters and self._adapters[name] is not adapter_cls:
            raise ValueError(
                f"adapter type {name!r} already registered as "
                f"{self._adapters[name].__name__}"
            )
        self._adapters[name] = adapter_cls

    # -- introspection -----------------------------------------------------

    def list(self) -> List[str]:
        """Return all known provider names (from YAML), sorted."""
        return sorted(self._configs)

    def list_adapters(self) -> List[str]:
        """Return all registered adapter type names, sorted."""
        return sorted(self._adapters)

    @property
    def defaults(self) -> Optional[DefaultConfig]:
        return self._defaults

    # -- construction ------------------------------------------------------

    def build(self, name: str, model: str, **overrides: Any) -> LLMAdapter:
        """Construct an ``LLMAdapter`` for the named provider + model.

        ``overrides`` win over the YAML row for ``base_url`` / ``timeout``
        / ``temperature`` / ``max_tokens``. They are passed straight into
        ``AdapterConfig`` so callers can spin up ad-hoc adapters in tests.
        """
        if name not in self._configs:
            raise KeyError(
                f"unknown provider {name!r}; known: {self.list()}"
            )
        cfg = self._configs[name]

        adapter_cls = self._adapters.get(cfg.adapter)
        if adapter_cls is None:
            raise ValueError(
                f"provider {name!r} declares unknown adapter type "
                f"{cfg.adapter!r}; known adapters: {self.list_adapters()}"
            )

        # Optional API key resolution — done at build-time so the boot
        # check in ``discover`` can confirm the env var EXISTS, then
        # build can read its CURRENT value (helpful in tests).
        api_key = None
        if cfg.api_key_env:
            api_key = os.environ.get(cfg.api_key_env)
            if not api_key:
                raise RuntimeError(
                    f"provider {name!r} requires env var "
                    f"{cfg.api_key_env!r} but it is empty / unset"
                )

        adapter_config = AdapterConfig(
            base_url=overrides.get("base_url", cfg.base_url or ""),
            model=model,
            temperature=overrides.get("temperature", 0.1),
            max_tokens=overrides.get("max_tokens", 2000),
            timeout=overrides.get("timeout", cfg.timeout),
        )

        # The adapter class signature is the contract here: every adapter
        # accepts (config: AdapterConfig). Some accept a strategy too —
        # detect via __init__ signature for back-compat with LMStudio's
        # current shape, but prefer wiring the strategy explicitly.
        strategy = StrategyRegistry.for_model(model)
        try:
            return adapter_cls(config=adapter_config, strategy=strategy)
        except TypeError:
            # Adapter does not take a strategy (e.g. a future Anthropic
            # adapter that does its own tool-call parsing).
            return adapter_cls(config=adapter_config)

    # -- discovery ---------------------------------------------------------

    @classmethod
    def discover(cls, config_path: Path) -> "ProviderRegistry":
        """Load + validate ``config_path`` (a ``providers.yaml`` file).

        Fails loud on every form of broken config we can detect at boot.
        """
        config_path = Path(config_path)
        if not config_path.is_file():
            raise FileNotFoundError(
                f"providers config file does not exist: {config_path}"
            )

        try:
            with config_path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ValueError(
                f"failed to parse providers config {config_path}: {exc}"
            ) from exc

        if not isinstance(raw, dict):
            raise ValueError(
                f"providers config {config_path} must be a YAML mapping, "
                f"got {type(raw).__name__}"
            )

        # ${VAR} expansion happens BEFORE pydantic validation so the
        # validator sees fully-resolved strings.
        expanded = _expand_env(raw)

        try:
            parsed = ProvidersFile(**expanded)
        except Exception as exc:
            raise ValueError(
                f"providers config {config_path} failed validation: {exc}"
            ) from exc

        # Detect duplicate provider names within the file.
        configs: Dict[str, ProviderConfig] = {}
        for entry in parsed.providers:
            if entry.name in configs:
                raise ValueError(
                    f"duplicate provider name {entry.name!r} in {config_path}"
                )
            if entry.adapter not in _BUILTIN_ADAPTERS:
                raise ValueError(
                    f"provider {entry.name!r} declares unknown adapter "
                    f"type {entry.adapter!r}; known: {sorted(_BUILTIN_ADAPTERS)}"
                )
            # Boot-time env var sanity check. Build-time still re-checks
            # because env vars CAN change between boot and a test re-read.
            if entry.api_key_env and not os.environ.get(entry.api_key_env):
                raise RuntimeError(
                    f"provider {entry.name!r} requires env var "
                    f"{entry.api_key_env!r} but it is empty / unset"
                )
            configs[entry.name] = entry

        logger.info(
            "ProviderRegistry.discover: loaded %d provider(s) from %s",
            len(configs),
            config_path,
        )
        return cls(configs=configs, defaults=parsed.defaults)


# ---------------------------------------------------------------------------
# Lazy module-level singleton — mirrors ``chart_registry()`` in S2.
# ---------------------------------------------------------------------------

_REGISTRY: Optional[ProviderRegistry] = None


def _default_config_path() -> Path:
    """Default path is ``<repo>/config/llm/providers.yaml`` — three levels
    up from this module (``api/agents/providers.py``)."""
    return Path(__file__).resolve().parents[2] / "config" / "llm" / "providers.yaml"


def provider_registry(
    config_path: Optional[Path] = None,
    *,
    reload: bool = False,
) -> ProviderRegistry:
    """Lazy singleton getter (matches ``chart_registry()`` semantics).

    Parameters
    ----------
    config_path:
        Override the default ``<repo>/config/llm/providers.yaml`` path.
        Mostly useful for tests.
    reload:
        Rebuild from disk even if a cached copy exists. Tests / dev only.
    """
    global _REGISTRY
    if _REGISTRY is None or reload:
        _REGISTRY = ProviderRegistry.discover(config_path or _default_config_path())
    return _REGISTRY
