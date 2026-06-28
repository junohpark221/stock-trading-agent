"""AgentMemoryManager — 에이전트 학습 메모리 CRUD 관리자.

에이전트가 과거 매매 경험에서 학습한 교훈(lesson)을 저장/조회하고,
청산 후 자동으로 학습 메모리를 생성한다.  만료된 메모리는 비활성화한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select, update

from src.core.exceptions import DatabaseError
from src.db.models.strategy import AgentMemory, PositionRecord

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.core.models import PipelineResult

logger = structlog.get_logger(__name__)


class AgentMemoryManager:
    """agent_memory 테이블 CRUD 관리자.

    - save_lesson: 학습 메모리 저장
    - get_relevant_memories: 관련 메모리 조회 (agent_type, symbol 필터)
    - record_trade_outcome: 청산 결과 → 자동 교훈 생성
    - cleanup_expired: 만료 메모리 비활성화
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    # ── Save ──────────────────────────────────────────────────────────────

    async def save_lesson(
        self,
        agent_type: str,
        symbol: str | None,
        content: str,
        *,
        context: dict | None = None,
        source_session_id: str | None = None,
        expires_days: int = 90,
        relevance_score: Decimal = Decimal("1.0"),
        account_id: str = "default",
    ) -> int:
        """학습 메모리 저장 → agent_memory.id 반환.

        Parameters
        ----------
        agent_type: 에이전트 유형 (AgentType.value)
        symbol: 종목 코드 (None이면 범용 메모리)
        content: 학습 내용
        context: 추가 컨텍스트 (JSONB)
        source_session_id: 원본 decision_log 세션 ID
        expires_days: 만료 기간 (기본 90일)
        relevance_score: 관련성 점수 (기본 1.0)
        """
        import uuid

        expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)
        session_uuid = uuid.UUID(source_session_id) if source_session_id else None

        record = AgentMemory(
            agent_type=agent_type,
            memory_type="lesson",
            symbol=symbol,
            content=content,
            context=context,
            source_session_id=session_uuid,
            relevance_score=relevance_score,
            expires_at=expires_at,
            is_active=True,
            account_id=account_id,
        )

        try:
            async with self._session_factory() as session:
                session.add(record)
                await session.commit()
                await session.refresh(record)
        except Exception as exc:
            raise DatabaseError(f"Memory save failed: {exc}") from exc

        logger.info(
            "memory.saved",
            id=record.id,
            agent_type=agent_type,
            symbol=symbol,
            expires_days=expires_days,
            account_id=account_id,
        )
        return record.id

    # ── Read ──────────────────────────────────────────────────────────────

    async def get_relevant_memories(
        self,
        agent_type: str,
        symbol: str | None = None,
        *,
        limit: int = 10,
        account_id: str | None = None,
    ) -> list[AgentMemory]:
        """관련 메모리 조회.

        - agent_type 매칭
        - symbol 매칭 (None이면 범용 메모리만)
        - is_active=True, expires_at 미만 or None
        - relevance_score DESC 정렬

        Parameters
        ----------
        agent_type: 에이전트 유형 필터
        symbol: 종목 코드 필터 (None이면 범용 + 해당 agent_type 전체)
        limit: 최대 반환 건수 (기본 10)
        """
        now = datetime.now(timezone.utc)

        try:
            async with self._session_factory() as session:
                stmt = (
                    select(AgentMemory)
                    .where(
                        AgentMemory.agent_type == agent_type,
                        AgentMemory.is_active.is_(True),
                    )
                    .where(
                        # 만료 전이거나 만료일 없는 것만
                        (AgentMemory.expires_at.is_(None))
                        | (AgentMemory.expires_at > now)
                    )
                )

                if account_id is not None:
                    stmt = stmt.where(
                        AgentMemory.account_id == account_id
                    )

                if symbol is not None:
                    # 종목별 + 범용(symbol=None) 모두 조회
                    stmt = stmt.where(
                        (AgentMemory.symbol == symbol)
                        | (AgentMemory.symbol.is_(None))
                    )

                stmt = (
                    stmt.order_by(AgentMemory.relevance_score.desc())
                    .limit(limit)
                )

                result = await session.execute(stmt)
                return list(result.scalars().all())
        except Exception as exc:
            raise DatabaseError(
                f"Memory query failed: {exc}"
            ) from exc

    # ── Trade Outcome ────────────────────────────────────────────────────

    async def record_trade_outcome(
        self,
        position: PositionRecord,
        *,
        account_id: str = "default",
    ) -> int | None:
        """청산 후 자동 학습 — 수익/손실 분석 → 교훈 메모리 생성.

        포지션의 실현 손익과 **진입 시 영속화된 분석 스냅샷**
        (`position.entry_analysis_snapshot`)을 비교하여 교훈을 자동 생성한다.
        진입 시점의 PipelineResult를 청산 시점에 다시 들고 있을 수 없으므로,
        진입 주문→포지션으로 전파된 스냅샷을 진실의 원천으로 삼는다.

        Parameters
        ----------
        position: 청산된 PositionRecord (entry_analysis_snapshot 포함)

        Returns
        -------
        생성된 memory id, 또는 학습 불필요 시 None
        """
        if position.status != "closed" or position.realized_pnl is None:
            return None

        pnl = position.realized_pnl
        outcome = "수익" if pnl > 0 else "손실"
        pnl_pct = (
            (pnl / (position.avg_cost * position.quantity) * 100)
            if position.avg_cost and position.quantity
            else Decimal(0)
        )

        # 진입 시 LLM 분석 요약 (진입 시점에 포지션에 영속화된 스냅샷)
        entry_analysis = _format_entry_snapshot(position.entry_analysis_snapshot)

        content = (
            f"종목 {position.symbol}: {outcome} 청산 "
            f"(수익률 {pnl_pct:+.2f}%, PnL {pnl:+,.0f}원). "
            f"전략: {position.strategy_type}. "
            f"청산사유: {position.exit_reason}. "
            f"진입 시 판단: {entry_analysis}"
        )

        context = {
            "realized_pnl": str(pnl),
            "pnl_pct": str(pnl_pct),
            "entry_price": str(position.entry_price),
            "exit_price": str(position.exit_price),
            "exit_reason": position.exit_reason,
            "strategy_type": position.strategy_type,
            "holding_days": (
                (position.exit_date - position.entry_date).days
                if position.exit_date and position.entry_date
                else None
            ),
        }

        # 손실 교훈은 관련성 높게, 수익은 보통
        relevance = Decimal("0.9") if pnl < 0 else Decimal("0.7")

        session_id = (
            str(position.entry_session_id)
            if position.entry_session_id
            else None
        )

        memory_id = await self.save_lesson(
            agent_type=_strategy_to_agent(position.strategy_type),
            symbol=position.symbol,
            content=content,
            context=context,
            source_session_id=session_id,
            relevance_score=relevance,
            account_id=account_id,
        )

        logger.info(
            "memory.trade_outcome_recorded",
            memory_id=memory_id,
            symbol=position.symbol,
            outcome=outcome,
            pnl_pct=str(pnl_pct),
        )
        return memory_id

    # ── Cleanup ───────────────────────────────────────────────────────────

    async def cleanup_expired(self) -> int:
        """만료된 메모리 비활성화 (is_active=False) → 처리 건수 반환."""
        now = datetime.now(timezone.utc)

        try:
            async with self._session_factory() as session:
                stmt = (
                    update(AgentMemory)
                    .where(
                        AgentMemory.is_active.is_(True),
                        AgentMemory.expires_at.isnot(None),
                        AgentMemory.expires_at <= now,
                    )
                    .values(is_active=False)
                )
                result = await session.execute(stmt)
                await session.commit()
                count = result.rowcount
        except Exception as exc:
            raise DatabaseError(
                f"Memory cleanup failed: {exc}"
            ) from exc

        if count > 0:
            logger.info("memory.cleanup_expired", deactivated=count)
        return count


