"""Tests for LLMRouter — routing, escalation, config caching, provider management."""

import os
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from src.core.enums import LLMProviderType, RoutingMode
from src.core.exceptions import ProviderError
from src.core.models import AgentModelConfig, LLMMessage, LLMResponse
from src.llm.cost_tracker import BudgetStatus, CostTracker
from src.llm.router import LLMRouter, RoutingResult


# ── Test schema for structured output ─────────────────────────────────


class MockAnalysis(BaseModel):
    action: str = "hold"
    confidence: Decimal = Decimal("0.80")
    reasoning: str = "test"


class LowConfidenceAnalysis(BaseModel):
    action: str = "buy"
    confidence: Decimal = Decimal("0.40")
    reasoning: str = "low confidence"


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.LLM_CONFIG_CACHE_TTL = 300
    settings.LLM_MONTHLY_BUDGET_USD = Decimal("100.00")
    settings.LLM_BUDGET_WARNING_PCT = 80
    settings.OPENAI_API_KEY = "test-key"
    settings.GOOGLE_API_KEY = "test-key"
    settings.ANTHROPIC_API_KEY = "test-key"
    settings.LLM_MAX_RETRIES = 3
    settings.LLM_REQUEST_TIMEOUT = 120
    return settings


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.fixture
def mock_session_factory(mock_session):
    factory = MagicMock()
    factory.return_value = mock_session
    return factory


@pytest.fixture
def mock_cost_tracker():
    tracker = AsyncMock(spec=CostTracker)
    tracker.record_usage = AsyncMock()
    tracker.get_budget_status = AsyncMock(
        return_value=BudgetStatus(
            current_month_cost_usd=Decimal("10.00"),
            budget_usd=Decimal("100.00"),
            remaining_usd=Decimal("90.00"),
            usage_percent=Decimal("10"),
            warning_triggered=False,
            budget_exceeded=False,
            escalation_disabled=False,
        )
    )
    return tracker


@pytest.fixture
def mock_cache():
    cache = AsyncMock()
    cache.get_json = AsyncMock(return_value=None)
    cache.set_json = AsyncMock()
    cache.delete = AsyncMock()
    cache.clear_namespace = AsyncMock()
    return cache


@pytest.fixture
def mock_response():
    return LLMResponse(
        content="Test response",
        model="gpt-4.1-mini",
        provider=LLMProviderType.OPENAI,
        tokens_in=100,
        tokens_out=50,
        cost_usd=Decimal("0.001"),
    )


@pytest.fixture
def mock_provider(mock_response):
    provider = AsyncMock()
    provider.model_id = "gpt-4.1-mini"
    provider.provider_name = LLMProviderType.OPENAI
    provider.chat = AsyncMock(return_value=mock_response)
    provider.structured_output = AsyncMock(
        return_value=(MockAnalysis(), 100, 50, Decimal("0.001"))
    )
    provider.initialize = AsyncMock()
    provider.shutdown = AsyncMock()
    return provider


@pytest.fixture
def messages():
    return [LLMMessage(role="user", content="Analyze stock")]


@pytest.fixture
def router(mock_session_factory, mock_settings, mock_cost_tracker, mock_cache):
    return LLMRouter(
        session_factory=mock_session_factory,
        settings=mock_settings,
        cost_tracker=mock_cost_tracker,
        cache=mock_cache,
    )


# ── _parse_model_string ─────────────────────────────────────────────


class TestParseModelString:
    def test_valid_openai(self):
        name, model = LLMRouter._parse_model_string("openai/gpt-4o")
        assert name == "openai"
        assert model == "gpt-4o"

    def test_valid_google(self):
        name, model = LLMRouter._parse_model_string("google/gemini-3-flash-preview")
        assert name == "google"
        assert model == "gemini-3-flash-preview"

    def test_valid_anthropic(self):
        name, model = LLMRouter._parse_model_string("anthropic/claude-opus-4-6")
        assert name == "anthropic"
        assert model == "claude-opus-4-6"

    def test_no_slash_raises(self):
        with pytest.raises(ProviderError, match="Invalid model string"):
            LLMRouter._parse_model_string("gpt-4.1-mini")

    def test_unknown_provider_raises(self):
        with pytest.raises(ProviderError, match="Unknown provider"):
            LLMRouter._parse_model_string("cohere/command-r")


