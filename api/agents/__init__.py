"""BMA Health Agent System — OpenMultiAgent architecture.

Usage:
    from agents import create_orchestrator
    orchestrator = create_orchestrator()  # singleton — shared across requests
    result = await orchestrator.process("obesity overview")
"""
from __future__ import annotations

from functools import lru_cache

from agents.core.orchestrator import OpenMultiAgent


@lru_cache(maxsize=1)
def create_orchestrator() -> OpenMultiAgent:
    """Singleton factory: create orchestrator once, reuse across requests.

    Two adapters are built — one for the analyst (tool selection) and one for
    the synthesizer (final Thai prose). They use the same `LMSTUDIO_URL` but
    can target different models via `LLM_MODEL_ANALYST` / `LLM_MODEL_SYNTHESIZER`.
    Both default to `LLM_MODEL` so single-model setups stay unchanged.

    CircuitBreaker, ToolRegistry, and the adapters are shared — this is
    critical for circuit breaker state to persist across requests.
    """
    import config
    from agents.adapters.base import AdapterConfig
    from agents.adapters.lmstudio import LMStudioAdapter
    from agents.tools.registry import ToolRegistry
    from agents.strategies.gemma import GemmaToolCallStrategy
    from agents.strategies.openai_native import OpenAINativeStrategy
    from agents.core.circuit_breaker import CircuitBreaker

    def _build_adapter(model: str) -> LMStudioAdapter:
        is_gemma = "gemma" in model.lower()
        strategy = GemmaToolCallStrategy() if is_gemma else OpenAINativeStrategy()
        return LMStudioAdapter(
            config=AdapterConfig(
                base_url=config.LMSTUDIO_URL,
                model=model,
                temperature=config.LLM_TEMPERATURE,
                max_tokens=config.LLM_MAX_TOKENS,
                timeout=config.LLM_TIMEOUT,
            ),
            strategy=strategy,
        )

    analyst_adapter = _build_adapter(config.LLM_MODEL_ANALYST)
    # When both env vars resolve to the same model, reuse the adapter so health
    # checks and HTTP clients aren't duplicated for nothing.
    if config.LLM_MODEL_SYNTHESIZER == config.LLM_MODEL_ANALYST:
        synthesizer_adapter = analyst_adapter
    else:
        synthesizer_adapter = _build_adapter(config.LLM_MODEL_SYNTHESIZER)

    registry = ToolRegistry.create_default()
    cb = CircuitBreaker(
        failure_threshold=config.CIRCUIT_BREAKER_THRESHOLD,
        recovery_timeout=config.CIRCUIT_BREAKER_RECOVERY,
    )

    return OpenMultiAgent(
        analyst_adapter=analyst_adapter,
        synthesizer_adapter=synthesizer_adapter,
        registry=registry,
        circuit_breaker=cb,
    )
