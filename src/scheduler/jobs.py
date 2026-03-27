"""Stateless job functions — 9개 스케줄 작업.

각 함수는 keyword-only 인자로 의존성을 받는다.
Step 8 SchedulerFactory에서 functools.partial로 바인딩하여
인자 없는 callable을 SchedulerEngine.register_jobs()에 전달한다.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

import structlog

from src.data.collector import collect_daily_ohlcv
from src.notification.templates import MessageTemplates

if TYPE_CHECKING:
    from src.agent.orchestrator import PipelineOrchestrator
    from src.broker.base import BrokerInterface
    from src.broker.kis.auth import KISAuth
    from src.data.providers.base import DataProvider
    from src.execution.exit_executor import ExitExecutionService
    from src.notification.telegram import TelegramBot
    from src.report.generator import ReportGenerator
    from src.scheduler.monitor import TradingMonitor
    from src.strategy.exit_checker import ExitConditionChecker
    from src.strategy.portfolio_state import PortfolioStateService
    from src.strategy.position_manager import PositionManager

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")
_Q2 = Decimal("0.01")


# ── Token / Data Collection ────────────────────────────────────────────


async def job_token_refresh(*, auth: KISAuth) -> None:
    """KIS OAuth 토큰 사전 갱신. Daily 06:00."""
    token = await auth.refresh_token()
    logger.info("job.token_refresh.done", token_prefix=token[:8])


async def job_market_data_collect(
    *,
    provider: DataProvider,
    symbols: list[str],
) -> None:
    """일별 OHLCV 수집. Daily 15:40."""
    summary = await collect_daily_ohlcv(provider, symbols)
    logger.info(
        "job.market_data_collect.done",
        total=summary.total_symbols,
        succeeded=summary.succeeded,
        failed=summary.failed,
        rows=summary.total_rows,
    )


# ── Analysis ────────────────────────────────────────────────────────────


async def job_swing_analysis(
    *,
    orchestrator: PipelineOrchestrator,
    symbols: list[str],
) -> None:
    """스윙 전략 시그널 스캔. Daily 16:00."""
    result = await orchestrator.execute(symbols)
    logger.info(
        "job.swing_analysis.done",
        session_id=str(result.session_id),
        symbols_count=len(symbols),
    )


async def job_position_analysis(
    *,
    orchestrator: PipelineOrchestrator,
    position_manager: PositionManager,
) -> None:
    """보유 포지션 심층 분석. Wed & Sat 16:30."""
    positions = await position_manager.get_open()
    if not positions:
        logger.info("job.position_analysis.skip", reason="no_open_positions")
        return

    symbols = list({p.symbol for p in positions})
    result = await orchestrator.execute(symbols)
    logger.info(
        "job.position_analysis.done",
        session_id=str(result.session_id),
        positions_count=len(positions),
        symbols_count=len(symbols),
    )


# ── Stop-Loss Check ────────────────────────────────────────────────────


async def job_stop_loss_check(
    *,
    exit_checker: ExitConditionChecker,
    exit_service: ExitExecutionService,
    position_manager: PositionManager,
    portfolio_service: PortfolioStateService,
    broker: BrokerInterface,
    monitor: TradingMonitor,
) -> None:
    """손절/익절/트레일링 스톱 체크 + 자동 청산 + 근접 알림. 5분 간격.

    Flow:
    1. 오픈 포지션 조회 (없으면 조기 리턴)
    2. 각 포지션: 현재가 조회 → 미실현 손익률 계산 → 4가지 청산 조건 체크
    3. exit_signals 수집 → ExitExecutionService로 일괄 청산
    4. TradingMonitor.check_all() → 근접/편중/예산/낙폭 알림
    """
    positions = await position_manager.get_open()
    if not positions:
        logger.debug("job.stop_loss_check.skip", reason="no_open_positions")
        return

    from src.core.models import ExitSignal

    exit_signals: list[ExitSignal] = []
    today = date.today()

    for position in positions:
        try:
            price_info = await broker.get_price(position.symbol)
            current_price = price_info.current_price

            # 미실현 손익률 계산
            if position.entry_price > _ZERO:
                unrealized_pnl_pct = (
                    (current_price - position.entry_price)
                    / position.entry_price
                    * _HUNDRED
                ).quantize(_Q2, rounding=ROUND_HALF_UP)
            else:
                unrealized_pnl_pct = _ZERO

            # 4가지 청산 조건 체크
            signal = exit_checker.check_stop_loss(position, current_price, unrealized_pnl_pct)
            if signal:
                exit_signals.append(signal)
                continue  # 손절 시그널이 나오면 다른 조건은 불필요

            signal = exit_checker.check_take_profit(position, current_price, unrealized_pnl_pct)
            if signal:
                exit_signals.append(signal)
                continue

            # 트레일링 스톱 (trailing_stop_pct가 설정된 경우만)
            if position.trailing_stop_pct is not None:
                # trailing_stop_price = 최고가 * (1 - trailing_stop_pct/100)
                # 간이 계산: entry_price 기준 (실제 high water mark는 별도 추적 필요)
                trailing_stop_price = (
                    position.entry_price
                    * (Decimal("1") - position.trailing_stop_pct / _HUNDRED)
                )
                signal = exit_checker.check_trailing_stop(
                    position, current_price, unrealized_pnl_pct, trailing_stop_price,
                )
                if signal:
                    exit_signals.append(signal)
                    continue

            signal = exit_checker.check_time_based(
                position, current_price, unrealized_pnl_pct, today,
            )
            if signal:
                exit_signals.append(signal)

        except Exception:
            logger.warning(
                "job.stop_loss_check.position_error",
                symbol=position.symbol,
                exc_info=True,
            )

    # 청산 시그널 실행
    if exit_signals:
        session_id = uuid.uuid4()
        results = await exit_service.process_exit_signals(
            exit_signals, positions, session_id=session_id,
        )
        logger.info(
            "job.stop_loss_check.exit_executed",
            signal_count=len(exit_signals),
            result_count=len(results),
            session_id=str(session_id),
        )

    # 모니터링 알림 (손절 근접, 섹터 편중, LLM 예산, 포트폴리오 낙폭)
    alerts = await monitor.check_all()
    if alerts:
        logger.info("job.stop_loss_check.alerts_sent", alert_count=len(alerts))


# ── Reports ─────────────────────────────────────────────────────────────


async def job_daily_report(
    *,
    generator: ReportGenerator,
    telegram_bot: TelegramBot,
    portfolio_service: PortfolioStateService,
) -> None:
    """일간 리포트 생성 + Telegram 전송. Daily 20:00.

    1. 포트폴리오 스냅샷 저장 (일일 기록)
    2. 일간 리포트 생성
    3. HTML 포맷팅 → Telegram 전송
    """
    # 스냅샷 저장
    state = await portfolio_service.get_current_state()
    await portfolio_service.save_snapshot(state)

    # 리포트 생성 + 전송
    data = await generator.generate_daily_report()
    html = MessageTemplates.daily_report(data)
    await telegram_bot.send_message(html)
    logger.info("job.daily_report.sent")


async def job_weekly_report(
    *,
    generator: ReportGenerator,
    telegram_bot: TelegramBot,
) -> None:
    """주간 리포트. Saturdays 10:00."""
    data = await generator.generate_weekly_report()
    html = MessageTemplates.weekly_report(data)
    await telegram_bot.send_message(html)
    logger.info("job.weekly_report.sent")


async def job_monthly_report(
    *,
    generator: ReportGenerator,
    telegram_bot: TelegramBot,
) -> None:
    """월간 리포트. 1st of month 10:00."""
    metrics, summary = await generator.generate_monthly_report()
    html = MessageTemplates.monthly_report(metrics=metrics, summary=summary)
    await telegram_bot.send_message(html)
    logger.info("job.monthly_report.sent")


async def job_llm_cost_report(
    *,
    generator: ReportGenerator,
    telegram_bot: TelegramBot,
) -> None:
    """LLM 비용 리포트. Mondays 09:00."""
    data = await generator.generate_llm_cost_report()
    html = MessageTemplates.llm_cost_report(data)
    await telegram_bot.send_message(html)
    logger.info("job.llm_cost_report.sent")