# ── _get_or_create_provider ──────────────────────────────────────────


class TestGetOrCreateProvider:
    @pytest.mark.asyncio
    async def test_creates_and_caches(self, router, mock_provider):
        """First call creates, second reuses cached."""
        with patch.object(router, "_create_provider", return_value=mock_provider):
            p1 = await router._get_or_create_provider("openai/gpt-4.1-mini")
            p2 = await router._get_or_create_provider("openai/gpt-4.1-mini")
            assert p1 is p2
            mock_provider.initialize.assert_called_once()

    @pytest.mark.asyncio
    async def test_different_models_separate_instances(self, router):
        """Different model strings get separate provider instances."""
        mock_p1 = AsyncMock()
        mock_p1.initialize = AsyncMock()
        mock_p2 = AsyncMock()
        mock_p2.initialize = AsyncMock()

        call_count = 0

        def create_side_effect(name, model):
            nonlocal call_count
            call_count += 1
            return mock_p1 if call_count == 1 else mock_p2

        with patch.object(router, "_create_provider", side_effect=create_side_effect):
            p1 = await router._get_or_create_provider("openai/gpt-4.1-mini")
            p2 = await router._get_or_create_provider("openai/gpt-4o")
            assert p1 is not p2


# ── _get_agent_config ────────────────────────────────────────────────


class TestGetAgentConfig:
    @pytest.mark.asyncio
    async def test_cache_miss_then_db(self, router, mock_session, mock_cache):
        """Cache miss falls through to DB, then stores in cache."""
        mock_cache.get_json.return_value = None

        db_row = MagicMock()
        db_row.agent_type = "trader"
        db_row.routing_mode = "fixed"
        db_row.primary_model = "openai/gpt-5.2-2025-12-11"
        db_row.escalation_model = None
        db_row.confidence_threshold = None
        db_row.is_active = True
        db_row.updated_by = "system"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = db_row
        mock_session.execute.return_value = mock_result

        config = await router._get_agent_config("trader")
        assert config.agent_type == "trader"
        assert config.primary_model == "openai/gpt-5.2-2025-12-11"
        mock_cache.set_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_hit(self, router, mock_cache):
        """Cache hit returns directly without DB query."""
        mock_cache.get_json.return_value = {
            "agent_type": "trader",
            "routing_mode": "fixed",
            "primary_model": "openai/gpt-5.2-2025-12-11",
            "escalation_model": None,
            "confidence_threshold": None,
            "is_active": True,
            "updated_by": "system",
        }

        config = await router._get_agent_config("trader")
        assert config.agent_type == "trader"

    @pytest.mark.asyncio
    async def test_db_miss_falls_to_yaml(self, router, mock_session, mock_cache):
        """DB miss falls through to YAML defaults."""
        mock_cache.get_json.return_value = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        config = await router._get_agent_config("trader")
        assert config.agent_type == "trader"
        assert config.primary_model == "openai/gpt-5.2-2025-12-11"

    @pytest.mark.asyncio
    async def test_no_config_anywhere_raises(self, router, mock_session, mock_cache):
        """No config in cache, DB, or YAML raises ProviderError."""
        mock_cache.get_json.return_value = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with pytest.raises(ProviderError, match="No config found"):
            await router._get_agent_config("nonexistent_agent")

    @pytest.mark.asyncio
    async def test_cache_error_falls_through(self, router, mock_session, mock_cache):
        """Redis CacheError is caught, falls through to DB."""
        from src.core.exceptions import CacheError

        mock_cache.get_json.side_effect = CacheError("Redis down")

        db_row = MagicMock()
        db_row.agent_type = "trader"
        db_row.routing_mode = "fixed"
        db_row.primary_model = "openai/gpt-5.2-2025-12-11"
        db_row.escalation_model = None
        db_row.confidence_threshold = None
        db_row.is_active = True
        db_row.updated_by = "system"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = db_row
        mock_session.execute.return_value = mock_result

        config = await router._get_agent_config("trader")
        assert config.agent_type == "trader"


# ── route() fixed mode ───────────────────────────────────────────────


