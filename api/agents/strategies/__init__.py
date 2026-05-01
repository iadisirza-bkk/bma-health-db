"""Strategy package — register all built-in tool-call strategies.

Registration happens here (instead of inside each strategy module) so
that ``agents.strategies.gemma`` and ``agents.strategies.openai_native``
stay free of registry imports — they remain pure strategy implementations.
The trade-off: anyone consuming ``StrategyRegistry`` MUST import
``agents.strategies`` (or anything inside it) at least once, which is
always true in practice because the registry is consulted via
``ProviderRegistry.build`` whose call site has already imported the
strategies package transitively.

Order matters — first match wins, so the catch-all default goes LAST.
"""
from __future__ import annotations

from agents.strategies.gemma import GemmaToolCallStrategy
from agents.strategies.openai_native import OpenAINativeStrategy
from agents.strategies.registry import StrategyRegistry

# Register Gemma first (specific) so a model name like "gemma-3-27b" wins
# over the catch-all below.
StrategyRegistry.register(r"(?i)gemma")(GemmaToolCallStrategy)

# Catch-all default — MUST be registered last.
StrategyRegistry.register(r".*")(OpenAINativeStrategy)


__all__ = [
    "GemmaToolCallStrategy",
    "OpenAINativeStrategy",
    "StrategyRegistry",
]
