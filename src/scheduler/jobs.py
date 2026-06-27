"""Stateless job functions — 11개 스케줄 작업.

각 함수는 keyword-only 인자로 의존성을 받는다.
Step 8 SchedulerFactory에서 functools.partial로 바인딩하여
인자 없는 callable을 SchedulerEngine.register_jobs()에 전달한다.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import structlog

from src.core.enums import DecisionAction
from src.data.collector import collect_daily_ohlcv
from src.notification.templates import MessageTemplates
from src.strategy.risk_manager import BatchReservation

if TYPE_CHECKING:
    from src.agent.orchestrator import PipelineOrchestrator
    from src.broker.base import BrokerInterface
    from src.broker.kis.auth import KISAuth
    from src.data.providers.base import DataProvider
    from src.execution.executor import OrderExecutor
    from src.execution.exit_executor import ExitExecutionService
    from src.execution.reconciler import OrderReconciler, PositionReconciler
    from src.notification.telegram import TelegramBot
    from src.report.generator import ReportGenerator
    from src.scheduler.monitor import TradingMonitor
    from src.strategy.base import Strategy
    from src.strategy.batch_allocator import BatchBudgetAllocator
    from src.strategy.exit_checker import ExitConditionChecker
    from src.strategy.portfolio_state import PortfolioStateService
    from src.strategy.position_manager import PositionManager

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")
_Q2 = Decimal("0.01")
_KST = ZoneInfo("Asia/Seoul")


# ── Token / Data Collection ────────────────────────────────────────────


async def job_token_refresh(
    *,
    auth: KISAuth,
    account_id: str = "default",
) -> None:
    """KIS OAuth 토큰 사전 갱신. Daily 06:00. 계좌별 실행."""
    token = await auth.refresh_token()
    logger.info("job.token_refresh.done", token_prefix=token[:8], account_id=account_id)


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


def _is_market_open(
    market_open: str = "09:00",
    market_close: str = "15:30",
    holidays: str = "",
) -> bool:
    """현재 시각이 한국 장 운영 시간(KST) 내인지 확인.

    주말(토/일) 및 공휴일(KR_HOLIDAYS 설정)에는 False를 반환한다.
    """
    now_kst = datetime.now(_KST)
    # 주말 체크 (0=월 ~ 4=금, 5=토, 6=일)
    if now_kst.weekday() >= 5:
        return False
    # 공휴일 체크
    if holidays:
        holiday_dates = {d.strip() for d in holidays.split(",") if d.strip()}
        if now_kst.strftime("%Y-%m-%d") in holiday_dates:
            return False
    now_time = now_kst.time()
    h_open, m_open = map(int, market_open.split(":"))
    h_close, m_close = map(int, market_close.split(":"))
    return time(h_open, m_open) <= now_time <= time(h_close, m_close)


async def _execute_buy_decisions(
    *,
    result: object,
    order_executor: OrderExecutor,
    strategy_type: str,
    account_id: str,
    account_label: str,
    market_open: str,
    market_close: str,
    holidays: str = "",
    allocator: BatchBudgetAllocator | None = None,
) -> int:
    """PipelineResult의 BUY 결정을 실제 주문으로 실행. 장중에만 동작.

    Returns:
        실행된 매수 주문 수.
    """
    from src.core.models import PipelineResult

    pipeline_result: PipelineResult = result  # type: ignore[assignment]
    buy_decisions = [
        td for td in pipeline_result.trade_decisions
        if td.action == DecisionAction.BUY
    ]
    if not buy_decisions:
        return 0

    if not _is_market_open(market_open, market_close, holidays):
        logger.info(
            "job.buy_execution.skip_market_closed",
            buy_count=len(buy_decisions),
            account_id=account_id,
        )
        return 0

    # 배치 예산 배분: 후보들을 가용 현금에 맞춰 순위·재사이징·필터.
    if allocator is not None:
        original_count = len(buy_decisions)
        buy_decisions = await allocator.allocate(
            buy_decisions,
            account_id=account_id,
            session_id=pipeline_result.session_id,
        )
        logger.info(
            "job.buy_execution.batch_allocated",
            account_id=account_id,
            original_count=original_count,
            allocated_count=len(buy_decisions),
        )
        if not buy_decisions:
            return 0

    # 배치 누적 한도 게이트용 in-flight 예약 (F-04). 이 배치에서 접수한 진입을
    # 누적해 후속 후보의 MAX_HOLDINGS/MAX_DAILY_TRADES/섹터 한도 검증에 반영한다.
    reservation = BatchReservation()
    executed = 0
    for td in buy_decisions:
        try:
            exec_result = await order_executor.execute_entry(
                trade_decision=td,
                session_id=pipeline_result.session_id,
                strategy_type=strategy_type,
                account_id=account_id,
                account_label=account_label,
                batch_reservation=reservation,
            )
            if exec_result.success:
                executed += 1
                logger.info(
                    "job.buy_execution.success",
                    symbol=td.symbol,
                    quantity=td.quantity,
                    price=str(td.price),
                    order_id=exec_result.order_id,
                    account_id=account_id,
                )
            else:
                logger.warning(
                    "job.buy_execution.failed",
                    symbol=td.symbol,
                    account_id=account_id,
                )
        except Exception:
            logger.exception(
                "job.buy_execution.error",
                symbol=td.symbol,
                account_id=account_id,
            )
    return executed


async def job_swing_analysis(
    *,
    orchestrator: PipelineOrchestrator,
    symbols: list[str],
    strategy: Strategy | None = None,
    account_id: str = "default",
    investment_prompt: str = "",
    risk_tolerance: str = "moderate",
    order_executor: OrderExecutor | None = None,
    account_label: str = "",
    market_open: str = "09:00",
    market_close: str = "15:30",
    holidays: str = "",
    allocator: BatchBudgetAllocator | None = None,
) -> None:
    """스윙 전략 시그널 스캔 + BUY 자동 실행. Daily 01:00 UTC (10:00 KST). 계좌별."""
    # scan_universe()로 필터링, 없으면 전체 watchlist fallback
    if strategy is not None:
        target_symbols = await strategy.scan_universe()
        logger.info(
            "job.swing_analysis.filtered",
            total=len(symbols),
            filtered=len(target_symbols),
            account_id=account_id,
        )
    else:
        target_symbols = symbols

    if not target_symbols:
        logger.info("job.swing_analysis.skip", reason="no_target_symbols", account_id=account_id)
        return

    result = await orchestrator.execute(
        target_symbols,
        investment_prompt=investment_prompt,
        risk_tolerance=risk_tolerance,
        account_id=account_id,
    )
    logger.info(
        "job.swing_analysis.done",
        session_id=str(result.session_id),
        symbols_count=len(target_symbols),
        buy_decisions=len([td for td in result.trade_decisions if td.action == DecisionAction.BUY]),
        account_id=account_id,
    )

    if order_executor is not None:
        executed = await _execute_buy_decisions(
            result=result,
            order_executor=order_executor,
            strategy_type="swing",
            account_id=account_id,
            account_label=account_label,
            market_open=market_open,
            market_close=market_close,
            holidays=holidays,
            allocator=allocator,
        )
        if executed:
            logger.info("job.swing_analysis.orders_executed", count=executed, account_id=account_id)


async def job_position_analysis(
    *,
    orchestrator: PipelineOrchestrator,
    position_manager: PositionManager,
    account_id: str = "default",
    investment_prompt: str = "",
    risk_tolerance: str = "moderate",
    order_executor: OrderExecutor | None = None,
    account_label: str = "",
    market_open: str = "09:00",
    market_close: str = "15:30",
    holidays: str = "",
    allocator: BatchBudgetAllocator | None = None,
) -> None:
    """보유 포지션 심층 분석 + BUY 자동 실행. Wed & Sat 01:30 UTC (10:30 KST). 계좌별."""
    positions = await position_manager.get_open(account_id=account_id)
    if not positions:
        logger.info("job.position_analysis.skip", reason="no_open_positions", account_id=account_id)
        return

    symbols = list({p.symbol for p in positions})
    result = await orchestrator.execute(
        symbols,
        investment_prompt=investment_prompt,
        risk_tolerance=risk_tolerance,
        account_id=account_id,
    )
    logger.info(
        "job.position_analysis.done",
        session_id=str(result.session_id),
        positions_count=len(positions),
        symbols_count=len(symbols),
        buy_decisions=len([td for td in result.trade_decisions if td.action == DecisionAction.BUY]),
        account_id=account_id,
    )

    if order_executor is not None:
        executed = await _execute_buy_decisions(
            result=result,
            order_executor=order_executor,
            strategy_type="position",
            account_id=account_id,
            account_label=account_label,
            market_open=market_open,
            market_close=market_close,
            holidays=holidays,
            allocator=allocator,
        )
        if executed:
            logger.info("job.position_analysis.orders_executed", count=executed, account_id=account_id)


# ── Stop-Loss Check ────────────────────────────────────────────────────


async def job_stop_loss_check(
    *,
    exit_checker: ExitConditionChecker,
    exit_service: ExitExecutionService,
    position_manager: PositionManager,
    portfolio_service: PortfolioStateService,
    broker: BrokerInterface,
    monitor: TradingMonitor,
    account_id: str = "default",
    account_label: str = "",
    market_open: str = "09:00",
    market_close: str = "15:30",
    holidays: str = "",
) -> None:
    """손절/익절/트레일링 스톱 체크 + 자동 청산 + 근접 알림. 5분 간격. 계좌별 실행.

    Flow:
    1. 오픈 포지션 조회 (없으면 조기 리턴)
    2. 각 포지션: 현재가 조회 → 미실현 손익률 계산 → 4가지 청산 조건 체크
    3. exit_signals 수집 → ExitExecutionService로 일괄 청산
    4. TradingMonitor.check_all() → 근접/편중/예산/낙폭 알림
    """
    if not _is_market_open(market_open=market_open, market_close=market_close, holidays=holidays):
        logger.debug("job.stop_loss_check.skip", reason="market_closed", account_id=account_id)
        return

    positions = await position_manager.get_open(account_id=account_id)
    if not positions:
        logger.debug("job.stop_loss_check.skip", reason="no_open_positions", account_id=account_id)
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
                    (current_price - position.entry_price) / position.entry_price * _HUNDRED
                ).quantize(_Q2, rounding=ROUND_HALF_UP)
            else:
                unrealized_pnl_pct = _ZERO

            # 트레일링 스탑용 고점(high water mark) 갱신
            if position.trailing_stop_pct is not None:
                if position.highest_price is None or current_price > position.highest_price:
                    await position_manager.update_highest_price(position.id, current_price)
                    position.highest_price = current_price  # 루프 내 로컬 캐시 동기화

            # 4가지 청산 조건 체크
            signal = exit_checker.check_stop_loss(position, current_price, unrealized_pnl_pct)
            if signal:
                exit_signals.append(signal)
                continue  # 손절 시그널이 나오면 다른 조건은 불필요

            signal = exit_checker.check_take_profit(position, current_price, unrealized_pnl_pct)
            if signal:
                if position.trailing_stop_pct is not None:
                    # 트레일링 스탑 설정 시: 익절가 도달을 트레일링 스탑 모드 전환으로 취급.
                    # 즉시 매도하지 않고 고점 추적을 계속하여 추가 상승을 노린다.
                    logger.info(
                        "job.take_profit_to_trailing",
                        symbol=position.symbol,
                        current_price=str(current_price),
                        take_profit_price=str(position.take_profit_price),
                        trailing_stop_pct=str(position.trailing_stop_pct),
                    )
                    # trailing stop 체크로 fall-through
                else:
                    exit_signals.append(signal)
                    continue

            # 트레일링 스톱 (trailing_stop_pct가 설정된 경우만)
            if position.trailing_stop_pct is not None:
                baseline = position.highest_price or position.entry_price
                trailing_stop_price = baseline * (
                    Decimal("1") - position.trailing_stop_pct / _HUNDRED
                )
                signal = exit_checker.check_trailing_stop(
                    position,
                    current_price,
                    unrealized_pnl_pct,
                    trailing_stop_price,
                )
                if signal:
                    exit_signals.append(signal)
                    continue

            signal = exit_checker.check_time_based(
                position,
                current_price,
                unrealized_pnl_pct,
                today,
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
            exit_signals,
            positions,
            session_id=session_id,
            account_id=account_id,
            account_label=account_label,
        )
        logger.info(
            "job.stop_loss_check.exit_executed",
            signal_count=len(exit_signals),
            result_count=len(results),
            session_id=str(session_id),
            account_id=account_id,
        )

    # 모니터링 알림 (손절 근접, 섹터 편중, LLM 예산, 포트폴리오 낙폭)
    alerts = await monitor.check_all()
    if alerts:
        logger.info(
            "job.stop_loss_check.alerts_sent",
            alert_count=len(alerts),
            account_id=account_id,
        )


# ── Reports ─────────────────────────────────────────────────────────────


async def job_daily_report(
    *,
    generator: ReportGenerator,
    telegram_bot: TelegramBot,
    portfolio_service: PortfolioStateService,
    account_id: str = "default",
    account_label: str = "",
) -> None:
    """일간 리포트 생성 + Telegram 전송. Daily 20:00. 계좌별 실행.

    1. 포트폴리오 스냅샷 저장 (일일 기록)
    2. 일간 리포트 생성
    3. HTML 포맷팅 → Telegram 전송
    """
    # 스냅샷 저장
    state = await portfolio_service.get_current_state()
    await portfolio_service.save_snapshot(state)

    # 리포트 생성 + 전송
    data = await generator.generate_daily_report(account_id=account_id)
    html = MessageTemplates.daily_report(data, account_label=account_label)
    await telegram_bot.send_message(html)
    logger.info("job.daily_report.sent", account_id=account_id)


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


# ── Order Reconciliation (SUBMITTED 미체결 정리) ─────────────────────


async def job_reconcile_open_orders(
    *,
    reconciler: OrderReconciler,
    eod: bool = False,
) -> None:
    """KIS REST 기반 SUBMITTED 주문 체결 확인.

    WS 체결통보(ExecutionStreamManager)가 놓친 주문을 KIS 일별 체결조회(TTTC0081R)로
    교차 확인. 12:00 KST (mid-day) + 15:40 KST (EOD) 2회 실행.
    """
    processed = await reconciler.run(eod=eod)
    logger.info("job.reconcile_open_orders.done", processed=processed, eod=eod)


async def job_reconcile_positions(
    *,
    position_reconciler: PositionReconciler,
) -> None:
    """브로커 보유 포지션 vs DB open 포지션 양방향 동기화.

    - DB only  → closed (exit_reason=reconciled)
    - broker only → 신규 생성 (strategy=manual)
    - 수량/단가 불일치 → DB 갱신
    장중 9:00–16:00 KST 매시 정각 + 15:50 KST EOD 실행.
    """
    result = await position_reconciler.reconcile()
    logger.info(
        "job.reconcile_positions.done",
        closed=result.closed_count,
        created=result.created_count,
        qty_updated=result.qty_updated_count,
        order_corrected=result.order_corrected_count,
        broker_symbols=result.broker_symbol_count,
        db_open=result.db_open_count,
    )
