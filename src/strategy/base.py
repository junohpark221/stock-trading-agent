"""Strategy ABC — 매매 전략 구현체의 공통 인터페이스.

Phase 3 PipelineOrchestrator 위에서 동작하는 상위 레이어로,
포지션 CRUD, 청산 조건 확인, LLM 분석 위임 등 공통 로직을 제공한다.
서브클래스(PositionTrading, SwingTrading)가 abstract 메서드를 구현한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

from src.core.enums import ExitReason, StrategyType
from src.core.models import ExitSignal, PipelineResult, PositionSizing, Signal
from src.db.models.strategy import PositionRecord
from src.strategy.exit_checker import ExitConditionChecker
from src.strategy.portfolio_state import PortfolioStateService
from src.strategy.position_manager import PositionManager

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.agent.decision_recorder import DecisionRecorder
    from src.agent.orchestrator import PipelineOrchestrator
    from src.broker.base import BrokerInterface
    from src.config import Settings

logger = structlog.get_logger(__name__)


class Strategy(ABC):
    """매매 전략 추상 기반 클래스.

    서브클래스는 ``strategy_type``, ``scan_universe``, ``generate_signals``,
    ``check_exit_conditions``를 구현해야 한다. 나머지 메서드는 공통 구현이다.
    """

    def __init__(
        self,
        *,
        orchestrator: PipelineOrchestrator,
        risk_manager: Any,  # AlgoRiskManager (Step 4에서 구현)
        portfolio_service: PortfolioStateService,
        broker: BrokerInterface,
        recorder: DecisionRecorder,
        position_manager: PositionManager,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        account_id: str = "default",
        investment_prompt: str = "",
    ) -> None:
        self._orchestrator = orchestrator
        self._risk_manager = risk_manager
        self._portfolio_service = portfolio_service
        self._broker = broker
        self._recorder = recorder
        self._position_manager = position_manager
        self._session_factory = session_factory
        self._settings = settings
        self._account_id = account_id
        self._investment_prompt = investment_prompt
        self._exit_checker = ExitConditionChecker(
            max_drawdown_pct=Decimal(str(settings.MAX_DRAWDOWN_PCT)),
        )

    # ── Abstract (서브클래스 구현) ────────────────────────────────────────

    @property
    @abstractmethod
    def strategy_type(self) -> StrategyType:
        """전략 유형 (POSITION 또는 SWING)."""

    @abstractmethod
    async def scan_universe(self) -> list[str]:
        """분석 대상 종목 코드 리스트를 반환한다."""

    @abstractmethod
    async def generate_signals(self, pipeline_result: PipelineResult) -> list[Signal]:
        """PipelineResult를 매매 시그널로 변환한다."""

    @abstractmethod
    async def check_exit_conditions(
        self, position: PositionRecord
    ) -> ExitSignal | None:
        """개별 포지션의 청산 조건을 확인한다. 청산 불필요 시 None."""

    # ── Concrete (공통 구현) ─────────────────────────────────────────────

    async def analyze(self, symbols: list[str]) -> PipelineResult:
        """PipelineOrchestrator에 분석을 위임한다."""
        logger.info(
            "strategy.analyze",
            strategy=self.strategy_type.value,
            symbols=symbols,
            account_id=self._account_id,
        )
        return await self._orchestrator.execute(
            symbols,
            investment_prompt=self._investment_prompt or None,
            account_id=self._account_id,
        )

    async def check_all_exit_conditions(self) -> list[ExitSignal]:
        """현재 전략의 모든 활성 포지션에 대해 청산 조건을 확인한다."""
        positions = await self.get_open_positions(strategy_type=self.strategy_type)
        signals: list[ExitSignal] = []

        for pos in positions:
            signal = await self.check_exit_conditions(pos)
            if signal is not None:
                signals.append(signal)

        logger.info(
            "strategy.exit_check",
            strategy=self.strategy_type.value,
            checked=len(positions),
            signals=len(signals),
        )
        return signals

    async def save_position(
        self,
        *,
        signal: Signal,
        sizing: PositionSizing,
        session_id: UUID,
        trailing_stop_pct: Decimal | None = None,
        max_holding_days: int | None = None,
    ) -> PositionRecord:
        """새 포지션을 DB에 저장한다. PositionManager에 위임."""
        return await self._position_manager.create(
            symbol=signal.symbol,
            strategy_type=self.strategy_type.value,
            quantity=sizing.quantity,
            entry_price=sizing.entry_price,
            stop_loss_price=sizing.stop_loss_price,
            take_profit_price=sizing.take_profit_price,
            trailing_stop_pct=trailing_stop_pct,
            max_holding_days=max_holding_days,
            entry_session_id=session_id,
            account_id=self._account_id,
        )

    async def close_position(
        self,
        position_id: int,
        *,
        exit_price: Decimal,
        reason: ExitReason,
        session_id: UUID,
    ) -> PositionRecord:
        """포지션을 청산 처리한다. PositionManager에 위임."""
        return await self._position_manager.close(
            position_id,
            exit_price=exit_price,
            exit_reason=reason,
            exit_session_id=session_id,
        )

    async def get_open_positions(
        self, strategy_type: StrategyType | None = None
    ) -> list[PositionRecord]:
        """활성 포지션 목록을 조회한다. PositionManager에 위임."""
        return await self._position_manager.get_open(
            strategy_type=strategy_type, account_id=self._account_id
        )
