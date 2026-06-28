"""TradeDecisionQueueManager — 진입 결정 큐 접근 모듈.

개장 전 결정 잡(08:30)이 BUY 결정을 ``enqueue``로 적재하고, 개장 후 실행
드레인 잡이 ``get_pending``으로 꺼내 당일가/갭 게이트를 통과한 건만 발주한 뒤
``mark_executed``/``mark_expired``로 전이한다.

이 코드베이스는 별도 repository 계층이 없고 ``async_sessionmaker`` 세션 팩토리를
직접 쓰는 패턴(``KISDataProvider``, ``AgentMemoryManager``)을 따른다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy import select, update

from src.core.exceptions import DatabaseError
from src.core.models import TradeDecision
from src.db.models.execution import TradeDecisionQueue

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")


@dataclass(frozen=True)
class PendingDecision:
    """실행 드레인이 소비하는 pending 큐 항목(세션 detach 안전)."""

    id: int
    account_id: str
    symbol: str
    reference_price: Decimal
    strategy_type: str
    session_id: UUID | None
    decision: TradeDecision
    entry_analysis_snapshot: dict | None
    expires_at: datetime | None = None


class TradeDecisionQueueManager:
    """trade_decision_queue 테이블 CRUD 관리자."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def enqueue(
        self,
        *,
        account_id: str,
        decision: TradeDecision,
        session_id: UUID | None,
        strategy_type: str,
        entry_snapshot: dict | None = None,
        expires_at: datetime | None = None,
    ) -> int:
        """BUY 결정을 status=pending으로 적재하고 row id 반환."""
        row = TradeDecisionQueue(
            account_id=account_id,
            session_id=session_id,
            symbol=decision.symbol,
            action=decision.action.value,
            quantity=decision.quantity,
            reference_price=decision.price if decision.price is not None else _ZERO,
            order_type=decision.order_type.value,
            strategy_type=strategy_type,
            decision_payload=decision.model_dump(mode="json"),
            entry_analysis_snapshot=entry_snapshot,
            status="pending",
            expires_at=expires_at,
        )
        try:
            async with self._session_factory() as session:
                session.add(row)
                await session.commit()
                await session.refresh(row)
                return row.id
        except Exception as exc:  # noqa: BLE001
            raise DatabaseError(f"enqueue decision DB error ({decision.symbol}): {exc}") from exc

    async def get_pending(self, account_id: str) -> list[PendingDecision]:
        """해당 계좌의 pending 결정들을 생성순으로 반환."""
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(TradeDecisionQueue)
                    .where(
                        TradeDecisionQueue.account_id == account_id,
                        TradeDecisionQueue.status == "pending",
                    )
                    .order_by(TradeDecisionQueue.created_at)
                )
                rows = result.scalars().all()
        except Exception as exc:  # noqa: BLE001
            raise DatabaseError(f"get_pending decision DB error ({account_id}): {exc}") from exc

        pending: list[PendingDecision] = []
        for row in rows:
            try:
                decision = TradeDecision.model_validate(row.decision_payload)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "decision_queue.payload_invalid", id=row.id, symbol=row.symbol
                )
                continue
            pending.append(
                PendingDecision(
                    id=row.id,
                    account_id=row.account_id,
                    symbol=row.symbol,
                    reference_price=Decimal(str(row.reference_price)),
                    strategy_type=row.strategy_type,
                    session_id=row.session_id,
                    decision=decision,
                    entry_analysis_snapshot=row.entry_analysis_snapshot,
                    expires_at=row.expires_at,
                )
            )
        return pending

    async def mark_executed(self, decision_id: int, order_id: int | None) -> None:
        """결정을 executed로 전이하고 order_id 연결."""
        await self._set_status(
            [decision_id], status="executed", order_id=order_id
        )

    async def mark_expired(self, decision_ids: list[int], reason: str) -> None:
        """결정들을 expired로 전이하고 사유 기록."""
        if not decision_ids:
            return
        await self._set_status(decision_ids, status="expired", gate_reason=reason)

    async def _set_status(
        self,
        decision_ids: list[int],
        *,
        status: str,
        order_id: int | None = None,
        gate_reason: str | None = None,
    ) -> None:
        values: dict = {"status": status}
        if order_id is not None:
            values["order_id"] = order_id
        if gate_reason is not None:
            values["gate_reason"] = gate_reason
        if status == "executed":
            values["executed_at"] = datetime.now(UTC)
        try:
            async with self._session_factory() as session:
                await session.execute(
                    update(TradeDecisionQueue)
                    .where(
                        TradeDecisionQueue.id.in_(decision_ids),
                        # pending 만 전이 — 중복/경합 방지.
                        TradeDecisionQueue.status == "pending",
                    )
                    .values(**values)
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            raise DatabaseError(f"set_status decision DB error: {exc}") from exc
