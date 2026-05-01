"""Unit tests for ProviderRegistry + StrategyRegistry (ADR-02 §2, §3).

No real network calls. The registry is constructed from in-tmp YAML and
walked entirely in-process; the LMStudio adapter class itself is just
checked for type identity, never invoked.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# tests/conftest.py inserts api/ on sys.path, but tests subdir doesn't
# always inherit that — make sure the api dir is importable here too.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "api"))

from agents.adapters.lmstudio import LMStudioAdapter  # noqa: E402
from agents.providers import (  # noqa: E402
    ProviderRegistry,
    _expand_env,
)
from agents.strategies.gemma import GemmaToolCallStrategy  # noqa: E402
from agents.strategies.openai_native import OpenAINativeStrategy  # noqa: E402
from agents.strategies.registry import StrategyRegistry  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_YAML = """\
providers:
  - name: lmstudio
    adapter: lmstudio
    base_url: ${TEST_LMSTUDIO_URL}
    timeout: 90

defaults:
  analyst:
    provider: lmstudio
    model: gemma-3-27b
  synthesizer:
    provider: lmstudio
    model: gemma-3-27b
"""


@pytest.fixture
def yaml_with_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a valid providers.yaml with an env-var placeholder pre-set."""
    monkeypatch.setenv("TEST_LMSTUDIO_URL", "http://localhost:5555")
    p = tmp_path / "providers.yaml"
    p.write_text(_VALID_YAML, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# _expand_env
# ---------------------------------------------------------------------------

def test_expand_env_substitutes_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "bar")
    assert _expand_env("hello-${FOO}") == "hello-bar"


def test_expand_env_walks_dict_and_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST", "h")
    monkeypatch.setenv("PORT", "9000")
    src = {
        "providers": [
            {"base_url": "http://${HOST}:${PORT}", "timeout": 30}
        ]
    }
    out = _expand_env(src)
    assert out == {
        "providers": [
            {"base_url": "http://h:9000", "timeout": 30}
        ]
    }


def test_expand_env_missing_var_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
    with pytest.raises(KeyError, match="DOES_NOT_EXIST"):
        _expand_env("${DOES_NOT_EXIST}")


# ---------------------------------------------------------------------------
# ProviderRegistry.discover
# ---------------------------------------------------------------------------

def test_discover_loads_yaml(yaml_with_env: Path) -> None:
    reg = ProviderRegistry.discover(yaml_with_env)
    assert reg.list() == ["lmstudio"]
    assert reg.defaults is not None
    assert reg.defaults.analyst.provider == "lmstudio"
    assert reg.defaults.analyst.model == "gemma-3-27b"


def test_discover_env_interpolation_runs_before_validation(
    yaml_with_env: Path,
) -> None:
    """The base_url stored in the registry should be the EXPANDED form."""
    reg = ProviderRegistry.discover(yaml_with_env)
    cfg = reg._configs["lmstudio"]
    assert cfg.base_url == "http://localhost:5555"


def test_discover_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ProviderRegistry.discover(tmp_path / "does-not-exist.yaml")


def test_discover_missing_env_var_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """${VAR} that resolves to nothing in os.environ is a boot-time error."""
    monkeypatch.delenv("LM_URL_THAT_DOES_NOT_EXIST", raising=False)
    p = tmp_path / "providers.yaml"
    p.write_text(
        "providers:\n"
        "  - name: lmstudio\n"
        "    adapter: lmstudio\n"
        "    base_url: ${LM_URL_THAT_DOES_NOT_EXIST}\n"
        "    timeout: 60\n"
        "defaults:\n"
        "  analyst: {provider: lmstudio, model: m}\n"
        "  synthesizer: {provider: lmstudio, model: m}\n",
        encoding="utf-8",
    )
    with pytest.raises(KeyError, match="LM_URL_THAT_DOES_NOT_EXIST"):
        ProviderRegistry.discover(p)


def test_discover_unknown_adapter_raises(tmp_path: Path) -> None:
    p = tmp_path / "providers.yaml"
    p.write_text(
        "providers:\n"
        "  - name: bogus\n"
        "    adapter: nonexistent_adapter\n"
        "    timeout: 30\n"
        "defaults:\n"
        "  analyst: {provider: bogus, model: m}\n"
        "  synthesizer: {provider: bogus, model: m}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown adapter"):
        ProviderRegistry.discover(p)


def test_discover_duplicate_provider_name_raises(tmp_path: Path) -> None:
    p = tmp_path / "providers.yaml"
    p.write_text(
        "providers:\n"
        "  - name: lmstudio\n"
        "    adapter: lmstudio\n"
        "    base_url: http://a\n"
        "    timeout: 30\n"
        "  - name: lmstudio\n"
        "    adapter: lmstudio\n"
        "    base_url: http://b\n"
        "    timeout: 30\n"
        "defaults:\n"
        "  analyst: {provider: lmstudio, model: m}\n"
        "  synthesizer: {provider: lmstudio, model: m}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate provider name"):
        ProviderRegistry.discover(p)