# ── Helpers ───────────────────────────────────────────────────────────────


def build_entry_snapshot(
    symbol: str, pipeline_result: PipelineResult
) -> dict | None:
    """진입 시 PipelineResult → 해당 종목의 분석 스냅샷(JSONB 직렬화 가능) 추출.

    진입 주문/포지션에 영속화해, 청산 후 record_trade_outcome이 참조한다.
    Decimal/Enum은 직렬화 가능한 형태(str/value)로 저장한다(금융 수치는 문자열로).
    """
    for sa in pipeline_result.stock_analyses:
        if sa.symbol == symbol:
            return {
                "symbol": sa.symbol,
                "action": sa.action.value,
                "confidence": str(sa.confidence),
                "key_factors": list(sa.key_factors[:5]),
            }
    return None


def _format_entry_snapshot(snapshot: dict | None) -> str:
    """진입 분석 스냅샷(dict) → 사람이 읽는 요약 문자열."""
    if not snapshot:
        return "분석 데이터 없음"
    parts = [
        f"action={snapshot.get('action')}, confidence={snapshot.get('confidence')}"
    ]
    factors = snapshot.get("key_factors") or []
    if factors:
        parts.append(f"factors={factors[:3]}")
    return "; ".join(parts)


def _strategy_to_agent(strategy_type: str) -> str:
    """전략 유형 → 대표 에이전트 유형 매핑."""
    return "stock_analyst"
