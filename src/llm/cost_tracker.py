"""LLM cost tracking and monthly budget management.

Tracks per-model, per-agent daily usage via the ``llm_usage`` table.
Provides budget status checks to gate escalation when costs exceed limits.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.db.models.llm import LLMUsage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.config import Settings

logger = structlog.get_logger(__name__)

# Sentinel for agent_type when None (PostgreSQL NULL unique constraint issue)
_SYSTEM_SENTINEL = "__system__"


class BudgetStatus(BaseModel):
    """Monthly LLM budget status."""

    model_config = ConfigDict(from_attributes=True)

    current_month_cost_usd: Decimal
    budget_usd: Decimal
    remaining_usd: Decimal
    usage_percent: Decimal
    warning_triggered: bool
    budget_exceeded: bool
    escalation_disabled: bool


class CostTracker:
    """Daily LLM usage aggregation and budget enforcement."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def record_usage(
        self,
        *,
        provider: str,
        model: str,
        agent_type: str | None,
        tokens_in: int,
        tokens_out: int,
        cost_usd: Decimal,
        is_escalation: bool = False,
    ) -> None:
        """INSERT ON CONFLICT DO UPDATE for daily upsert into llm_usage.

        Best-effort: DB failure is logged but does not block the caller.
        """
        effective_agent = agent_type or _SYSTEM_SENTINEL
        today = date.today()

        try:
            async with self._session_factory() as session:
                stmt = pg_insert(LLMUsage).values(
                    date=today,
                    provider=provider,
                    model=model,
                    agent_type=effective_agent,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost_usd,
                    call_count=1,
                    escalation_count=1 if is_escalation else 0,
                )
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_llm_usage_daily",
                    set_={
                        "tokens_in": LLMUsage.tokens_in + stmt.excluded.tokens_in,
                        "tokens_out": LLMUsage.tokens_out + stmt.excluded.tokens_out,
                        "cost_usd": LLMUsage.cost_usd + stmt.excluded.cost_usd,
                        "call_count": LLMUsage.call_count + stmt.excluded.call_count,
                        "escalation_count": LLMUsage.escalation_count
                        + stmt.excluded.escalation_count,
                    },
                )
                await session.execute(stmt)
                await session.commit()
        except Exception:
            logger.warning("cost_tracker_record_failed", exc_info=True)

    async def get_budget_status(self) -> BudgetStatus:
        """Current month SUM(cost_usd) vs budget. 80% warning, 100% escalation off."""
        today = date.today()
        first_of_month = today.replace(day=1)
        budget = self._settings.LLM_MONTHLY_BUDGET_USD

        async with self._session_factory() as session:
            result = await session.execute(
                select(func.coalesce(func.sum(LLMUsage.cost_usd), Decimal("0"))).where(
                    LLMUsage.date >= first_of_month
                )
            )
            current_cost = result.scalar_one()

        remaining = max(budget - current_cost, Decimal("0"))
        usage_pct = (
            (current_cost / budget * 100) if budget > 0 else Decimal("0")
        )
        warning_pct = Decimal(self._settings.LLM_BUDGET_WARNING_PCT)

        return BudgetStatus(
            current_month_cost_usd=current_cost,
            budget_usd=budget,
            remaining_usd=remaining,
            usage_percent=usage_pct,
            warning_triggered=usage_pct >= warning_pct,
            budget_exceeded=usage_pct >= Decimal("100"),
            escalation_disabled=usage_pct >= Decimal("100"),
        )

    async def get_usage_stats(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        provider: str | None = None,
        agent_type: str | None = None,
    ) -> list[dict]:
        """Filter-based usage statistics query."""
        async with self._session_factory() as session:
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
                effective = agent_type if agent_type != "__system__" else _SYSTEM_SENTINEL
                stmt = stmt.where(LLMUsage.agent_type == effective)

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

    async def get_monthly_summary(self, year: int, month: int) -> dict:
        """Monthly summary: total cost, call count, breakdown by provider."""
        first_day = date(year, month, 1)
        if month == 12:
            last_day = date(year + 1, 1, 1)
        else:
            last_day = date(year, month + 1, 1)

        async with self._session_factory() as session:
            # Total aggregates
            total_result = await session.execute(
                select(
                    func.coalesce(func.sum(LLMUsage.cost_usd), Decimal("0")).label(
                        "total_cost"
                    ),
                    func.coalesce(func.sum(LLMUsage.call_count), 0).label(
                        "total_calls"
                    ),
                    func.coalesce(func.sum(LLMUsage.tokens_in), 0).label(
                        "total_tokens_in"
                    ),
                    func.coalesce(func.sum(LLMUsage.tokens_out), 0).label(
                        "total_tokens_out"
                    ),
                    func.coalesce(func.sum(LLMUsage.escalation_count), 0).label(
                        "total_escalations"
                    ),
                ).where(LLMUsage.date >= first_day, LLMUsage.date < last_day)
            )
            totals = total_result.one()

            # Per-provider breakdown
            provider_result = await session.execute(
                select(
                    LLMUsage.provider,
                    func.sum(LLMUsage.cost_usd).label("cost"),
                    func.sum(LLMUsage.call_count).label("calls"),
                )
                .where(LLMUsage.date >= first_day, LLMUsage.date < last_day)
                .group_by(LLMUsage.provider)
            )
            providers = {
                r.provider: {"cost_usd": str(r.cost), "call_count": r.calls}
                for r in provider_result.all()
            }

        return {
            "year": year,
            "month": month,
            "total_cost_usd": str(totals.total_cost),
            "total_calls": totals.total_calls,
            "total_tokens_in": totals.total_tokens_in,
            "total_tokens_out": totals.total_tokens_out,
            "total_escalations": totals.total_escalations,
            "by_provider": providers,
        }