class TestRouteFixed:
    @pytest.mark.asyncio
    async def test_basic_routing(self, router, messages, mock_provider, mock_response):
        """Fixed routing: single provider call, no escalation."""
        with (
            patch.object(router, "_get_agent_config", return_value=AgentModelConfig(
                agent_type="trader",
                routing_mode=RoutingMode.FIXED,
                primary_model="openai/gpt-4.1-mini",
            )),
            patch.object(router, "_get_or_create_provider", return_value=mock_provider),
        ):
            result = await router.route(agent_type="trader", messages=messages)
            assert isinstance(result, RoutingResult)
            assert result.escalated is False
            assert result.primary_response is None
            assert result.response == mock_response

    @pytest.mark.asyncio
    async def test_inactive_agent_raises(self, router, messages):
        """Inactive agent raises ProviderError."""
        with patch.object(router, "_get_agent_config", return_value=AgentModelConfig(
            agent_type="trader",
            routing_mode=RoutingMode.FIXED,
            primary_model="openai/gpt-4.1-mini",
            is_active=False,
        )):
            with pytest.raises(ProviderError, match="inactive"):
                await router.route(agent_type="trader", messages=messages)

    @pytest.mark.asyncio
    async def test_tools_and_temperature_passed(self, router, messages, mock_provider, mock_response):
        """Tools and temperature are forwarded to provider.chat()."""
        tools = [MagicMock()]
        with (
            patch.object(router, "_get_agent_config", return_value=AgentModelConfig(
                agent_type="trader",
                routing_mode=RoutingMode.FIXED,
                primary_model="openai/gpt-4.1-mini",
            )),
            patch.object(router, "_get_or_create_provider", return_value=mock_provider),
        ):
            await router.route(
                agent_type="trader",
                messages=messages,
                tools=tools,
                temperature=0.5,
                max_tokens=500,
            )
            mock_provider.chat.assert_called_once_with(
                messages, tools=tools, temperature=0.5, max_tokens=500
            )


# ── route() escalation mode ─────────────────────────────────────────


