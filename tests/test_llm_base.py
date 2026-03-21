"""Tests for LLMProvider ABC and MockLLMProvider.

~30 tests covering ABC contract, lifecycle, chat(), structured_output(),
response injection, tool calls, call history, and context manager.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.enums import LLMProviderType, MessageRole
from src.core.exceptions import ProviderError
from src.core.models import LLMMessage, LLMResponse, ToolCall
from src.llm.base import LLMProvider
from src.llm.providers.mock import MockLLMProvider

# ── Test helpers ──────────────────────────────────────────────────────

from pydantic import BaseModel


def _make_messages(*contents: str) -> list[LLMMessage]:
    """Create USER-role LLMMessage list from content strings."""
    return [
        LLMMessage(role=MessageRole.USER, content=c)
        for c in contents
    ]


def _make_response(content: str = "test", **kw: object) -> LLMResponse:
    """Create a zero-cost LLMResponse with sensible defaults."""
    defaults = {
        "model": "mock-model-v1",
        "provider": LLMProviderType.OPENAI,
        "tokens_in": 1,
        "tokens_out": 1,
        "cost_usd": Decimal("0"),
    }
    defaults.update(kw)
    return LLMResponse(content=content, **defaults)  # type: ignore[arg-type]


class _SimpleSchema(BaseModel):
    """Schema with all-default fields for model_construct fallback testing."""
    name: str = "default"
    value: int = 0


class _RequiredSchema(BaseModel):
    """Schema with required fields (no defaults) — model_construct will fail."""
    name: str
    value: int


# ── ABC contract ──────────────────────────────────────────────────────


class TestABCContract:
    def test_cannot_instantiate_abc(self) -> None:
        with pytest.raises(TypeError):
            LLMProvider()  # type: ignore[abstract]

    def test_incomplete_subclass_raises(self) -> None:
        class Incomplete(LLMProvider):
            pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_estimate_tokens(self) -> None:
        assert LLMProvider._estimate_tokens("") == 1  # min 1
        assert LLMProvider._estimate_tokens("abcd") == 1
        assert LLMProvider._estimate_tokens("a" * 100) == 25


# ── Lifecycle ─────────────────────────────────────────────────────────


class TestLifecycle:
    @pytest.fixture
    def provider(self) -> MockLLMProvider:
        return MockLLMProvider()

    @pytest.mark.asyncio
    async def test_initialize_enables_health(self, provider: MockLLMProvider) -> None:
        assert await provider.health_check() is False
        await provider.initialize()
        assert await provider.health_check() is True

    @pytest.mark.asyncio
    async def test_shutdown_disables_health(self, provider: MockLLMProvider) -> None:
        await provider.initialize()
        await provider.shutdown()
        assert await provider.health_check() is False

    def test_provider_name(self, provider: MockLLMProvider) -> None:
        assert provider.provider_name == LLMProviderType.OPENAI

    def test_model_id(self, provider: MockLLMProvider) -> None:
        assert provider.model_id == "mock-model-v1"

    def test_custom_model_id(self) -> None:
        p = MockLLMProvider(model_id="custom-v2")
        assert p.model_id == "custom-v2"


# ── chat() basic ──────────────────────────────────────────────────────


class TestChatBasic:
    @pytest.fixture
    def provider(self) -> MockLLMProvider:
        return MockLLMProvider()

    @pytest.mark.asyncio
    async def test_returns_llm_response(self, provider: MockLLMProvider) -> None:
        msgs = _make_messages("Hello")
        result = await provider.chat(msgs)
        assert isinstance(result, LLMResponse)

    @pytest.mark.asyncio
    async def test_default_content(self, provider: MockLLMProvider) -> None:
        msgs = _make_messages("Hello")
        result = await provider.chat(msgs)
        assert result.content == "Mock LLM response."

    @pytest.mark.asyncio
    async def test_cost_is_zero(self, provider: MockLLMProvider) -> None:
        msgs = _make_messages("Hello")
        result = await provider.chat(msgs)
        assert result.cost_usd == Decimal("0")

    @pytest.mark.asyncio
    async def test_tokens_positive(self, provider: MockLLMProvider) -> None:
        msgs = _make_messages("Hello world test message")
        result = await provider.chat(msgs)
        assert result.tokens_in > 0
        assert result.tokens_out > 0

    @pytest.mark.asyncio
    async def test_default_finish_reason(self, provider: MockLLMProvider) -> None:
        msgs = _make_messages("Hello")
        result = await provider.chat(msgs)
        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_model_in_response(self, provider: MockLLMProvider) -> None:
        msgs = _make_messages("Hello")
        result = await provider.chat(msgs)
        assert result.model == "mock-model-v1"

    @pytest.mark.asyncio
    async def test_provider_in_response(self, provider: MockLLMProvider) -> None:
        msgs = _make_messages("Hello")
        result = await provider.chat(msgs)
        assert result.provider == LLMProviderType.OPENAI


# ── structured_output() ──────────────────────────────────────────────


class TestStructuredOutput:
    @pytest.fixture
    def provider(self) -> MockLLMProvider:
        return MockLLMProvider()

    @pytest.mark.asyncio
    async def test_registered_response(self, provider: MockLLMProvider) -> None:
        instance = _SimpleSchema(name="test", value=42)
        provider.set_structured_response(_SimpleSchema, instance)
        parsed, tokens_in, tokens_out, cost = await provider.structured_output(
            _make_messages("analyze"), _SimpleSchema
        )
        assert parsed.name == "test"
        assert parsed.value == 42
        assert tokens_in > 0
        assert tokens_out > 0

    @pytest.mark.asyncio
    async def test_model_construct_fallback(self, provider: MockLLMProvider) -> None:
        parsed, tokens_in, tokens_out, cost = await provider.structured_output(
            _make_messages("analyze"), _SimpleSchema
        )
        assert isinstance(parsed, _SimpleSchema)
        assert parsed.name == "default"

    @pytest.mark.asyncio
    async def test_required_schema_raises(self, provider: MockLLMProvider) -> None:
        with pytest.raises(ProviderError, match="no structured response registered"):
            await provider.structured_output(_make_messages("analyze"), _RequiredSchema)

    @pytest.mark.asyncio
    async def test_tracks_in_history(self, provider: MockLLMProvider) -> None:
        provider.set_structured_response(_SimpleSchema, _SimpleSchema())
        await provider.structured_output(_make_messages("analyze"), _SimpleSchema)
        assert provider.chat_call_count == 1


# ── Response injection ────────────────────────────────────────────────


class TestResponseInjection:
    @pytest.fixture
    def provider(self) -> MockLLMProvider:
        return MockLLMProvider()

    @pytest.mark.asyncio
    async def test_single_enqueue(self, provider: MockLLMProvider) -> None:
        provider.enqueue_response("custom response")
        result = await provider.chat(_make_messages("Hi"))
        assert result.content == "custom response"

    @pytest.mark.asyncio
    async def test_fifo_order(self, provider: MockLLMProvider) -> None:
        provider.enqueue_response("first")
        provider.enqueue_response("second")
        r1 = await provider.chat(_make_messages("1"))
        r2 = await provider.chat(_make_messages("2"))
        assert r1.content == "first"
        assert r2.content == "second"

    @pytest.mark.asyncio
    async def test_queue_exhausted_returns_default(self, provider: MockLLMProvider) -> None:
        provider.enqueue_response("only one")
        await provider.chat(_make_messages("1"))
        result = await provider.chat(_make_messages("2"))
        assert result.content == "Mock LLM response."

    @pytest.mark.asyncio
    async def test_set_default_content(self, provider: MockLLMProvider) -> None:
        provider.set_default_content("new default")
        result = await provider.chat(_make_messages("Hi"))
        assert result.content == "new default"

    @pytest.mark.asyncio
    async def test_batch_enqueue(self, provider: MockLLMProvider) -> None:
        provider.enqueue_responses(["a", "b", "c"])
        results = [await provider.chat(_make_messages(str(i))) for i in range(3)]
        assert [r.content for r in results] == ["a", "b", "c"]


# ── Tool calls ────────────────────────────────────────────────────────


class TestToolCalls:
    @pytest.fixture
    def provider(self) -> MockLLMProvider:
        return MockLLMProvider()

    @pytest.mark.asyncio
    async def test_enqueue_tool_calls(self, provider: MockLLMProvider) -> None:
        calls = [ToolCall(id="tc_1", name="get_price", arguments={"symbol": "005930"})]
        provider.enqueue_tool_calls(calls)
        result = await provider.chat(_make_messages("check price"))
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "get_price"

    @pytest.mark.asyncio
    async def test_tool_calls_finish_reason(self, provider: MockLLMProvider) -> None:
        calls = [ToolCall(id="tc_1", name="get_price", arguments={})]
        provider.enqueue_tool_calls(calls)
        result = await provider.chat(_make_messages("check"))
        assert result.finish_reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_default_no_tool_calls(self, provider: MockLLMProvider) -> None:
        result = await provider.chat(_make_messages("Hi"))
        assert result.tool_calls == []
        assert result.finish_reason == "stop"


# ── Call history ──────────────────────────────────────────────────────


class TestCallHistory:
    @pytest.fixture
    def provider(self) -> MockLLMProvider:
        return MockLLMProvider()

    @pytest.mark.asyncio
    async def test_call_count(self, provider: MockLLMProvider) -> None:
        assert provider.chat_call_count == 0
        await provider.chat(_make_messages("1"))
        await provider.chat(_make_messages("2"))
        assert provider.chat_call_count == 2

    @pytest.mark.asyncio
    async def test_get_last_messages(self, provider: MockLLMProvider) -> None:
        await provider.chat(_make_messages("first"))
        await provider.chat(_make_messages("second"))
        last = provider.get_last_chat_messages()
        assert len(last) == 1
        assert last[0].content == "second"

    @pytest.mark.asyncio
    async def test_get_last_messages_empty(self, provider: MockLLMProvider) -> None:
        assert provider.get_last_chat_messages() == []

    @pytest.mark.asyncio
    async def test_parameters_recorded(self, provider: MockLLMProvider) -> None:
        await provider.chat(_make_messages("test"), temperature=0.5, max_tokens=100)
        call = provider.get_chat_call(0)
        assert call.temperature == 0.5
        assert call.max_tokens == 100

    @pytest.mark.asyncio
    async def test_reset_history(self, provider: MockLLMProvider) -> None:
        provider.enqueue_response("queued")
        provider.set_structured_response(_SimpleSchema, _SimpleSchema())
        provider.enqueue_tool_calls([ToolCall(id="tc", name="fn", arguments={})])
        await provider.chat(_make_messages("1"))
        provider.reset_history()
        assert provider.chat_call_count == 0
        assert provider.get_last_chat_messages() == []
        # Queue should be cleared too
        result = await provider.chat(_make_messages("2"))
        assert result.content == "Mock LLM response."


# ── Context manager ───────────────────────────────────────────────────


class TestContextManager:
    @pytest.mark.asyncio
    async def test_aenter_initializes(self) -> None:
        provider = MockLLMProvider()
        async with provider as p:
            assert p is provider
            assert await provider.health_check() is True

    @pytest.mark.asyncio
    async def test_aexit_shuts_down(self) -> None:
        provider = MockLLMProvider()
        async with provider:
            pass
        assert await provider.health_check() is False
