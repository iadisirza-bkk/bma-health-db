"""LMStudio LLM adapter — connects to LMStudio's OpenAI-compatible API.

Uses Gemma 4 native tool format via the ToolCallStrategy pattern.
KEPT ASYNC — uses httpx.AsyncClient for streaming.
"""
from __future__ import annotations

import json
import logging
import time
from typing import AsyncGenerator

import httpx

from agents.adapters.base import AdapterConfig, LLMAdapter, LLMResponse
from agents.strategies.base import ToolCallStrategy

logger = logging.getLogger(__name__)

# Module-level shared client (connection pooling)
_client: httpx.AsyncClient | None = None
_health_cache: tuple[bool, float] = (False, 0.0)
_HEALTH_CACHE_TTL = 60


def _get_client(timeout: int) -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_connections=10))
    return _client


class LMStudioAdapter(LLMAdapter):
    def __init__(self, config: AdapterConfig, strategy: ToolCallStrategy):
        super().__init__(config)
        self.strategy = strategy

    async def health_check(self) -> bool:
        global _health_cache
        ok, checked_at = _health_cache
        if ok and (time.time() - checked_at) < _HEALTH_CACHE_TTL:
            return True
        try:
            client = _get_client(self.config.timeout)
            resp = await client.get(f"{self.config.base_url}/v1/models")
            is_healthy = resp.status_code == 200
            _health_cache = (is_healthy, time.time())
            return is_healthy
        except Exception:
            _health_cache = (False, time.time())
            return False

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        client = _get_client(self.config.timeout)

        if tools:
            patched_msgs, extra_params = self.strategy.inject_tools(messages, tools)
        else:
            patched_msgs, extra_params = messages, {}

        payload = {
            "model": self.config.model,
            "messages": patched_msgs,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            **extra_params,
        }

        resp = await client.post(f"{self.config.base_url}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()

        content = data["choices"][0]["message"].get("content", "") or ""
        tool_calls = self.strategy.parse_tool_calls(data) if tools else []
        content_clean = self.strategy.strip_artifacts(content) if not tool_calls else content

        return LLMResponse(
            content=content_clean,
            tool_calls=tool_calls,
            raw=data,
            finish_reason=data["choices"][0].get("finish_reason", "stop"),
        )

    async def stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        client = _get_client(self.config.timeout)

        async with client.stream(
            "POST",
            f"{self.config.base_url}/v1/chat/completions",
            json={
                "model": self.config.model,
                "messages": messages,
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
                "stream": True,
            },
        ) as resp:
            buffer = ""
            yielded_len = 0
            async for line in resp.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                try:
                    chunk = json.loads(line[6:])
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    if not delta:
                        continue
                    buffer += delta

                    # Delegate artifact stripping to strategy (no hardcoded tags)
                    cleaned = self.strategy.strip_artifacts(buffer)

                    # Yield only the NEW portion
                    if len(cleaned) > yielded_len:
                        new_text = cleaned[yielded_len:]
                        yielded_len = len(cleaned)
                        yield new_text
                    elif len(buffer) > 2000:
                        # Safety: if buffer grows huge (stuck in tag), flush
                        buffer = ""
                        yielded_len = 0

                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