class TestRouteEscalation:
    @pytest.mark.asyncio
    async def test_no_escalation_when_confidence_high(
        self, router, messages, mock_provider, mock_cost_tracker
    ):
        """Confidence >= threshold → no escalation."""
        high_conf_response = LLMResponse(
            content='{"confidence": 0.8}',
            model="gemini-3-flash-preview",
            provider=LLMProviderType.GOOGLE,
            tokens_in=100,
            tokens_out=50,
            cost_usd=Decimal("0.001"),
        )
        mock_provider.chat.return_value = high_conf_response

        with (
            patch.object(router, "_get_agent_config", return_value=AgentModelConfig(
                agent_type="stock_analyst",
                routing_mode=RoutingMode.ESCALATION,
                primary_model="google/gemini-3-flash-preview",
                escalation_model="openai/gpt-5.2-2025-12-11",
                confidence_threshold=Decimal("0.60"),
            )),
            patch.object(router, "_get_or_create_provider", return_value=mock_provider),
        ):
            result = await router.route(
                agent_type="stock_analyst",
                messages=messages,
                extract_confidence=lambda r: 0.8,
            )
            assert result.escalated is False
            # Only primary call, no escalation
            assert mock_provider.chat.call_count == 1

    @pytest.mark.asyncio
    async def test_escalation_when_confidence_low(self, router, messages, mock_cost_tracker):
        """Confidence < threshold → escalation to premium model."""
        primary_response = LLMResponse(
            content="low confidence",
            model="gemini-3-flash-preview",
            provider=LLMProviderType.GOOGLE,
            tokens_in=100,
            tokens_out=50,
            cost_usd=Decimal("0.001"),
        )
        esc_response = LLMResponse(
            content="high confidence from premium",
            model="gpt-5.2-2025-12-11",
            provider=LLMProviderType.OPENAI,
            tokens_in=200,
            tokens_out=100,
            cost_usd=Decimal("0.010"),
        )

        primary_provider = AsyncMock()
        primary_provider.model_id = "gemini-3-flash-preview"
        primary_provider.chat.return_value = primary_response

        esc_provider = AsyncMock()
        esc_provider.model_id = "gpt-5.2-2025-12-11"
        esc_provider.chat.return_value = esc_response

        call_count = 0

        async def get_provider(model_string):
            nonlocal call_count
            call_count += 1
            return primary_provider if call_count == 1 else esc_provider

        with (
            patch.object(router, "_get_agent_config", return_value=AgentModelConfig(
                agent_type="stock_analyst",
                routing_mode=RoutingMode.ESCALATION,
                primary_model="google/gemini-3-flash-preview",
                escalation_model="openai/gpt-5.2-2025-12-11",
                confidence_threshold=Decimal("0.60"),
            )),
            patch.object(router, "_get_or_create_provider", side_effect=get_provider),
        ):
            result = await router.route(
                agent_type="stock_analyst",
                messages=messages,
                extract_confidence=lambda r: 0.9 if r.model == "gpt-5.2-2025-12-11" else 0.4,
            )
            assert result.escalated is True
            assert result.response == esc_response
            assert result.primary_response == primary_response

    @pytest.mark.asyncio
    async def test_escalation_skipped_when_budget_exceeded(
        self, router, messages, mock_provider, mock_cost_tracker
    ):
        """Budget exceeded → escalation skipped, primary result returned."""
        mock_cost_tracker.get_budget_status.return_value = BudgetStatus(
            current_month_cost_usd=Decimal("100.00"),
            budget_usd=Decimal("100.00"),
            remaining_usd=Decimal("0"),
            usage_percent=Decimal("100"),
            warning_triggered=True,
            budget_exceeded=True,
            escalation_disabled=True,
        )

        with (
            patch.object(router, "_get_agent_config", return_value=AgentModelConfig(
                agent_type="stock_analyst",
                routing_mode=RoutingMode.ESCALATION,
                primary_model="google/gemini-3-flash-preview",
                escalation_model="openai/gpt-5.2-2025-12-11",
                confidence_threshold=Decimal("0.60"),
            )),
            patch.object(router, "_get_or_create_provider", return_value=mock_provider),
        ):
            result = await router.route(
                agent_type="stock_analyst",
                messages=messages,
                extract_confidence=lambda r: 0.3,
            )
            assert result.escalated is False
            assert mock_provider.chat.call_count == 1

    @pytest.mark.asyncio
    async def test_no_escalation_without_escalation_model(
        self, router, messages, mock_provider
    ):
        """No escalation model set → no escalation even with low confidence."""
        with (
            patch.object(router, "_get_agent_config", return_value=AgentModelConfig(
                agent_type="stock_analyst",
                routing_mode=RoutingMode.ESCALATION,
                primary_model="google/gemini-3-flash-preview",
                escalation_model=None,
                confidence_threshold=Decimal("0.60"),
            )),
            patch.object(router, "_get_or_create_provider", return_value=mock_provider),
        ):
            result = await router.route(
                agent_type="stock_analyst",
                messages=messages,
                extract_confidence=lambda r: 0.3,
            )
            assert result.escalated is False


# ── route_structured() ───────────────────────────────────────────────


