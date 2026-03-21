"""Central LLM routing layer: Agent → Router → Provider.

Routes agent requests to the correct LLM provider based on DB configuration
(with Redis caching). Supports escalation mode where low-confidence results
trigger re-analysis with a premium model.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import yaml
from pydantic import BaseModel, ConfigDict

from src.core.enums import LLMProviderType, RoutingMode
from src.core.exceptions import CacheError, ProviderError
from src.core.models import AgentModelConfig, LLMResponse
from src.db.models.llm import AgentModelConfigDB
from src.llm.base import LLMProvider
from src.llm.cost_tracker import CostTracker
from src.llm.providers.anthropic import AnthropicProvider
from src.llm.providers.google import GoogleProvider
from src.llm.providers.openai import OpenAIProvider

if TYPE_CHECKING:
    from sqlalchemy import select as sa_select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.config import Settings
    from src.core.models import LLMMessage, Tool
    from src.data.cache import RedisCache

logger = structlog.get_logger(__name__)

_YAML_PATH = Path(__file__).resolve().parents[2] / "config" / "default_model_assignments.yaml"

_PROVIDER_MAP: dict[str, type[LLMProvider]] = {
    "openai": OpenAIProvider,
    "google": GoogleProvider,
    "anthropic": AnthropicProvider,
}


class RoutingResult(BaseModel):
    """Result of an LLM routing operation."""

    model_config = ConfigDict(from_attributes=True)

    response: LLMResponse
    escalated: bool = False
    primary_response: LLMResponse | None = None
    config_used: AgentModelConfig


class LLMRouter:
    """Central routing layer connecting agents to LLM providers."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        cost_tracker: CostTracker,
        cache: RedisCache | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._cost_tracker = cost_tracker
        self._cache = cache
        self._providers: dict[str, LLMProvider] = {}
        self._default_configs: dict[str, dict[str, Any]] | None = None

    # ── Provider management ──────────────────────────────────────────

    @staticmethod
    def _parse_model_string(model_string: str) -> tuple[str, str]:
        """Parse "provider/model-id" → (provider_name, model_id).

        Raises ProviderError if the format is invalid.
        """
        if "/" not in model_string:
            raise ProviderError(f"Invalid model string: {model_string}")
        provider_name, model_id = model_string.split("/", 1)
        if provider_name not in _PROVIDER_MAP:
            raise ProviderError(f"Unknown provider: {provider_name}")
        return provider_name, model_id

    def _create_provider(self, provider_name: str, model_id: str) -> LLMProvider:
        """Create a new provider instance (not yet initialized)."""
        cls = _PROVIDER_MAP.get(provider_name)
        if cls is None:
            raise ProviderError(f"Unknown provider: {provider_name}")
        return cls(model_id=model_id, settings=self._settings)

    async def _get_or_create_provider(self, model_string: str) -> LLMProvider:
        """Get a cached provider or create + initialize a new one."""
        if model_string in self._providers:
            return self._providers[model_string]

        provider_name, model_id = self._parse_model_string(model_string)
        provider = self._create_provider(provider_name, model_id)
        await provider.initialize()
        self._providers[model_string] = provider
        return provider

    # ── Config management (Redis cached) ─────────────────────────────

    def _load_default_configs(self) -> dict[str, dict[str, Any]]:
        """Load and cache the YAML defaults."""
        if self._default_configs is None:
            with open(_YAML_PATH) as f:
                data = yaml.safe_load(f)
            self._default_configs = data.get("agents", {})
        return self._default_configs

    def _config_from_yaml(self, agent_type: str) -> AgentModelConfig | None:
        """Build AgentModelConfig from YAML defaults."""
        defaults = self._load_default_configs()
        cfg = defaults.get(agent_type)
        if cfg is None:
            return None
        return AgentModelConfig(
            agent_type=agent_type,
            routing_mode=cfg["routing_mode"],
            primary_model=cfg["primary_model"],
            escalation_model=cfg.get("escalation_model"),
            confidence_threshold=Decimal(str(cfg["confidence_threshold"]))
            if cfg.get("confidence_threshold")
            else None,
        )

    async def _get_agent_config(self, agent_type: str) -> AgentModelConfig:
        """Get agent config: Redis cache → DB → YAML defaults."""
        cache_ns = "llm_config"

        # 1. Try Redis cache
        if self._cache is not None:
            try:
                cached = await self._cache.get_json(cache_ns, agent_type)
                if cached is not None:
                    return AgentModelConfig.model_validate(cached)
            except CacheError:
                logger.warning("llm_config_cache_read_failed", agent_type=agent_type)

        # 2. Try DB
        from sqlalchemy import select

        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentModelConfigDB).where(
                    AgentModelConfigDB.agent_type == agent_type
                )
            )
            row = result.scalar_one_or_none()

        if row is not None:
            config = AgentModelConfig(
                agent_type=row.agent_type,
                routing_mode=row.routing_mode,
                primary_model=row.primary_model,
                escalation_model=row.escalation_model,
                confidence_threshold=row.confidence_threshold,
                is_active=row.is_active,
                updated_by=row.updated_by,
            )
            # Store in cache
            if self._cache is not None:
                try:
                    await self._cache.set_json(
                        cache_ns,
                        agent_type,
                        config.model_dump(mode="json"),
                        ttl=self._settings.LLM_CONFIG_CACHE_TTL,
                    )
                except CacheError:
                    logger.warning("llm_config_cache_write_failed", agent_type=agent_type)
            return config

        # 3. YAML defaults
        yaml_config = self._config_from_yaml(agent_type)
        if yaml_config is not None:
            return yaml_config

        raise ProviderError(f"No config found for agent: {agent_type}")

    async def _invalidate_config_cache(self, agent_type: str) -> None:
        """Invalidate Redis cache for a specific agent config."""
        if self._cache is not None:
            try:
                await self._cache.delete("llm_config", agent_type)
            except CacheError:
                logger.warning("llm_config_cache_invalidate_failed", agent_type=agent_type)

    async def _invalidate_all_config_cache(self) -> None:
        """Invalidate all agent config caches."""
        if self._cache is not None:
            try:
                await self._cache.clear_namespace("llm_config")
            except CacheError:
                logger.warning("llm_config_cache_clear_failed")

    # ── Routing ──────────────────────────────────────────────────────

    async def route(
        self,
        *,
        agent_type: str,
        messages: list[LLMMessage],
        tools: list[Tool] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        extract_confidence: Any | None = None,
    ) -> RoutingResult:
        """Route a chat request to the appropriate LLM provider.

        Args:
            agent_type: Agent identifier for config lookup.
            messages: Conversation messages.
            tools: Optional function-calling tools.
            temperature: Sampling temperature.
            max_tokens: Max response tokens.
            extract_confidence: Optional callable(LLMResponse) → float|None
                to extract confidence for escalation decisions.
        """
        config = await self._get_agent_config(agent_type)

        if not config.is_active:
            raise ProviderError(f"Agent '{agent_type}' is inactive")

        # Primary call
        provider = await self._get_or_create_provider(config.primary_model)
        response = await provider.chat(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens
        )

        # Record usage
        provider_name, _ = self._parse_model_string(config.primary_model)
        await self._cost_tracker.record_usage(
            provider=provider_name,
            model=provider.model_id,
            agent_type=agent_type,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
        )

        # Escalation check
        if (
            config.routing_mode == RoutingMode.ESCALATION
            and config.escalation_model
            and config.confidence_threshold is not None
            and extract_confidence is not None
        ):
            confidence = extract_confidence(response)
            if confidence is not None and Decimal(str(confidence)) < config.confidence_threshold:
                # Check budget before escalating
                budget = await self._cost_tracker.get_budget_status()
                if not budget.escalation_disabled:
                    esc_provider = await self._get_or_create_provider(config.escalation_model)
                    esc_response = await esc_provider.chat(
                        messages,
                        tools=tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )

                    esc_provider_name, _ = self._parse_model_string(config.escalation_model)
                    await self._cost_tracker.record_usage(
                        provider=esc_provider_name,
                        model=esc_provider.model_id,
                        agent_type=agent_type,
                        tokens_in=esc_response.tokens_in,
                        tokens_out=esc_response.tokens_out,
                        cost_usd=esc_response.cost_usd,
                        is_escalation=True,
                    )

                    # Pick higher confidence result
                    esc_confidence = extract_confidence(esc_response)
                    if esc_confidence is not None and Decimal(str(esc_confidence)) > Decimal(str(confidence)):
                        return RoutingResult(
                            response=esc_response,
                            escalated=True,
                            primary_response=response,
                            config_used=config,
                        )
                    else:
                        return RoutingResult(
                            response=response,
                            escalated=True,
                            primary_response=response,
                            config_used=config,
                        )
                else:
                    logger.warning(
                        "escalation_skipped_budget_exceeded",
                        agent_type=agent_type,
                    )

        return RoutingResult(
            response=response,
            escalated=False,
            primary_response=None,
            config_used=config,
        )

    async def route_structured(
        self,
        *,
        agent_type: str,
        messages: list[LLMMessage],
        schema: type[BaseModel],
        temperature: float = 0.3,
        confidence_field: str = "confidence",
    ) -> tuple[BaseModel, RoutingResult]:
        """Route a structured output request with automatic escalation.

        Returns (parsed_model, routing_result).
        """
        config = await self._get_agent_config(agent_type)

        if not config.is_active:
            raise ProviderError(f"Agent '{agent_type}' is inactive")

        # Primary call
        provider = await self._get_or_create_provider(config.primary_model)
        parsed, tokens_in, tokens_out, cost_usd = await provider.structured_output(
            messages, schema, temperature=temperature
        )

        # Build a synthetic LLMResponse for tracking
        content = parsed.model_dump_json()
        provider_name, _ = self._parse_model_string(config.primary_model)

        response = LLMResponse(
            content=content,
            model=provider.model_id,
            provider=LLMProviderType(provider_name),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )

        await self._cost_tracker.record_usage(
            provider=provider_name,
            model=provider.model_id,
            agent_type=agent_type,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )

        # Escalation check
        if (
            config.routing_mode == RoutingMode.ESCALATION
            and config.escalation_model
            and config.confidence_threshold is not None
        ):
            confidence_val = getattr(parsed, confidence_field, None)
            if confidence_val is not None and Decimal(str(confidence_val)) < config.confidence_threshold:
                budget = await self._cost_tracker.get_budget_status()
                if not budget.escalation_disabled:
                    esc_provider = await self._get_or_create_provider(
                        config.escalation_model
                    )
                    esc_parsed, esc_tokens_in, esc_tokens_out, esc_cost_usd = (
                        await esc_provider.structured_output(
                            messages, schema, temperature=temperature
                        )
                    )

                    esc_content = esc_parsed.model_dump_json()
                    esc_provider_name, _ = self._parse_model_string(
                        config.escalation_model
                    )

                    esc_response = LLMResponse(
                        content=esc_content,
                        model=esc_provider.model_id,
                        provider=LLMProviderType(esc_provider_name),
                        tokens_in=esc_tokens_in,
                        tokens_out=esc_tokens_out,
                        cost_usd=esc_cost_usd,
                    )

                    await self._cost_tracker.record_usage(
                        provider=esc_provider_name,
                        model=esc_provider.model_id,
                        agent_type=agent_type,
                        tokens_in=esc_tokens_in,
                        tokens_out=esc_tokens_out,
                        cost_usd=esc_cost_usd,
                        is_escalation=True,
                    )

                    esc_confidence = getattr(esc_parsed, confidence_field, None)
                    if esc_confidence is not None and Decimal(str(esc_confidence)) > Decimal(str(confidence_val)):
                        return esc_parsed, RoutingResult(
                            response=esc_response,
                            escalated=True,
                            primary_response=response,
                            config_used=config,
                        )
                    else:
                        return parsed, RoutingResult(
                            response=response,
                            escalated=True,
                            primary_response=response,
                            config_used=config,
                        )
                else:
                    logger.warning(
                        "escalation_skipped_budget_exceeded",
                        agent_type=agent_type,
                    )

        return parsed, RoutingResult(
            response=response,
            escalated=False,
            primary_response=None,
            config_used=config,
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Shut down all cached providers."""
        for model_string, provider in self._providers.items():
            try:
                await provider.shutdown()
            except Exception:
                logger.warning("provider_shutdown_failed", model=model_string, exc_info=True)
        self._providers.clear()
