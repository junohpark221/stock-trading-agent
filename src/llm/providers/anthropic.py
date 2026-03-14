"""Anthropic LLM provider wrapping the ``anthropic`` SDK.

Uses ``AsyncAnthropic`` for all API calls. The ``structured_output()``
method uses the tool-use trick to force structured JSON responses.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from anthropic import (
    APIError,
    APITimeoutError,
    AsyncAnthropic,
    AuthenticationError,
    RateLimitError,
)
from pydantic import BaseModel

from src.core.enums import LLMProviderType, MessageRole
from src.core.exceptions import ProviderError
from src.core.models import LLMMessage, LLMResponse, Tool, ToolCall
from src.llm.base import LLMProvider

if TYPE_CHECKING:
    from src.config import Settings

logger = structlog.get_logger(__name__)

_DEFAULT_MAX_TOKENS = 4096

# Per-million-token pricing: (input, output)
_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-4-6": (Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-4-5-20250929": (Decimal("3.00"), Decimal("15.00")),
    "claude-haiku-4-5-20251001": (Decimal("1.00"), Decimal("5.00")),
}


class AnthropicProvider(LLMProvider):
    """Anthropic LLM provider (Claude models, backup provider)."""

    def __init__(self, *, model_id: str, settings: Settings) -> None:
        self._model_id = model_id
        self._settings = settings
        self._client: AsyncAnthropic | None = None
        self._initialized = False

    # ── Identity ──────────────────────────────────────────────────────

    @property
    def provider_name(self) -> LLMProviderType:
        return LLMProviderType.ANTHROPIC

    @property
    def model_id(self) -> str:
        return self._model_id

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def initialize(self) -> None:
        if not self._settings.ANTHROPIC_API_KEY:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        self._client = AsyncAnthropic(
            api_key=self._settings.ANTHROPIC_API_KEY,
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
        system_text, converted_messages = _convert_messages(messages)

        kwargs: dict[str, Any] = {
            "model": self._model_id,
            "messages": converted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS,
        }
        if system_text:
            kwargs["system"] = system_text
        if tools:
            kwargs["tools"] = _convert_tools(tools)

        response = await self._call_with_retry(
            lambda: self._client.messages.create(**kwargs),  # type: ignore[union-attr]
        )

        # Extract content from text blocks
        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                content_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else {},
                    )
                )

        content = "".join(content_parts)

        # Map stop_reason
        finish_reason = "stop"
        if response.stop_reason == "tool_use":
            finish_reason = "tool_calls"
        elif response.stop_reason == "end_turn":
            finish_reason = "stop"
        elif response.stop_reason:
            finish_reason = response.stop_reason

        tokens_in = response.usage.input_tokens if response.usage else self._estimate_tokens(str(messages))
        tokens_out = response.usage.output_tokens if response.usage else self._estimate_tokens(content)

        return LLMResponse(
            content=content,
            model=self._model_id,
            provider=self.provider_name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=_calculate_cost(self._model_id, tokens_in, tokens_out),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    async def structured_output(
        self,
        messages: list[LLMMessage],
        schema: type[BaseModel],
        *,
        temperature: float = 0.3,
    ) -> BaseModel:
        """Use tool_use trick to force structured JSON output."""
        start = time.monotonic()
        system_text, converted_messages = _convert_messages(messages)

        tool_name = f"output_{schema.__name__}"
        tool_def = {
            "name": tool_name,
            "description": f"Output structured data as {schema.__name__}",
            "input_schema": schema.model_json_schema(),
        }

        kwargs: dict[str, Any] = {
            "model": self._model_id,
            "messages": converted_messages,
            "temperature": temperature,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "tools": [tool_def],
            "tool_choice": {"type": "tool", "name": tool_name},
        }
        if system_text:
            kwargs["system"] = system_text

        response = await self._call_with_retry(
            lambda: self._client.messages.create(**kwargs),  # type: ignore[union-attr]
        )

        # Find the tool_use block
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                try:
                    return schema.model_validate(block.input)
                except Exception as exc:
                    raise ProviderError(f"Failed to validate structured output: {exc}") from exc

        raise ProviderError(
            f"Anthropic structured_output: no tool_use block with name '{tool_name}' found"
        )

    # ── Retry logic ───────────────────────────────────────────────────

    async def _call_with_retry(self, coro_factory: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self._settings.LLM_MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except AuthenticationError as exc:
                raise ProviderError(f"Anthropic auth error: {exc}") from exc
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
                raise ProviderError(f"Anthropic API error: {exc}") from exc
            except Exception as exc:
                raise ProviderError(f"Anthropic unexpected error: {exc}") from exc

        raise ProviderError(f"Anthropic max retries exceeded: {last_exc}") from last_exc


# ── Conversion helpers (module-level) ─────────────────────────────────


def _convert_messages(
    messages: list[LLMMessage],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert LLMMessage list to Anthropic message format.

    Returns (system_text, messages). System messages are extracted into
    the top-level ``system`` parameter.
    """
    system_parts: list[str] = []
    result: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == MessageRole.SYSTEM:
            system_parts.append(msg.content)
            continue
        if msg.role == MessageRole.TOOL:
            result.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id or "",
                        "content": msg.content,
                    }
                ],
            })
            continue
        result.append({
            "role": msg.role.value,
            "content": msg.content,
        })

    system_text = "\n".join(system_parts) if system_parts else None
    return system_text, result


def _convert_tools(tools: list[Tool]) -> list[dict[str, Any]]:
    """Convert Tool list to Anthropic tool format."""
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
            "name": tool.name,
            "description": tool.description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
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
