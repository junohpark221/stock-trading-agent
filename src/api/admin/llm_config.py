"""Admin API for LLM model configuration management.

Endpoints:
    GET  /api/admin/llm/config              — list all agent model configs
    POST /api/admin/llm/config/reset        — reset to YAML defaults
    GET  /api/admin/llm/config/{agent_type} — get agent config
    PUT  /api/admin/llm/config/{agent_type} — update agent config
    GET  /api/admin/llm/models              — available models + pricing
    GET  /api/admin/llm/usage               — usage stats with filters
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.enums import AgentType
from src.core.models import AgentModelConfig
from src.data.cache import get_cache
from src.db.models.llm import AgentModelConfigDB
from src.db.session import get_db_session

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/admin/llm", tags=["admin-llm"])

_YAML_PATH = Path(__file__).resolve().parents[3] / "config" / "default_model_assignments.yaml"


# ── Request/Response schemas ────────────────────────────────────────


class UpdateConfigRequest(BaseModel):
    """PUT /config/{agent_type} request body."""

    routing_mode: str | None = None
    primary_model: str | None = None
    escalation_model: str | None = None
    confidence_threshold: Decimal | None = None
    is_active: bool | None = None
    updated_by: str = "admin"


class ModelInfo(BaseModel):
    """Single model in the models listing."""

    provider: str
    model_id: str
    full_name: str
    input_price_per_m: str
    output_price_per_m: str


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("/config")
async def list_configs(
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    """List all agent model configurations."""
    result = await session.execute(
        select(AgentModelConfigDB).order_by(AgentModelConfigDB.agent_type)
    )
    rows = result.scalars().all()
    return [
        {
            "agent_type": r.agent_type,
            "routing_mode": r.routing_mode,
            "primary_model": r.primary_model,
            "escalation_model": r.escalation_model,
            "confidence_threshold": str(r.confidence_threshold)
            if r.confidence_threshold is not None
            else None,
            "is_active": r.is_active,
            "updated_by": r.updated_by,
        }
        for r in rows
    ]


@router.post("/config/reset")
async def reset_configs(
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Reset all agent configs to YAML defaults."""
    with open(_YAML_PATH) as f:
        data = yaml.safe_load(f)

    agents = data.get("agents", {})
    reset_count = 0

    for agent_type, cfg in agents.items():
        stmt = pg_insert(AgentModelConfigDB).values(
            agent_type=agent_type,
            routing_mode=cfg["routing_mode"],
            primary_model=cfg["primary_model"],
            escalation_model=cfg.get("escalation_model"),
            confidence_threshold=Decimal(str(cfg["confidence_threshold"]))
            if cfg.get("confidence_threshold")
            else None,
            is_active=True,
            updated_by="system",
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_agent_model_config_agent_type",
            set_={
                "routing_mode": stmt.excluded.routing_mode,
                "primary_model": stmt.excluded.primary_model,
                "escalation_model": stmt.excluded.escalation_model,
                "confidence_threshold": stmt.excluded.confidence_threshold,
                "is_active": stmt.excluded.is_active,
                "updated_by": stmt.excluded.updated_by,
            },
        )
        await session.execute(stmt)
        reset_count += 1

    await session.commit()

    # Clear all config caches
    try:
        cache = get_cache()
        await cache.clear_namespace("llm_config")
    except (RuntimeError, Exception):
        logger.warning("reset_cache_clear_failed", exc_info=True)

    return {"status": "ok", "reset_count": reset_count}


@router.get("/config/{agent_type}")
async def get_config(
    agent_type: str,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Get a specific agent's model configuration."""
    result = await session.execute(
        select(AgentModelConfigDB).where(
            AgentModelConfigDB.agent_type == agent_type
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Config not found: {agent_type}")

    return {
        "agent_type": row.agent_type,
        "routing_mode": row.routing_mode,
        "primary_model": row.primary_model,
        "escalation_model": row.escalation_model,
        "confidence_threshold": str(row.confidence_threshold)
        if row.confidence_threshold is not None
        else None,
        "is_active": row.is_active,
        "updated_by": row.updated_by,
    }


@router.put("/config/{agent_type}")
async def update_config(
    agent_type: str,
    body: UpdateConfigRequest,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Update an agent's model configuration. Invalidates Redis cache immediately."""
    result = await session.execute(
        select(AgentModelConfigDB).where(
            AgentModelConfigDB.agent_type == agent_type
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Config not found: {agent_type}")

    if body.routing_mode is not None:
        row.routing_mode = body.routing_mode
    if body.primary_model is not None:
        row.primary_model = body.primary_model
    if body.escalation_model is not None:
        row.escalation_model = body.escalation_model
    if body.confidence_threshold is not None:
        row.confidence_threshold = body.confidence_threshold
    if body.is_active is not None:
        row.is_active = body.is_active
    row.updated_by = body.updated_by

    await session.commit()

    # Invalidate cache
    try:
        cache = get_cache()
        await cache.delete("llm_config", agent_type)
    except (RuntimeError, Exception):
        logger.warning("update_cache_invalidate_failed", agent_type=agent_type)

    return {
        "agent_type": row.agent_type,
        "routing_mode": row.routing_mode,
        "primary_model": row.primary_model,
        "escalation_model": row.escalation_model,
        "confidence_threshold": str(row.confidence_threshold)
        if row.confidence_threshold is not None
        else None,
        "is_active": row.is_active,
        "updated_by": row.updated_by,
    }


@router.get("/models")
async def list_models() -> list[dict[str, str]]:
    """List all available models with pricing from provider _PRICING dicts."""
    from src.llm.providers.anthropic import _PRICING as anthropic_pricing
    from src.llm.providers.google import _PRICING as google_pricing
    from src.llm.providers.openai import _PRICING as openai_pricing

    models = []
    for provider_name, pricing in [
        ("openai", openai_pricing),
        ("google", google_pricing),
        ("anthropic", anthropic_pricing),
    ]:
        for model_id, (input_price, output_price) in pricing.items():
            models.append({
                "provider": provider_name,
                "model_id": model_id,
                "full_name": f"{provider_name}/{model_id}",
                "input_price_per_m": str(input_price),
                "output_price_per_m": str(output_price),
            })
    return models


@router.get("/usage")
async def get_usage(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    provider: str | None = Query(None),
    agent_type: str | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict]:
    """Query usage stats with optional filters."""
    from sqlalchemy import func

    from src.db.models.llm import LLMUsage

    stmt = select(
        LLMUsage.date,
        LLMUsage.provider,
        LLMUsage.model,
        LLMUsage.agent_type,
        LLMUsage.tokens_in,
        LLMUsage.tokens_out,
        LLMUsage.cost_usd,
        LLMUsage.call_count,
        LLMUsage.escalation_count,
    )
    if start_date:
        stmt = stmt.where(LLMUsage.date >= start_date)
    if end_date:
        stmt = stmt.where(LLMUsage.date <= end_date)
    if provider:
        stmt = stmt.where(LLMUsage.provider == provider)
    if agent_type:
        stmt = stmt.where(LLMUsage.agent_type == agent_type)

    stmt = stmt.order_by(LLMUsage.date.desc())
    result = await session.execute(stmt)
    rows = result.all()

    return [
        {
            "date": str(r.date),
            "provider": r.provider,
            "model": r.model,
            "agent_type": r.agent_type,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "cost_usd": str(r.cost_usd),
            "call_count": r.call_count,
            "escalation_count": r.escalation_count,
        }
        for r in rows
    ]
