"""DecisionRecorder — audit trail for all agent decisions."""

from __future__ import annotations

import uuid
from decimal import Decimal

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.exceptions import DatabaseError
from src.db.models.llm import DecisionLog

logger = structlog.get_logger(__name__)

_MAX_CHAIN_DEPTH = 20


class DecisionRecorder:
    """Records and queries agent decisions in the ``decision_log`` table.

    Every agent judgment is persisted with its reasoning, LLM context, and
    optional data snapshot so the full decision chain can be audited later.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # -- Record a new decision ------------------------------------------------

    async def record(
        self,
        *,
        session_id: uuid.UUID,
        stage: str,
        decision: str,
        reasoning: str,
        parent_id: uuid.UUID | None = None,
        agent_type: str | None = None,
        symbol: str | None = None,
        confidence: Decimal | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        llm_prompt: str | None = None,
        llm_response: str | None = None,
        llm_tokens_in: int | None = None,
        llm_tokens_out: int | None = None,
        llm_cost_usd: Decimal | None = None,
        data_snapshot: dict | None = None,
    ) -> uuid.UUID:
        """Insert a decision record and return its ``decision_id``."""
        decision_id = uuid.uuid4()
        row = DecisionLog(
            decision_id=decision_id,
            parent_id=parent_id,
            session_id=session_id,
            stage=stage,
            agent_type=agent_type,
            symbol=symbol,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_prompt=llm_prompt,
            llm_response=llm_response,
            llm_tokens_in=llm_tokens_in,
            llm_tokens_out=llm_tokens_out,
            llm_cost_usd=llm_cost_usd,
            decision=decision,
            confidence=confidence,
            reasoning=reasoning,
            data_snapshot=data_snapshot,
        )
        try:
            async with self._session_factory() as session:
                session.add(row)
                await session.commit()
        except Exception as exc:
            raise DatabaseError(f"Decision record insert failed: {exc}") from exc

        logger.info(
            "decision.recorded",
            decision_id=str(decision_id),
            session_id=str(session_id),
            stage=stage,
            decision=decision,
        )
        return decision_id

    # -- Update outcome -------------------------------------------------------

    async def update_outcome(
        self,
        decision_id: uuid.UUID,
        *,
        outcome: str,
        outcome_pnl: Decimal | None = None,
        outcome_note: str | None = None,
    ) -> bool:
        """Update the outcome fields of an existing decision.

        Returns ``True`` if the row was found and updated, ``False`` otherwise.
        """
        try:
            async with self._session_factory() as session:
                stmt = (
                    update(DecisionLog)
                    .where(DecisionLog.decision_id == decision_id)
                    .values(
                        outcome=outcome,
                        outcome_pnl=outcome_pnl,
                        outcome_note=outcome_note,
                    )
                )
                result = await session.execute(stmt)
                await session.commit()
                return result.rowcount > 0  # type: ignore[union-attr]
        except Exception as exc:
            raise DatabaseError(f"Decision outcome update failed: {exc}") from exc

    # -- Query decisions ------------------------------------------------------

    async def get_session_decisions(
        self, session_id: uuid.UUID
    ) -> list[DecisionLog]:
        """Return all decisions for a pipeline session, ordered by created_at."""
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(DecisionLog)
                    .where(DecisionLog.session_id == session_id)
                    .order_by(DecisionLog.created_at)
                )
                result = await session.execute(stmt)
                return list(result.scalars().all())
        except Exception as exc:
            raise DatabaseError(
                f"Session decisions query failed: {exc}"
            ) from exc

    async def get_decision_chain(
        self, decision_id: uuid.UUID
    ) -> list[DecisionLog]:
        """Walk the parent_id chain from *decision_id* up to root (max depth 20).

        Returns the chain in leaf → root order.  If the decision is not found,
        returns an empty list.
        """
        chain: list[DecisionLog] = []
        current_id: uuid.UUID | None = decision_id

        try:
            async with self._session_factory() as session:
                for _ in range(_MAX_CHAIN_DEPTH):
                    if current_id is None:
                        break
                    stmt = select(DecisionLog).where(
                        DecisionLog.decision_id == current_id
                    )
                    result = await session.execute(stmt)
                    row = result.scalars().first()
                    if row is None:
                        break
                    chain.append(row)
                    current_id = row.parent_id
        except Exception as exc:
            raise DatabaseError(
                f"Decision chain query failed: {exc}"
            ) from exc

        return chain
