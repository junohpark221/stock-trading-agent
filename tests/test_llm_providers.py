"""Tests for OpenAI, Google, and Anthropic LLM providers.

All SDK calls are mocked — no real API keys or network access required.
~40+ tests covering identity, lifecycle, chat, structured output, and error handling.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from src.core.enums import LLMProviderType, MessageRole
from src.core.exceptions import ProviderError
from src.core.models import LLMMessage, LLMResponse, Tool, ToolCall, ToolParameter
from src.llm.providers.anthropic import AnthropicProvider
from src.llm.providers.google import GoogleProvider
from src.llm.providers.openai import OpenAIProvider
from tests.conftest import make_settings


# ── Test helpers ──────────────────────────────────────────────────────


def _make_messages(*contents: str) -> list[LLMMessage]:
    return [LLMMessage(role=MessageRole.USER, content=c) for c in contents]


def _make_settings(**overrides: object):
    defaults = {
        "OPENAI_API_KEY": "test-openai-key",
        "GOOGLE_API_KEY": "test-google-key",
        "ANTHROPIC_API_KEY": "test-anthropic-key",
        "LLM_MAX_RETRIES": 2,
        "LLM_REQUEST_TIMEOUT": 30,
    }
    defaults.update(overrides)
    return make_settings(**defaults)


class _TestSchema(BaseModel):
    name: str
    value: int


# ═══════════════════════════════════════════════════════════════════════
# OpenAI Provider Tests
# ═══════════════════════════════════════════════════════════════════════


class TestOpenAIIdentity:
    def test_provider_name(self) -> None:
        p = OpenAIProvider(model_id="gpt-4o", settings=_make_settings())
        assert p.provider_name == LLMProviderType.OPENAI

    def test_model_id(self) -> None:
        p = OpenAIProvider(model_id="gpt-4o", settings=_make_settings())
        assert p.model_id == "gpt-4o"


class TestOpenAILifecycle:
    @pytest.mark.asyncio
    async def test_initialize_creates_client(self) -> None:
        p = OpenAIProvider(model_id="gpt-4o", settings=_make_settings())
        with patch("src.llm.providers.openai.AsyncOpenAI"):
            await p.initialize()
            assert await p.health_check() is True

    @pytest.mark.asyncio
    async def test_initialize_missing_key_raises(self) -> None:
        p = OpenAIProvider(model_id="gpt-4o", settings=_make_settings(OPENAI_API_KEY=""))
        with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
            await p.initialize()

    @pytest.mark.asyncio
    async def test_shutdown(self) -> None:
        p = OpenAIProvider(model_id="gpt-4o", settings=_make_settings())
        with patch("src.llm.providers.openai.AsyncOpenAI") as mock_cls:
            mock_cls.return_value.close = AsyncMock()
            await p.initialize()
            await p.shutdown()
            assert await p.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_before_init(self) -> None:
        p = OpenAIProvider(model_id="gpt-4o", settings=_make_settings())
        assert await p.health_check() is False


def _mock_openai_completion(
    content: str = "response",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    finish_reason: str = "stop",
    tool_calls: list | None = None,
):
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = content
    completion.choices[0].message.tool_calls = tool_calls
    completion.choices[0].finish_reason = finish_reason
    completion.usage.prompt_tokens = prompt_tokens
    completion.usage.completion_tokens = completion_tokens
    return completion


class TestOpenAIChat:
    @pytest.fixture
    def provider_and_client(self):
        settings = _make_settings()
        p = OpenAIProvider(model_id="gpt-4o", settings=settings)
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_mock_openai_completion()
        )
        mock_client.close = AsyncMock()
        p._client = mock_client
        p._initialized = True
        return p, mock_client

    @pytest.mark.asyncio
    async def test_chat_returns_response(self, provider_and_client) -> None:
        p, _ = provider_and_client
        result = await p.chat(_make_messages("Hello"))
        assert isinstance(result, LLMResponse)

    @pytest.mark.asyncio
    async def test_chat_content(self, provider_and_client) -> None:
        p, _ = provider_and_client
        result = await p.chat(_make_messages("Hello"))
        assert result.content == "response"

    @pytest.mark.asyncio
    async def test_chat_token_counts(self, provider_and_client) -> None:
        p, _ = provider_and_client
        result = await p.chat(_make_messages("Hello"))
        assert result.tokens_in == 100
        assert result.tokens_out == 50

    @pytest.mark.asyncio
    async def test_chat_cost_calculation(self, provider_and_client) -> None:
        p, _ = provider_and_client
        result = await p.chat(_make_messages("Hello"))
        # gpt-4o: input=$2.50/MTok, output=$10.00/MTok
        expected = Decimal("2.50") * 100 / 1_000_000 + Decimal("10.00") * 50 / 1_000_000
        assert result.cost_usd == expected

    @pytest.mark.asyncio
    async def test_chat_with_tools(self, provider_and_client) -> None:
        p, client = provider_and_client
        tools = [
            Tool(
                name="get_price",
                description="Get stock price",
                parameters=[ToolParameter(name="symbol", type="string", description="Stock symbol")],
            )
        ]
        await p.chat(_make_messages("check price"), tools=tools)
        call_kwargs = client.chat.completions.create.call_args
        assert "tools" in call_kwargs.kwargs

    @pytest.mark.asyncio
    async def test_chat_tool_calls_parsed(self, provider_and_client) -> None:
        p, client = provider_and_client
        tc = MagicMock()
        tc.id = "call_123"
        tc.function.name = "get_price"
        tc.function.arguments = '{"symbol": "005930"}'
        client.chat.completions.create = AsyncMock(
            return_value=_mock_openai_completion(
                tool_calls=[tc], finish_reason="tool_calls"
            )
        )
        result = await p.chat(_make_messages("check price"))
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "get_price"
        assert result.tool_calls[0].arguments == {"symbol": "005930"}
        assert result.finish_reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_chat_unknown_model_zero_cost(self) -> None:
        settings = _make_settings()
        p = OpenAIProvider(model_id="unknown-model", settings=settings)
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_mock_openai_completion()
        )
        p._client = mock_client
        p._initialized = True
        result = await p.chat(_make_messages("Hello"))
        assert result.cost_usd == Decimal("0")


class TestOpenAIStructuredOutput:
    @pytest.mark.asyncio
    async def test_structured_output_parses(self) -> None:
        settings = _make_settings()
        p = OpenAIProvider(model_id="gpt-4o", settings=settings)
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_mock_openai_completion(content='{"name": "test", "value": 42}')
        )
        p._client = mock_client
        p._initialized = True
        parsed, tokens_in, tokens_out, cost = await p.structured_output(
            _make_messages("analyze"), _TestSchema
        )
        assert isinstance(parsed, _TestSchema)
        assert parsed.name == "test"
        assert parsed.value == 42
        assert tokens_in == 100
        assert tokens_out == 50
        assert cost > Decimal("0")

    @pytest.mark.asyncio
    async def test_structured_output_invalid_raises(self) -> None:
        settings = _make_settings()
        p = OpenAIProvider(model_id="gpt-4o", settings=settings)
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_mock_openai_completion(content="not json")
        )
        p._client = mock_client
        p._initialized = True
        with pytest.raises(ProviderError, match="Failed to parse"):
            await p.structured_output(_make_messages("analyze"), _TestSchema)


class TestOpenAIErrors:
    @pytest.mark.asyncio
    async def test_auth_error_no_retry(self) -> None:
        from openai import AuthenticationError

        settings = _make_settings()
        p = OpenAIProvider(model_id="gpt-4o", settings=settings)
        mock_client = MagicMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.headers = {}
        err = AuthenticationError(
            message="invalid key",
            response=mock_resp,
            body=None,
        )
        mock_client.chat.completions.create = AsyncMock(side_effect=err)
        p._client = mock_client
        p._initialized = True
        with pytest.raises(ProviderError, match="auth error"):
            await p.chat(_make_messages("Hello"))
        # Should be called only once (no retry)
        assert mock_client.chat.completions.create.call_count == 1

    @pytest.mark.asyncio
    async def test_rate_limit_retries(self) -> None:
        from openai import RateLimitError

        settings = _make_settings(LLM_MAX_RETRIES=1)
        p = OpenAIProvider(model_id="gpt-4o", settings=settings)
        mock_client = MagicMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {}
        err = RateLimitError(
            message="rate limited",
            response=mock_resp,
            body=None,
        )
        mock_client.chat.completions.create = AsyncMock(side_effect=err)
        p._client = mock_client
        p._initialized = True

        with patch("src.llm.providers.openai.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ProviderError, match="max retries"):
                await p.chat(_make_messages("Hello"))
        # initial + 1 retry = 2
        assert mock_client.chat.completions.create.call_count == 2

    @pytest.mark.asyncio
    async def test_sdk_error_wrapped(self) -> None:
        settings = _make_settings()
        p = OpenAIProvider(model_id="gpt-4o", settings=settings)
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=ValueError("unexpected")
        )
        p._client = mock_client
        p._initialized = True
        with pytest.raises(ProviderError, match="unexpected"):
            await p.chat(_make_messages("Hello"))


# ═══════════════════════════════════════════════════════════════════════
# Google Provider Tests
# ═══════════════════════════════════════════════════════════════════════


class TestGoogleIdentity:
    def test_provider_name(self) -> None:
        p = GoogleProvider(model_id="gemini-3-flash-preview", settings=_make_settings())
        assert p.provider_name == LLMProviderType.GOOGLE

    def test_model_id(self) -> None:
        p = GoogleProvider(model_id="gemini-3-flash-preview", settings=_make_settings())
        assert p.model_id == "gemini-3-flash-preview"


class TestGoogleLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_creates_client(self) -> None:
        p = GoogleProvider(model_id="gemini-3-flash-preview", settings=_make_settings())
        with patch("src.llm.providers.google.genai.Client"):
            await p.initialize()
            assert await p.health_check() is True

    @pytest.mark.asyncio
    async def test_initialize_missing_key_raises(self) -> None:
        p = GoogleProvider(model_id="gemini-3-flash-preview", settings=_make_settings(GOOGLE_API_KEY=""))
        with pytest.raises(ProviderError, match="GOOGLE_API_KEY"):
            await p.initialize()

    @pytest.mark.asyncio
    async def test_shutdown(self) -> None:
        p = GoogleProvider(model_id="gemini-3-flash-preview", settings=_make_settings())
        with patch("src.llm.providers.google.genai.Client"):
            await p.initialize()
            await p.shutdown()
            assert await p.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_before_init(self) -> None:
        p = GoogleProvider(model_id="gemini-3-flash-preview", settings=_make_settings())
        assert await p.health_check() is False


def _mock_google_response(
    text: str = "response",
    prompt_tokens: int = 100,
    candidates_tokens: int = 50,
    function_calls: list | None = None,
):
    response = MagicMock()
    response.text = text

    # Usage metadata
    response.usage_metadata = MagicMock()
    response.usage_metadata.prompt_token_count = prompt_tokens
    response.usage_metadata.candidates_token_count = candidates_tokens

    # Candidates with parts
    candidate = MagicMock()
    parts = []
    if text:
        text_part = MagicMock()
        text_part.function_call = None
        parts.append(text_part)
    if function_calls:
        for fc in function_calls:
            fc_part = MagicMock()
            fc_part.function_call = MagicMock()
            fc_part.function_call.name = fc["name"]
            fc_part.function_call.args = fc.get("args", {})
            parts.append(fc_part)
    candidate.content = MagicMock()
    candidate.content.parts = parts
    response.candidates = [candidate]

    return response


class TestGoogleChat:
    @pytest.fixture
    def provider_and_client(self):
        settings = _make_settings()
        p = GoogleProvider(model_id="gemini-3-flash-preview", settings=settings)
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=_mock_google_response()
        )
        p._client = mock_client
        p._initialized = True
        return p, mock_client

    @pytest.mark.asyncio
    async def test_chat_returns_response(self, provider_and_client) -> None:
        p, _ = provider_and_client
        result = await p.chat(_make_messages("Hello"))
        assert isinstance(result, LLMResponse)

    @pytest.mark.asyncio
    async def test_chat_content(self, provider_and_client) -> None:
        p, _ = provider_and_client
        result = await p.chat(_make_messages("Hello"))
        assert result.content == "response"

    @pytest.mark.asyncio
    async def test_chat_token_counts(self, provider_and_client) -> None:
        p, _ = provider_and_client
        result = await p.chat(_make_messages("Hello"))
        assert result.tokens_in == 100
        assert result.tokens_out == 50

    @pytest.mark.asyncio
    async def test_chat_cost_calculation(self, provider_and_client) -> None:
        p, _ = provider_and_client
        result = await p.chat(_make_messages("Hello"))
        # gemini-3-flash-preview: input=$0.50/MTok, output=$3.00/MTok
        expected = Decimal("0.50") * 100 / 1_000_000 + Decimal("3.00") * 50 / 1_000_000
        assert result.cost_usd == expected

    @pytest.mark.asyncio
    async def test_chat_with_tools(self, provider_and_client) -> None:
        p, client = provider_and_client
        tools = [
            Tool(
                name="get_price",
                description="Get stock price",
                parameters=[ToolParameter(name="symbol", type="string", description="Stock symbol")],
            )
        ]
        await p.chat(_make_messages("check price"), tools=tools)
        call_kwargs = client.aio.models.generate_content.call_args
        config = call_kwargs.kwargs.get("config") or call_kwargs[1].get("config")
        assert config is not None

    @pytest.mark.asyncio
    async def test_chat_tool_calls_parsed(self, provider_and_client) -> None:
        p, client = provider_and_client
        client.aio.models.generate_content = AsyncMock(
            return_value=_mock_google_response(
                text="",
                function_calls=[{"name": "get_price", "args": {"symbol": "005930"}}],
            )
        )
        result = await p.chat(_make_messages("check price"))
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "google_tc_0"
        assert result.tool_calls[0].name == "get_price"
        assert result.finish_reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_chat_unknown_model_zero_cost(self) -> None:
        settings = _make_settings()
        p = GoogleProvider(model_id="unknown-model", settings=settings)
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=_mock_google_response()
        )
        p._client = mock_client
        p._initialized = True
        result = await p.chat(_make_messages("Hello"))
        assert result.cost_usd == Decimal("0")

    @pytest.mark.asyncio
    async def test_chat_system_message_extracted(self, provider_and_client) -> None:
        p, client = provider_and_client
        msgs = [
            LLMMessage(role=MessageRole.SYSTEM, content="You are helpful."),
            LLMMessage(role=MessageRole.USER, content="Hello"),
        ]
        await p.chat(msgs)
        call_kwargs = client.aio.models.generate_content.call_args
        config = call_kwargs.kwargs.get("config") or call_kwargs[1].get("config")
        assert config.system_instruction == "You are helpful."


class TestGoogleStructuredOutput:
    @pytest.mark.asyncio
    async def test_structured_output_parses(self) -> None:
        settings = _make_settings()
        p = GoogleProvider(model_id="gemini-3-flash-preview", settings=settings)
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=_mock_google_response(text='{"name": "test", "value": 42}')
        )
        p._client = mock_client
        p._initialized = True
        parsed, tokens_in, tokens_out, cost = await p.structured_output(
            _make_messages("analyze"), _TestSchema
        )
        assert isinstance(parsed, _TestSchema)
        assert parsed.name == "test"
        assert parsed.value == 42
        assert tokens_in == 100
        assert tokens_out == 50
        assert cost > Decimal("0")

    @pytest.mark.asyncio
    async def test_structured_output_invalid_raises(self) -> None:
        settings = _make_settings()
        p = GoogleProvider(model_id="gemini-3-flash-preview", settings=settings)
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=_mock_google_response(text="not json")
        )
        p._client = mock_client
        p._initialized = True
        with pytest.raises(ProviderError, match="Failed to parse"):
            await p.structured_output(_make_messages("analyze"), _TestSchema)


class TestGoogleErrors:
    @pytest.mark.asyncio
    async def test_auth_error_no_retry(self) -> None:
        from google.genai import errors as genai_errors

        settings = _make_settings()
        p = GoogleProvider(model_id="gemini-3-flash-preview", settings=settings)
        mock_client = MagicMock()

        err = genai_errors.ClientError(401, {"error": "unauthorized"})
        mock_client.aio.models.generate_content = AsyncMock(side_effect=err)
        p._client = mock_client
        p._initialized = True

        with pytest.raises(ProviderError, match="auth error"):
            await p.chat(_make_messages("Hello"))
        assert mock_client.aio.models.generate_content.call_count == 1

    @pytest.mark.asyncio
    async def test_rate_limit_retries(self) -> None:
        from google.genai import errors as genai_errors

        settings = _make_settings(LLM_MAX_RETRIES=1)
        p = GoogleProvider(model_id="gemini-3-flash-preview", settings=settings)
        mock_client = MagicMock()

        err = genai_errors.ClientError(429, {"error": "rate limited"})
        mock_client.aio.models.generate_content = AsyncMock(side_effect=err)
        p._client = mock_client
        p._initialized = True

        with patch("src.llm.providers.google.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ProviderError, match="max retries"):
                await p.chat(_make_messages("Hello"))
        assert mock_client.aio.models.generate_content.call_count == 2

    @pytest.mark.asyncio
    async def test_server_error_retries(self) -> None:
        from google.genai import errors as genai_errors

        settings = _make_settings(LLM_MAX_RETRIES=1)
        p = GoogleProvider(model_id="gemini-3-flash-preview", settings=settings)
        mock_client = MagicMock()

        err = genai_errors.ServerError(500, {"error": "internal error"})
        mock_client.aio.models.generate_content = AsyncMock(side_effect=err)
        p._client = mock_client
        p._initialized = True

        with patch("src.llm.providers.google.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ProviderError, match="max retries"):
                await p.chat(_make_messages("Hello"))
        assert mock_client.aio.models.generate_content.call_count == 2

    @pytest.mark.asyncio
    async def test_sdk_error_wrapped(self) -> None:
        settings = _make_settings()
        p = GoogleProvider(model_id="gemini-3-flash-preview", settings=settings)
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            side_effect=ValueError("unexpected")
        )
        p._client = mock_client
        p._initialized = True
        with pytest.raises(ProviderError, match="unexpected"):
            await p.chat(_make_messages("Hello"))


# ═══════════════════════════════════════════════════════════════════════
# Anthropic Provider Tests
# ═══════════════════════════════════════════════════════════════════════


class TestAnthropicIdentity:
    def test_provider_name(self) -> None:
        p = AnthropicProvider(model_id="claude-opus-4-6", settings=_make_settings())
        assert p.provider_name == LLMProviderType.ANTHROPIC

    def test_model_id(self) -> None:
        p = AnthropicProvider(model_id="claude-opus-4-6", settings=_make_settings())
        assert p.model_id == "claude-opus-4-6"


class TestAnthropicLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_creates_client(self) -> None:
        p = AnthropicProvider(model_id="claude-opus-4-6", settings=_make_settings())
        with patch("src.llm.providers.anthropic.AsyncAnthropic"):
            await p.initialize()
            assert await p.health_check() is True

    @pytest.mark.asyncio
    async def test_initialize_missing_key_raises(self) -> None:
        p = AnthropicProvider(
            model_id="claude-opus-4-6",
            settings=_make_settings(ANTHROPIC_API_KEY=""),
        )
        with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
            await p.initialize()

    @pytest.mark.asyncio
    async def test_shutdown(self) -> None:
        p = AnthropicProvider(model_id="claude-opus-4-6", settings=_make_settings())
        with patch("src.llm.providers.anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.close = AsyncMock()
            await p.initialize()
            await p.shutdown()
            assert await p.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_before_init(self) -> None:
        p = AnthropicProvider(model_id="claude-opus-4-6", settings=_make_settings())
        assert await p.health_check() is False


def _mock_anthropic_response(
    content_text: str = "response",
    input_tokens: int = 100,
    output_tokens: int = 50,
    stop_reason: str = "end_turn",
    tool_use_blocks: list | None = None,
):
    response = MagicMock()

    blocks = []
    if content_text:
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = content_text
        blocks.append(text_block)
    if tool_use_blocks:
        for tb in tool_use_blocks:
            tu_block = MagicMock()
            tu_block.type = "tool_use"
            tu_block.id = tb["id"]
            tu_block.name = tb["name"]
            tu_block.input = tb["input"]
            blocks.append(tu_block)

    response.content = blocks
    response.stop_reason = stop_reason
    response.usage = MagicMock()
    response.usage.input_tokens = input_tokens
    response.usage.output_tokens = output_tokens
    return response


class TestAnthropicChat:
    @pytest.fixture
    def provider_and_client(self):
        settings = _make_settings()
        p = AnthropicProvider(model_id="claude-opus-4-6", settings=settings)
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_anthropic_response()
        )
        mock_client.close = AsyncMock()
        p._client = mock_client
        p._initialized = True
        return p, mock_client

    @pytest.mark.asyncio
    async def test_chat_returns_response(self, provider_and_client) -> None:
        p, _ = provider_and_client
        result = await p.chat(_make_messages("Hello"))
        assert isinstance(result, LLMResponse)

    @pytest.mark.asyncio
    async def test_chat_content(self, provider_and_client) -> None:
        p, _ = provider_and_client
        result = await p.chat(_make_messages("Hello"))
        assert result.content == "response"

    @pytest.mark.asyncio
    async def test_chat_token_counts(self, provider_and_client) -> None:
        p, _ = provider_and_client
        result = await p.chat(_make_messages("Hello"))
        assert result.tokens_in == 100
        assert result.tokens_out == 50

    @pytest.mark.asyncio
    async def test_chat_cost_calculation(self, provider_and_client) -> None:
        p, _ = provider_and_client
        result = await p.chat(_make_messages("Hello"))
        # claude-opus-4-6: input=$5.00/MTok, output=$25.00/MTok
        expected = Decimal("5.00") * 100 / 1_000_000 + Decimal("25.00") * 50 / 1_000_000
        assert result.cost_usd == expected

    @pytest.mark.asyncio
    async def test_chat_with_tools(self, provider_and_client) -> None:
        p, client = provider_and_client
        tools = [
            Tool(
                name="get_price",
                description="Get stock price",
                parameters=[ToolParameter(name="symbol", type="string", description="Stock symbol")],
            )
        ]
        await p.chat(_make_messages("check price"), tools=tools)
        call_kwargs = client.messages.create.call_args
        assert "tools" in call_kwargs.kwargs

    @pytest.mark.asyncio
    async def test_chat_tool_calls_parsed(self, provider_and_client) -> None:
        p, client = provider_and_client
        client.messages.create = AsyncMock(
            return_value=_mock_anthropic_response(
                content_text="",
                stop_reason="tool_use",
                tool_use_blocks=[
                    {"id": "tu_123", "name": "get_price", "input": {"symbol": "005930"}},
                ],
            )
        )
        result = await p.chat(_make_messages("check price"))
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "tu_123"
        assert result.tool_calls[0].name == "get_price"
        assert result.tool_calls[0].arguments == {"symbol": "005930"}
        assert result.finish_reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_chat_unknown_model_zero_cost(self) -> None:
        settings = _make_settings()
        p = AnthropicProvider(model_id="unknown-model", settings=settings)
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_anthropic_response()
        )
        p._client = mock_client
        p._initialized = True
        result = await p.chat(_make_messages("Hello"))
        assert result.cost_usd == Decimal("0")

    @pytest.mark.asyncio
    async def test_max_tokens_default(self, provider_and_client) -> None:
        """max_tokens=None should use default 4096."""
        p, client = provider_and_client
        await p.chat(_make_messages("Hello"), max_tokens=None)
        call_kwargs = client.messages.create.call_args.kwargs
        assert call_kwargs["max_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_stop_reason_end_turn_maps_to_stop(self, provider_and_client) -> None:
        p, _ = provider_and_client
        result = await p.chat(_make_messages("Hello"))
        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_system_message_extracted(self, provider_and_client) -> None:
        p, client = provider_and_client
        msgs = [
            LLMMessage(role=MessageRole.SYSTEM, content="You are helpful."),
            LLMMessage(role=MessageRole.USER, content="Hello"),
        ]
        await p.chat(msgs)
        call_kwargs = client.messages.create.call_args.kwargs
        assert call_kwargs["system"] == "You are helpful."

    @pytest.mark.asyncio
    async def test_tool_message_converted(self, provider_and_client) -> None:
        p, client = provider_and_client
        msgs = [
            LLMMessage(role=MessageRole.USER, content="check price"),
            LLMMessage(
                role=MessageRole.TOOL,
                content='{"price": 70000}',
                tool_call_id="tc_1",
            ),
        ]
        await p.chat(msgs)
        call_kwargs = client.messages.create.call_args.kwargs
        tool_msg = call_kwargs["messages"][1]
        assert tool_msg["role"] == "user"
        assert tool_msg["content"][0]["type"] == "tool_result"
        assert tool_msg["content"][0]["tool_use_id"] == "tc_1"


class TestAnthropicStructuredOutput:
    @pytest.mark.asyncio
    async def test_structured_output_tool_use_trick(self) -> None:
        settings = _make_settings()
        p = AnthropicProvider(model_id="claude-opus-4-6", settings=settings)
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_anthropic_response(
                content_text="",
                stop_reason="tool_use",
                tool_use_blocks=[
                    {
                        "id": "tu_1",
                        "name": "output__TestSchema",
                        "input": {"name": "test", "value": 42},
                    },
                ],
            )
        )
        p._client = mock_client
        p._initialized = True

        parsed, tokens_in, tokens_out, cost = await p.structured_output(
            _make_messages("analyze"), _TestSchema
        )
        assert isinstance(parsed, _TestSchema)
        assert parsed.name == "test"
        assert parsed.value == 42
        assert tokens_in == 100
        assert tokens_out == 50
        assert cost > Decimal("0")

        # Verify tool_choice was set
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["tool_choice"]["type"] == "tool"
        assert call_kwargs["tool_choice"]["name"] == "output__TestSchema"

    @pytest.mark.asyncio
    async def test_structured_output_no_block_raises(self) -> None:
        settings = _make_settings()
        p = AnthropicProvider(model_id="claude-opus-4-6", settings=settings)
        mock_client = MagicMock()
        # Return response with no tool_use blocks
        mock_client.messages.create = AsyncMock(
            return_value=_mock_anthropic_response(content_text="just text")
        )
        p._client = mock_client
        p._initialized = True

        with pytest.raises(ProviderError, match="no tool_use block"):
            await p.structured_output(_make_messages("analyze"), _TestSchema)


class TestAnthropicErrors:
    @pytest.mark.asyncio
    async def test_auth_error_no_retry(self) -> None:
        from anthropic import AuthenticationError

        settings = _make_settings()
        p = AnthropicProvider(model_id="claude-opus-4-6", settings=settings)
        mock_client = MagicMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.headers = {}
        err = AuthenticationError(
            message="invalid key",
            response=mock_resp,
            body=None,
        )
        mock_client.messages.create = AsyncMock(side_effect=err)
        p._client = mock_client
        p._initialized = True

        with pytest.raises(ProviderError, match="auth error"):
            await p.chat(_make_messages("Hello"))
        assert mock_client.messages.create.call_count == 1

    @pytest.mark.asyncio
    async def test_rate_limit_retries(self) -> None:
        from anthropic import RateLimitError

        settings = _make_settings(LLM_MAX_RETRIES=1)
        p = AnthropicProvider(model_id="claude-opus-4-6", settings=settings)
        mock_client = MagicMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {}
        err = RateLimitError(
            message="rate limited",
            response=mock_resp,
            body=None,
        )
        mock_client.messages.create = AsyncMock(side_effect=err)
        p._client = mock_client
        p._initialized = True

        with patch("src.llm.providers.anthropic.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ProviderError, match="max retries"):
                await p.chat(_make_messages("Hello"))
        assert mock_client.messages.create.call_count == 2

    @pytest.mark.asyncio
    async def test_sdk_error_wrapped(self) -> None:
        settings = _make_settings()
        p = AnthropicProvider(model_id="claude-opus-4-6", settings=settings)
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            side_effect=ValueError("unexpected")
        )
        p._client = mock_client
        p._initialized = True
        with pytest.raises(ProviderError, match="unexpected"):
            await p.chat(_make_messages("Hello"))
