"""OpenAI LLM provider wrapping the ``openai`` SDK.

Uses ``AsyncOpenAI`` for all API calls. Implements retry logic with
exponential backoff for rate-limit and transient errors.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from openai import (
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)
from pydantic import BaseModel

from src.core.enums import LLMProviderType
from src.core.exceptions import ProviderError
from src.core.models import LLMMessage, LLMResponse, Tool, ToolCall
from src.llm.base import LLMProvider

if TYPE_CHECKING:
    from src.config import Settings

logger = structlog.get_logger(__name__)

# Per-million-token pricing: (input, output)
_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "o3-deep-research": (Decimal("2.00"), Decimal("8.00")),
    "o4-mini-deep-research": (Decimal("1.00"), Decimal("4.00")),
    "gpt-5.4": (Decimal("1.25"), Decimal("10.00")),
    "gpt-5.4-pro": (Decimal("5.00"), Decimal("25.00")),
    "gpt-5-mini": (Decimal("0.25"), Decimal("2.00")),
    "gpt-5-nano": (Decimal("0.05"), Decimal("0.40")),
}


class OpenAIProvider(LLMProvider):
    """OpenAI LLM provider (GPT-First strategy primary provider)."""

    def __init__(self, *, model_id: str, settings: Settings) -> None:
        self._model_id = model_id
        self._settings = settings
        self._client: AsyncOpenAI | None = None
        self._initialized = False

    # ── Identity ──────────────────────────────────────────────────────

    @property
    def provider_name(self) -> LLMProviderType:
        return LLMProviderType.OPENAI

    @property
    def model_id(self) -> str:
        return self._model_id

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def initialize(self) -> None:
        if not self._settings.OPENAI_API_KEY:
            raise ProviderError("OPENAI_API_KEY is not set")
        self._client = AsyncOpenAI(
            api_key=self._settings.OPENAI_API_KEY,
            timeout=float(self._settings.LLM_REQUEST_TIMEOUT),
        )
        self._initialized = True

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._initialized = False

    async def health_check(self) -> bool:
        return self._initialized

    # ── Core LLM ──────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[Tool] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        start = time.monotonic()
        kwargs: dict[str, Any] = {
            "model": self._model_id,
            "messages": _convert_messages(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = _convert_tools(tools)

        completion = await self._call_with_retry(
            lambda: self._client.chat.completions.create(**kwargs),  # type: ignore[union-attr]
        )

        choice = completion.choices[0]
        content = choice.message.content or ""
        tool_calls: list[ToolCall] = []
        if choice.message.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=json.loads(tc.function.arguments),
                )
                for tc in choice.message.tool_calls
            ]

        tokens_in = completion.usage.prompt_tokens if completion.usage else self._estimate_tokens(str(messages))
        tokens_out = completion.usage.completion_tokens if completion.usage else self._estimate_tokens(content)

        return LLMResponse(
            content=content,
            model=self._model_id,
            provider=self.provider_name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=_calculate_cost(self._model_id, tokens_in, tokens_out),
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    async def structured_output(
        self,
        messages: list[LLMMessage],
        schema: type[BaseModel],
        *,
        temperature: float = 0.3,
    ) -> BaseModel:
        start = time.monotonic()
        kwargs: dict[str, Any] = {
            "model": self._model_id,
            "messages": _convert_messages(messages),
            "temperature": temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": True,
                },
            },
        }

        completion = await self._call_with_retry(
            lambda: self._client.chat.completions.create(**kwargs),  # type: ignore[union-attr]
        )

        content = completion.choices[0].message.content or ""
        try:
            return schema.model_validate_json(content)
        except Exception as exc:
            raise ProviderError(f"Failed to parse structured output: {exc}") from exc

    # ── Retry logic ───────────────────────────────────────────────────

    async def _call_with_retry(self, coro_factory: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self._settings.LLM_MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except AuthenticationError as exc:
                raise ProviderError(f"OpenAI auth error: {exc}") from exc
            except RateLimitError as exc:
                last_exc = exc
                if attempt < self._settings.LLM_MAX_RETRIES:
                    await asyncio.sleep(2**attempt)
                    continue
            except APITimeoutError as exc:
                last_exc = exc
                if attempt < self._settings.LLM_MAX_RETRIES:
                    await asyncio.sleep(2**attempt)
                    continue
            except APIError as exc:
                if exc.status_code and exc.status_code >= 500:
                    last_exc = exc
                    if attempt < self._settings.LLM_MAX_RETRIES:
                        await asyncio.sleep(2**attempt)
                        continue
                raise ProviderError(f"OpenAI API error: {exc}") from exc
            except Exception as exc:
                raise ProviderError(f"OpenAI unexpected error: {exc}") from exc

        raise ProviderError(f"OpenAI max retries exceeded: {last_exc}") from last_exc


# ── Conversion helpers (module-level) ─────────────────────────────────


def _convert_messages(messages: list[LLMMessage]) -> list[dict[str, Any]]:
    """Convert LLMMessage list to OpenAI message format."""
    result = []
    for msg in messages:
        d: dict[str, Any] = {"role": msg.role.value, "content": msg.content}
        if msg.name:
            d["name"] = msg.name
        if msg.tool_call_id:
            d["tool_call_id"] = msg.tool_call_id
        result.append(d)
    return result


def _convert_tools(tools: list[Tool]) -> list[dict[str, Any]]:
    """Convert Tool list to OpenAI function-calling format."""
    result = []
    for tool in tools:
        properties = {}
        required = []
        for param in tool.parameters:
            prop: dict[str, Any] = {"type": param.type, "description": param.description}
            if param.enum:
                prop["enum"] = param.enum
            properties[param.name] = prop
            if param.required:
                required.append(param.name)
        result.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        })
    return result


def _calculate_cost(model_id: str, tokens_in: int, tokens_out: int) -> Decimal:
    """Calculate cost based on pricing table."""
    rates = _PRICING.get(model_id)
    if rates is None:
        logger.warning("unknown_model_pricing", model_id=model_id)
        return Decimal("0")
    return rates[0] * tokens_in / 1_000_000 + rates[1] * tokens_out / 1_000_000