def test_discover_pydantic_extra_field_rejected(tmp_path: Path) -> None:
    """extra='forbid' means a typo in the YAML kills boot."""
    p = tmp_path / "providers.yaml"
    p.write_text(
        "providers:\n"
        "  - name: lmstudio\n"
        "    adapter: lmstudio\n"
        "    base_url: http://a\n"
        "    timeout: 30\n"
        "    typo_field: oops\n"
        "defaults:\n"
        "  analyst: {provider: lmstudio, model: m}\n"
        "  synthesizer: {provider: lmstudio, model: m}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="failed validation"):
        ProviderRegistry.discover(p)


def test_discover_missing_api_key_env_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that declares api_key_env requires that env var to exist."""
    # Pretend "lmstudio" needs an api key — registering a fake adapter
    # would be cleaner but the boot check is purely on api_key_env, so
    # we can use any built-in adapter type.
    monkeypatch.delenv("FAKE_LLM_KEY", raising=False)
    p = tmp_path / "providers.yaml"
    p.write_text(
        "providers:\n"
        "  - name: lmstudio\n"
        "    adapter: lmstudio\n"
        "    base_url: http://x\n"
        "    api_key_env: FAKE_LLM_KEY\n"
        "    timeout: 30\n"
        "defaults:\n"
        "  analyst: {provider: lmstudio, model: m}\n"
        "  synthesizer: {provider: lmstudio, model: m}\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="FAKE_LLM_KEY"):
        ProviderRegistry.discover(p)


# ---------------------------------------------------------------------------
# ProviderRegistry.build
# ---------------------------------------------------------------------------

def test_build_returns_lmstudio_adapter(yaml_with_env: Path) -> None:
    reg = ProviderRegistry.discover(yaml_with_env)
    adapter = reg.build("lmstudio", "gemma-3-27b")
    assert isinstance(adapter, LMStudioAdapter)
    assert adapter.config.model == "gemma-3-27b"
    assert adapter.config.base_url == "http://localhost:5555"
    assert adapter.config.timeout == 90
    # Strategy should be Gemma because of the model name.
    assert isinstance(adapter.strategy, GemmaToolCallStrategy)


def test_build_unknown_provider_raises(yaml_with_env: Path) -> None:
    reg = ProviderRegistry.discover(yaml_with_env)
    with pytest.raises(KeyError, match="unknown provider"):
        reg.build("does-not-exist", "any-model")


def test_build_overrides_win_over_yaml(yaml_with_env: Path) -> None:
    reg = ProviderRegistry.discover(yaml_with_env)
    adapter = reg.build(
        "lmstudio",
        "gemma-3-27b",
        timeout=999,
        base_url="http://override:1234",
    )
    assert adapter.config.timeout == 999
    assert adapter.config.base_url == "http://override:1234"


# ---------------------------------------------------------------------------
# StrategyRegistry
# ---------------------------------------------------------------------------

def test_strategy_for_model_picks_gemma() -> None:
    s = StrategyRegistry.for_model("gemma-3-27b")
    assert isinstance(s, GemmaToolCallStrategy)


def test_strategy_for_model_case_insensitive_gemma() -> None:
    s = StrategyRegistry.for_model("Google-GEMMA-3-12B")
    assert isinstance(s, GemmaToolCallStrategy)


def test_strategy_for_model_default_falls_through() -> None:
    """Until S3.2 lands a Claude strategy, claude-* falls through to the
    OpenAINative default."""
    s = StrategyRegistry.for_model("claude-3-7-sonnet")
    assert isinstance(s, OpenAINativeStrategy)


def test_strategy_for_model_arbitrary_falls_through() -> None:
    s = StrategyRegistry.for_model("some-future-model-x")
    assert isinstance(s, OpenAINativeStrategy)


def test_strategy_registry_first_match_wins() -> None:
    """A pattern registered earlier shadows one registered later."""
    # Snapshot then restore so the test is self-contained.
    saved = list(StrategyRegistry._entries)
    try:
        StrategyRegistry.clear()

        @StrategyRegistry.register(r"foo")
        class _A(GemmaToolCallStrategy):
            pass

        @StrategyRegistry.register(r"foo")
        class _B(OpenAINativeStrategy):
            pass

        assert isinstance(StrategyRegistry.for_model("foobar"), _A)
    finally:
        StrategyRegistry._entries = saved


def test_strategy_registry_empty_raises() -> None:
    """No registered patterns should raise LookupError."""
    saved = list(StrategyRegistry._entries)
    try:
        StrategyRegistry.clear()
        with pytest.raises(LookupError):
            StrategyRegistry.for_model("anything")
    finally:
        StrategyRegistry._entries = saved


# ---------------------------------------------------------------------------
# Built-in registration
# ---------------------------------------------------------------------------

def test_builtin_registry_has_lmstudio() -> None:
    """A bare ProviderRegistry knows the built-in adapter types even
    before discover() runs."""
    reg = ProviderRegistry()
    assert "lmstudio" in reg.list_adapters()


def test_register_custom_adapter() -> None:
    """register() lets external code add new adapter types."""
    reg = ProviderRegistry()

    class _FakeAdapter(LMStudioAdapter):
        pass

    reg.register("fake-adapter", _FakeAdapter)
    assert "fake-adapter" in reg.list_adapters()


def test_register_duplicate_same_class_is_noop() -> None:
    """Re-registering the same class is fine; collisions raise."""
    reg = ProviderRegistry()

    class _FakeAdapter(LMStudioAdapter):
        pass

    reg.register("fake", _FakeAdapter)
    reg.register("fake", _FakeAdapter)  # idempotent

    class _OtherAdapter(LMStudioAdapter):
        pass

    with pytest.raises(ValueError, match="already registered"):
        reg.register("fake", _OtherAdapter)
