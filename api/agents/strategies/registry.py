"""Strategy registry — model-aware tool-call strategy selection (ADR-02 §3).

Maps a model name to a ``ToolCallStrategy`` by regex. The first registered
pattern that matches wins, so registration order matters: the catch-all
``r".*"`` default MUST be registered last. This mirrors how Django's URL
resolver picks the first matching route.

Registration is a class decorator:

.. code-block:: python

    @StrategyRegistry.register(r"(?i)gemma")
    class GemmaToolCallStrategy: ...

    @StrategyRegistry.register(r".*")
    class OpenAINativeStrategy: ...

Lookup:

.. code-block:: python

    strategy = StrategyRegistry.for_model("gemma-3-27b")  # → GemmaToolCallStrategy()

Insertion order is preserved via ``list`` of ``(compiled_pattern, cls)``
pairs — Python guarantees ``list`` ordering, which is the registry's
contract.
"""
from __future__ import annotations

import logging
import re
from typing import Callable, List, Tuple, Type

from agents.strategies.base import ToolCallStrategy

logger = logging.getLogger("agents.strategies.registry")


class StrategyRegistry:
    """Class-level registry of (regex, strategy class) pairs.

    All operations are class-level so callers don't have to plumb a
    registry instance through every layer. The registry is effectively
    a process-wide singleton populated at import time via the
    ``@register`` decorator.
    """

    # Insertion order is the contract. Each entry is
    # (compiled_pattern, strategy_class).
    _entries: List[Tuple[re.Pattern[str], Type[ToolCallStrategy]]] = []

    @classmethod
    def register(
        cls, pattern: str
    ) -> Callable[[Type[ToolCallStrategy]], Type[ToolCallStrategy]]:
        """Decorator: associate ``pattern`` (a regex string) with the
        decorated ``ToolCallStrategy`` subclass. Returns the class
        unchanged so it can still be used directly.
        """
        compiled = re.compile(pattern)

        def _decorate(strategy_cls: Type[ToolCallStrategy]) -> Type[ToolCallStrategy]:
            cls._entries.append((compiled, strategy_cls))
            logger.debug(
                "StrategyRegistry: registered %s for pattern %r",
                strategy_cls.__name__,
                pattern,
            )
            return strategy_cls

        return _decorate

    @classmethod
    def for_model(cls, model: str) -> ToolCallStrategy:
        """Return a fresh ``ToolCallStrategy`` instance for ``model``.

        First match wins (insertion order). Raises ``LookupError`` if the
        registry is empty or nothing matches — the catch-all default
        pattern ``r".*"`` should always be registered, so a miss here
        signals a setup error.
        """
        for compiled, strategy_cls in cls._entries:
            if compiled.search(model):
                return strategy_cls()
        raise LookupError(
            f"no strategy registered for model {model!r}; did you import "
            f"agents.strategies (which performs registration)?"
        )

    @classmethod
    def list_patterns(cls) -> List[str]:
        """Return the registered patterns in insertion order. Used by
        diagnostics endpoints / tests."""
        return [p.pattern for p, _ in cls._entries]

    @classmethod
    def clear(cls) -> None:
        """Reset the registry. Tests use this to start fresh; production
        code should never call this."""
        cls._entries = []