class TestRouteStructured:
    @pytest.mark.asyncio
    async def test_returns_parsed_model(self, router, messages, mock_provider):
        """structured route returns parsed Pydantic model + RoutingResult."""
        with (
            patch.object(router, "_get_agent_config", return_value=AgentModelConfig(
                agent_type="stock_analyst",
                routing_mode=RoutingMode.FIXED,
                primary_model="openai/gpt-4.1-mini",
            )),
            patch.object(router, "_get_or_create_provider", return_value=mock_provider),
        ):
            parsed, result = await router.route_structured(
                agent_type="stock_analyst",
                messages=messages,
                schema=MockAnalysis,
            )
            assert isinstance(parsed, MockAnalysis)
            assert isinstance(result, RoutingResult)
            assert result.escalated is False

    @pytest.mark.asyncio
    async def test_escalation_on_low_confidence(self, router, messages, mock_cost_tracker):
        """Structured output escalation when confidence field < threshold."""
        primary_provider = AsyncMock()
        primary_provider.model_id = "gemini-3-flash-preview"
        primary_provider.structured_output = AsyncMock(
            return_value=(LowConfidenceAnalysis(), 100, 50, Decimal("0.001"))
        )

        esc_provider = AsyncMock()
        esc_provider.model_id = "gpt-5.2-2025-12-11"
        esc_provider.structured_output = AsyncMock(
            return_value=(MockAnalysis(confidence=Decimal("0.90")), 200, 80, Decimal("0.002"))
        )

        call_count = 0

        async def get_provider(model_string):
            nonlocal call_count
            call_count += 1
            return primary_provider if call_count == 1 else esc_provider

        with (
            patch.object(router, "_get_agent_config", return_value=AgentModelConfig(
                agent_type="stock_analyst",
                routing_mode=RoutingMode.ESCALATION,
                primary_model="google/gemini-3-flash-preview",
                escalation_model="openai/gpt-5.2-2025-12-11",
                confidence_threshold=Decimal("0.60"),
            )),
            patch.object(router, "_get_or_create_provider", side_effect=get_provider),
        ):
            parsed, result = await router.route_structured(
                agent_type="stock_analyst",
                messages=messages,
                schema=MockAnalysis,
            )
            assert result.escalated is True
            assert parsed.confidence == Decimal("0.90")

    @pytest.mark.asyncio
    async def test_inactive_agent_raises(self, router, messages):
        """Inactive agent raises ProviderError."""
        with patch.object(router, "_get_agent_config", return_value=AgentModelConfig(
            agent_type="trader",
            routing_mode=RoutingMode.FIXED,
            primary_model="openai/gpt-4.1-mini",
            is_active=False,
        )):
            with pytest.raises(ProviderError, match="inactive"):
                await router.route_structured(
                    agent_type="trader",
                    messages=messages,
                    schema=MockAnalysis,
                )


# ── shutdown ─────────────────────────────────────────────────────────


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shuts_down_all_providers(self, router):
        """Shutdown calls shutdown() on all cached providers."""
        p1 = AsyncMock()
        p2 = AsyncMock()
        router._providers = {"openai/gpt-4.1-mini": p1, "google/gemini-3-flash-preview": p2}

        await router.shutdown()
        p1.shutdown.assert_called_once()
        p2.shutdown.assert_called_once()
        assert len(router._providers) == 0

    @pytest.mark.asyncio
    async def test_shutdown_failure_logged(self, router):
        """Provider shutdown failure is logged but does not propagate."""
        p1 = AsyncMock()
        p1.shutdown.side_effect = Exception("Shutdown failed")
        router._providers = {"openai/gpt-4.1-mini": p1}

        # Should not raise
        await router.shutdown()
        assert len(router._providers) == 0


# ── Cache invalidation ───────────────────────────────────────────────


class TestCacheInvalidation:
    @pytest.mark.asyncio
    async def test_invalidate_single(self, router, mock_cache):
        await router._invalidate_config_cache("trader")
        mock_cache.delete.assert_called_once_with("llm_config", "trader")

    @pytest.mark.asyncio
    async def test_invalidate_all(self, router, mock_cache):
        await router._invalidate_all_config_cache()
        mock_cache.clear_namespace.assert_called_once_with("llm_config")

    @pytest.mark.asyncio
    async def test_invalidate_no_cache(self, mock_session_factory, mock_settings, mock_cost_tracker):
        """No cache configured → invalidation is a no-op."""
        router = LLMRouter(
            session_factory=mock_session_factory,
            settings=mock_settings,
            cost_tracker=mock_cost_tracker,
            cache=None,
        )
        # Should not raise
        await router._invalidate_config_cache("trader")
        await router._invalidate_all_config_cache()


# ── RoutingResult model ──────────────────────────────────────────────


class TestRoutingResult:
    def test_serialization(self):
        response = LLMResponse(
            content="test",
            model="gpt-4.1-mini",
            provider=LLMProviderType.OPENAI,
            tokens_in=100,
            tokens_out=50,
            cost_usd=Decimal("0.001"),
        )
        result = RoutingResult(
            response=response,
            escalated=False,
            config_used=AgentModelConfig(
                agent_type="trader",
                routing_mode=RoutingMode.FIXED,
                primary_model="openai/gpt-4.1-mini",
            ),
        )
        data = result.model_dump()
        assert data["escalated"] is False
        assert data["primary_response"] is None
