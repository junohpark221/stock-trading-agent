"""Mock LLM provider for testing.

All state is held in memory — no network calls, no API keys required.
Supports response injection (queue), structured output mapping, tool call
simulation, and call history tracking.

Usage::

    async with MockLLMProvider() as llm:
        llm.enqueue_response("Buy signal detected.")
        response = await llm.chat(messages)
        assert response.content == "Buy signal detected."
        assert llm.chat_call_count == 1
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from src.core.enums import LLMProviderType
from src.core.exceptions import ProviderError
from src.core.models import LLMMessage, LLMResponse, ToolCall
from src.llm.base import LLMProvider


@dataclass
class _ChatCall:
    """Record of a single chat() invocation."""

    messages: list[LLMMessage]
    tools: list[Any] | None
    temperature: float
    max_tokens: int | None
    response: LLMResponse


class MockLLMProvider(LLMProvider):
    """Test LLM provider with response injection and call tracking.

    Args:
        model_id: Model identifier returned in responses.
        default_content: Default response content when the queue is empty.
        latency_ms: Simulated latency per call (milliseconds).
    """

    def __init__(
        self,
        *,
        model_id: str = "mock-model-v1",
        default_content: str = "Mock LLM response.",
        latency_ms: int = 0,
    ) -> None:
        self._model_id = model_id
        self._default_content = default_content
        self._latency_ms = latency_ms

        # Response queues
        self._response_queue: deque[str] = deque()
        self._tool_calls_queue: deque[list[ToolCall]] = deque()
        self._structured_responses: dict[str, BaseModel] = {}

        # Call history
        self._chat_history: list[_ChatCall] = []

        # State
        self._initialized = False

    # ── Identity ──────────────────────────────────────────────────────

    @property
    def provider_name(self) -> LLMProviderType:
        return LLMProviderType.OPENAI  # mock pretends to be openai

    @property
    def model_id(self) -> str:
        return self._model_id

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def initialize(self) -> None:
        self._initialized = True

    async def shutdown(self) -> None:
        self._initialized = False

    async def health_check(self) -> bool:
        return self._initialized

    # ── Response injection ────────────────────────────────────────────

    def enqueue_response(self, content: str) -> None:
        """Add a response to the FIFO queue (consumed by ``chat()``)."""
        self._response_queue.append(content)

    def enqueue_responses(self, contents: list[str]) -> None:
        """Add multiple responses to the queue."""
        self._response_queue.extend(contents)

    def set_default_content(self, content: str) -> None:
        """Change the default response when the queue is empty."""
        self._default_content = content

    def enqueue_tool_calls(self, calls: list[ToolCall]) -> None:
        """Add tool calls for the next ``chat()`` invocation."""
        self._tool_calls_queue.append(calls)

    def set_structured_response(self, schema: type[BaseModel], instance: BaseModel) -> None:
        """Register a response for ``structured_output()`` by schema class name."""
        self._structured_responses[schema.__name__] = instance

    # ── Call history ──────────────────────────────────────────────────

    @property
    def chat_call_count(self) -> int:
        """Total number of ``chat()`` calls made."""
        return len(self._chat_history)

    def get_last_chat_messages(self) -> list[LLMMessage]:
        """Return messages from the most recent ``chat()`` call."""
        if not self._chat_history:
            return []
        return self._chat_history[-1].messages

    def get_chat_call(self, index: int) -> _ChatCall:
        """Return the i-th ``chat()`` call record."""
        return self._chat_history[index]

    def reset_history(self) -> None:
        """Clear all queues and call history."""
        self._response_queue.clear()
        self._tool_calls_queue.clear()
        self._structured_responses.clear()
        self._chat_history.clear()

    # ── Core LLM ──────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[Any] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Return queued or default response, tracking the call."""
        if self._latency_ms > 0:
            await asyncio.sleep(self._latency_ms / 1000)

        # Determine content
        content = (
            self._response_queue.popleft()
            if self._response_queue
            else self._default_content
        )

        # Determine tool calls
        tool_calls: list[ToolCall] = []
        finish_reason = "stop"
        if self._tool_calls_queue:
            tool_calls = self._tool_calls_queue.popleft()
            finish_reason = "tool_calls"

        # Estimate tokens
        input_text = " ".join(m.content for m in messages)
        tokens_in = self._estimate_tokens(input_text)
        tokens_out = self._estimate_tokens(content)

        response = LLMResponse(
            content=content,
            model=self._model_id,
            provider=self.provider_name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=Decimal("0"),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            latency_ms=self._latency_ms,
        )

        self._chat_history.append(
            _ChatCall(
                messages=list(messages),
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                response=response,
            )
        )

        return response

    async def structured_output(
        self,
        messages: list[LLMMessage],
        schema: type[BaseModel],
        *,
        temperature: float = 0.3,
    ) -> BaseModel:
        """Return registered instance, model_construct fallback, or raise."""
        # Track via chat() for consistent history
        await self.chat(messages, temperature=temperature)

        # 1. Registered instance
        key = schema.__name__
        if key in self._structured_responses:
            return self._structured_responses[key]

        # 2. model_construct fallback (works when all fields have defaults)
        try:
            instance = schema.model_construct()
            # Validate it can serialize (catches missing required fields)
            schema.model_validate(instance.model_dump())
            return instance
        except Exception:  # noqa: BLE001
            pass

        raise ProviderError(
            f"MockLLMProvider: no structured response registered for {key} "
            f"and model_construct() failed. Use set_structured_response() first."
        )
