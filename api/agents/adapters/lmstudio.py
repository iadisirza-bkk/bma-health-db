"""LMStudio LLM adapter — connects to LMStudio's OpenAI-compatible API.

Uses Gemma 4 native tool format via the ToolCallStrategy pattern.
KEPT ASYNC — uses httpx.AsyncClient for streaming.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import AsyncGenerator

import httpx

from agents.adapters.base import AdapterConfig, LLMAdapter, LLMResponse
from agents.strategies.base import ToolCallStrategy

logger = logging.getLogger(__name__)

# Module-level shared client (connection pooling)
_client: httpx.AsyncClient | None = None
# Cache positive health checks briefly. Negative results are NOT cached —
# we re-probe every time so a recovering LLM is detected immediately.
# TTL is short (10s) so a healthy→dead transition propagates within ~10s
# instead of the previous 60s window.
_health_cache: tuple[bool, float] = (False, 0.0)
_HEALTH_CACHE_TTL = 10


def _get_client(timeout: int) -> httpx.AsyncClient:
    """Get the shared async HTTP client (lazy-initialised, connection-pooled).

    TLS verification:
      - Defaults to True (verify with system CA bundle).
      - Set LMSTUDIO_VERIFY_TLS=false ONLY for local dev with self-signed certs.
      - In production this MUST be true; we log a loud error if it's disabled
        in production.
    """
    global _client
    if _client is None:
        verify_env = os.getenv("LMSTUDIO_VERIFY_TLS", "true").strip().lower()
        verify = verify_env not in ("false", "0", "no")
        is_prod = os.getenv("ENVIRONMENT", "").strip().lower() == "production"
        if not verify and is_prod:
            logger.error(
                "LMSTUDIO_VERIFY_TLS is disabled in production — "
                "TLS-MITM attack surface is open. Re-enable verification."
            )
        _client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_connections=10),
            verify=verify,
        )
    return _client


def _invalidate_health_cache() -> None:
    """Force the next health_check() call to re-probe LMStudio.

    Called from chat()/stream() error paths so an outage detected during a
    real call doesn't get masked by a stale 'healthy' cache entry.
    """
    global _health_cache
    _health_cache = (False, 0.0)


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

        try:
            resp = await client.post(f"{self.config.base_url}/v1/chat/completions", json=payload)
            resp.raise_for_status()
        except (httpx.HTTPError, httpx.HTTPStatusError):
            # Real call failed → trust this signal over our cached health
            _invalidate_health_cache()
            raise
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

        try:
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
                # Detect HTTP-level failures before iterating the body, so
                # we can invalidate the health cache and surface the error.
                if resp.status_code >= 400:
                    _invalidate_health_cache()
                    raise httpx.HTTPStatusError(
                        f"LMStudio stream returned {resp.status_code}",
                        request=resp.request, response=resp,
                    )

                buffer = ""
                yielded_len = 0
                # Track parse errors — log a sampled subset for debugging
                # without flooding logs when LMStudio sends a stream of garbage.
                parse_errors = 0

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

                    except (json.JSONDecodeError, KeyError, IndexError) as e:
                        # Don't let a single bad chunk kill the whole stream,
                        # but DO surface them so a degraded LLM is visible.
                        parse_errors += 1
                        if parse_errors <= 3:
                            logger.warning(
                                "SSE parse error #%d: %s | line=%r",
                                parse_errors, type(e).__name__, line[:200],
                            )
                        elif parse_errors == 10:
                            logger.error(
                                "SSE parse errors exceeded 10 — LMStudio output is malformed"
                            )
                        continue
        except (httpx.HTTPError, httpx.HTTPStatusError):
            _invalidate_health_cache()
            raise
