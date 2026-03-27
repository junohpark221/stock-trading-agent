"""LLMReplayProvider — decision_log 기반 과거 LLM 응답 재생.

백테스트 시작 시 대상 기간/종목의 decision_log를 일괄 로드하여
dict[(date, symbol)] → list[LLMReplayDecision] 캐싱.
Mode 2 (LLM_REPLAY) 백테스트에서 시그널 소스로 사용된다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.enums import AgentType, DecisionAction, SignalAction
from src.core.models import Signal
from src.db.models.llm import DecisionLog

logger = structlog.get_logger(__name__)

# 한국 표준시 (UTC+9) — decision_log.created_at → KST date 변환용
_KST = timezone(timedelta(hours=9))

# 시그널 변환 시 최소 확신도
_MIN_CONFIDENCE = Decimal("0.5")

# 기본 stage 필터
_DEFAULT_STAGE_FILTER = ["trade_decision", "stock_analysis"]

# 시그널 변환 대상 decision 값
_ACTIONABLE_DECISIONS = [
    DecisionAction.BUY.value,
    DecisionAction.SELL.value,
    DecisionAction.HOLD.value,
]

# DecisionAction → SignalAction 매핑
_DECISION_TO_SIGNAL: dict[str, SignalAction] = {
    DecisionAction.BUY.value: SignalAction.BUY,
    DecisionAction.SELL.value: SignalAction.SELL,
}


class LLMReplayDecision(BaseModel):
    """DecisionLog에서 필요한 필드만 추출한 경량 DTO."""

    model_config = ConfigDict(from_attributes=True)

    decision_id: UUID
    symbol: str
    stage: str
    decision: str  # DecisionAction value
    confidence: Decimal
    reasoning: str
    llm_provider: str | None = None
    llm_model: str | None = None
    created_at: datetime


class LLMReplayProvider:
    """decision_log 기반 과거 LLM 응답 재생.

    백테스트 시작 시 대상 기간/종목의 decision_log를 일괄 로드하여
    dict[(date, symbol)] → list[LLMReplayDecision] 캐싱.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory
        self._cache: dict[tuple[date, str], list[LLMReplayDecision]] = {}
        self._loaded_count: int = 0

    async def load(
        self,
        *,
        start_date: date,
        end_date: date,
        symbols: list[str],
        stage_filter: list[str] | None = None,
        llm_model_filter: str | None = None,
    ) -> int:
        """decision_log에서 대상 데이터 일괄 로드.

        Returns:
            로드된 레코드 수.
        """
        if stage_filter is None:
            stage_filter = _DEFAULT_STAGE_FILTER

        # 기존 캐시 클리어
        self._cache.clear()
        self._loaded_count = 0

        # created_at 범위: start_date 00:00 KST ~ end_date+1 00:00 KST
        start_dt = datetime(
            start_date.year, start_date.month, start_date.day, tzinfo=_KST,
        )
        end_dt = datetime(
            end_date.year, end_date.month, end_date.day, tzinfo=_KST,
        ) + timedelta(days=1)

        stmt = (
            select(DecisionLog)
            .where(
                DecisionLog.created_at >= start_dt,
                DecisionLog.created_at < end_dt,
                DecisionLog.symbol.in_(symbols),
                DecisionLog.symbol.isnot(None),
                DecisionLog.confidence.isnot(None),
                DecisionLog.stage.in_(stage_filter),
                DecisionLog.decision.in_(_ACTIONABLE_DECISIONS),
            )
            .order_by(DecisionLog.created_at)
        )

        if llm_model_filter is not None:
            stmt = stmt.where(DecisionLog.llm_model == llm_model_filter)

        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()

        for row in rows:
            decision = LLMReplayDecision(
                decision_id=row.decision_id,
                symbol=row.symbol,  # type: ignore[arg-type]
                stage=row.stage,
                decision=row.decision,
                confidence=row.confidence,  # type: ignore[arg-type]
                reasoning=row.reasoning,
                llm_provider=row.llm_provider,
                llm_model=row.llm_model,
                created_at=row.created_at,  # type: ignore[arg-type]
            )
            # KST 기준 날짜로 캐시 키 생성
            kst_date = row.created_at.astimezone(_KST).date()  # type: ignore[union-attr]
            key = (kst_date, row.symbol)  # type: ignore[arg-type]
            self._cache.setdefault(key, []).append(decision)

        self._loaded_count = len(rows)

        logger.info(
            "llm_replay_loaded",
            count=self._loaded_count,
            symbols=len(symbols),
            date_range=f"{start_date}~{end_date}",
            model_filter=llm_model_filter,
        )

        return self._loaded_count

    def get_decisions(
        self, *, target_date: date, symbol: str,
    ) -> list[LLMReplayDecision]:
        """특정 (날짜, 종목) 의사결정 조회. 캐시 O(1)."""
        return self._cache.get((target_date, symbol), [])

    def to_signal(
        self, decision: LLMReplayDecision, *, current_price: Decimal,
    ) -> Signal | None:
        """LLMReplayDecision → Signal 변환.

        BUY → Signal(BUY), SELL → Signal(SELL), HOLD → None.
        confidence < 0.5 → None (낮은 확신도 무시).
        """
        # 낮은 확신도 무시
        if decision.confidence < _MIN_CONFIDENCE:
            return None

        # HOLD → None
        action = _DECISION_TO_SIGNAL.get(decision.decision)
        if action is None:
            return None

        return Signal(
            symbol=decision.symbol,
            action=action,
            confidence=decision.confidence,
            target_price=None,
            stop_loss_price=None,
            quantity=0,
            position_value_krw=Decimal("0"),
            reasoning=f"LLM replay [{decision.stage}]: {decision.reasoning[:200]}",
            source_agent=AgentType.TRADER,
            timestamp=decision.created_at,
        )

    @property
    def loaded_count(self) -> int:
        """로드된 레코드 수."""
        return self._loaded_count
