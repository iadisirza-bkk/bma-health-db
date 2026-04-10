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

    CircuitBreaker, ToolRegistry, and LMStudioAdapter are shared —
    this is critical for circuit breaker state to persist across requests.
    """
    import config
    from agents.adapters.base import AdapterConfig
    from agents.adapters.lmstudio import LMStudioAdapter
    from agents.tools.registry import ToolRegistry
    from agents.strategies.gemma import GemmaToolCallStrategy
    from agents.strategies.openai_native import OpenAINativeStrategy
    from agents.core.circuit_breaker import CircuitBreaker

    is_gemma = "gemma" in config.LLM_MODEL.lower()
    strategy = GemmaToolCallStrategy() if is_gemma else OpenAINativeStrategy()

    adapter = LMStudioAdapter(
        config=AdapterConfig(
            base_url=config.LMSTUDIO_URL,
            model=config.LLM_MODEL,
            temperature=config.LLM_TEMPERATURE,
            max_tokens=config.LLM_MAX_TOKENS,
            timeout=config.LLM_TIMEOUT,
        ),
        strategy=strategy,
    )

    registry = ToolRegistry.create_default()
    cb = CircuitBreaker(
        failure_threshold=config.CIRCUIT_BREAKER_THRESHOLD,
        recovery_timeout=config.CIRCUIT_BREAKER_RECOVERY,
    )

    return OpenMultiAgent(adapter=adapter, registry=registry, circuit_breaker=cb)
