"""Stateless job functions — 11개 스케줄 작업.

각 함수는 keyword-only 인자로 의존성을 받는다.
Step 8 SchedulerFactory에서 functools.partial로 바인딩하여
인자 없는 callable을 SchedulerEngine.register_jobs()에 전달한다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select

from src.core.enums import DecisionAction, ExitReason, StrategyType
from src.core.time import KST as _KST
from src.data.collector import collect_daily_ohlcv
from src.db.models.market_data import DailyOHLCV, StockMaster
from src.execution.exit_coordinator import exit_phase
from src.notification.templates import MessageTemplates
from src.strategy.risk_manager import BatchReservation
from src.strategy.trailing import (
    calculate_atr,
    is_trailing_active,
    partial_tp_quantity,
    trailing_stop_price,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.agent.orchestrator import PipelineOrchestrator
    from src.broker.base import BrokerInterface
    from src.broker.kis.auth import KISAuth
    from src.config import Settings
    from src.core.models import PipelineResult, TradeDecision
    from src.data.providers.base import DataProvider
    from src.db.models.strategy import PositionRecord
    from src.execution.decision_queue import TradeDecisionQueueManager
    from src.execution.executor import OrderExecutor
    from src.execution.exit_coordinator import ExitCoordinator
    from src.execution.exit_executor import ExitExecutionService
    from src.execution.reconciler import OrderReconciler, PositionReconciler
    from src.notification.telegram import TelegramBot
    from src.report.generator import ReportGenerator
    from src.scheduler.monitor import TradingMonitor
    from src.strategy.base import Strategy
    from src.strategy.batch_allocator import BatchBudgetAllocator
    from src.strategy.exit_checker import ExitConditionChecker
    from src.strategy.memory_manager import AgentMemoryManager
    from src.strategy.portfolio_state import PortfolioStateService
    from src.strategy.position_manager import PositionManager

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")
_Q2 = Decimal("0.01")

# POSITION 트레일링용 ATR 산출 — ATR(14) 계산에 필요한 봉 확보를 위한 조회 일수.
_ATR_PERIOD = 14
_ATR_LOOKBACK_DAYS = 40


async def _position_trailing_atr(
    broker: BrokerInterface, position: PositionRecord
) -> Decimal | None:
    """POSITION 전략 트레일링용 ATR 동적 폭 산출에 쓸 ATR. 비대상/실패 시 None.

    F-10 B3: 라이브 폴링에서 POSITION 트레일링 폭을 ATR×배수로 동적 산출하기 위해
    매 사이클 ATR을 재계산한다(저빈도 5분 폴링). SWING(고정 폭)·조회 실패는 None
    → 저장된 trailing_stop_pct(고정/폴백)로 폴백.
    """
    if position.strategy_type != StrategyType.POSITION.value:
        return None
    try:
        ohlcv = await broker.get_daily_ohlcv(
            position.symbol, period_days=_ATR_LOOKBACK_DAYS
        )
        atr = calculate_atr(ohlcv, period=_ATR_PERIOD)
        return atr if atr > _ZERO else None
    except Exception:
        logger.warning(
            "job.stop_loss_check.atr_failed", symbol=position.symbol, exc_info=True
        )
        return None


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


async def job_pre_open_prep(
    *,
    provider: DataProvider | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_bot: TelegramBot | None = None,
) -> None:
    """개장 전 준비(08:00 KST): 결측 일봉 백필 + 신선도·커버리지 게이트 검증.

    야간 market_data_collect(00:40)가 일부 실패해도 결측 종목만 경량 재수집해
    개장 전 데이터를 복구하고, 신선도 미달이면 텔레그램 경고를 보낸다. 실제
    매매 차단(stale-data 방지)은 결정 잡이 _check_data_freshness로 독립 검증한다.
    """
    holidays = settings.KR_HOLIDAYS
    prev = _previous_trading_day(datetime.now(_KST).date(), holidays)

    # 1. 결측 종목 백필 (타깃 한정이라 경량)
    if provider is not None:
        missing = await _symbols_missing_latest(session_factory, prev)
        if missing:
            summary = await collect_daily_ohlcv(provider, missing)
            logger.info(
                "job.pre_open_prep.backfill",
                missing=len(missing),
                succeeded=summary.succeeded,
                failed=summary.failed,
                rows=summary.total_rows,
            )

    # 2. 신선도 게이트 검증
    ok, detail = await _check_data_freshness(
        session_factory,
        holidays=holidays,
        min_coverage_pct=settings.DATA_FRESHNESS_MIN_COVERAGE_PCT,
    )
    logger.info("job.pre_open_prep.freshness", ok=ok, **detail)
    if not ok and telegram_bot is not None:
        await telegram_bot.send_message(
            f"⚠️ <b>데이터 신선도 미달</b> — 직전 거래일 {detail['prev_trading_day']} "
            f"커버리지 {detail['coverage_pct']}% (최신일 {detail['max_date']}). "
            f"결정 잡이 오늘 매매를 보류할 수 있습니다."
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


def _holiday_set(holidays: str) -> set[str]:
    return {d.strip() for d in holidays.split(",") if d.strip()} if holidays else set()


def _previous_trading_day(ref: date, holidays: str = "") -> date:
    """ref 직전(이전)의 거래일(주말·공휴일 제외)을 반환."""
    hol = _holiday_set(holidays)
    d = ref - timedelta(days=1)
    while d.weekday() >= 5 or d.strftime("%Y-%m-%d") in hol:
        d -= timedelta(days=1)
    return d


def _market_close_dt(market_close: str = "15:30") -> datetime:
    """오늘(KST) 장 마감 시각의 tz-aware datetime — 결정 큐 만료 기준."""
    h, m = map(int, market_close.split(":"))
    return datetime.now(_KST).replace(hour=h, minute=m, second=0, microsecond=0)


async def _check_data_freshness(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    holidays: str = "",
    min_coverage_pct: float = 95.0,
) -> tuple[bool, dict]:
    """직전 거래일 일봉의 신선도·커버리지 검증.

    ok = (daily_ohlcv 최신일 ≥ 직전 거래일) AND (직전 거래일 커버리지 ≥ 임계).
    stale 데이터로 매매가 진행되는 것을 결정 잡 진입부에서 차단하기 위함.
    """
    prev = _previous_trading_day(datetime.now(_KST).date(), holidays)
    async with session_factory() as session:
        active = await session.scalar(
            select(func.count())
            .select_from(StockMaster)
            .where(
                StockMaster.is_active.is_(True),
                StockMaster.market_type.in_(["kospi", "kosdaq"]),
            )
        )
        covered = await session.scalar(
            select(func.count(func.distinct(DailyOHLCV.symbol))).where(
                DailyOHLCV.date == prev
            )
        )
        max_date = await session.scalar(select(func.max(DailyOHLCV.date)))
    active = int(active or 0)
    covered = int(covered or 0)
    coverage_pct = (covered / active * 100) if active else 0.0
    ok = max_date is not None and max_date >= prev and coverage_pct >= min_coverage_pct
    detail = {
        "prev_trading_day": prev.isoformat(),
        "max_date": max_date.isoformat() if max_date else None,
        "active": active,
        "covered": covered,
        "coverage_pct": round(coverage_pct, 1),
    }
    return ok, detail


async def _symbols_missing_latest(
    session_factory: async_sessionmaker[AsyncSession], prev_day: date
) -> list[str]:
    """직전 거래일 일봉이 없는 활성 종목 목록 — pre_open_prep 백필 대상."""
    async with session_factory() as session:
        active_rows = await session.execute(
            select(StockMaster.symbol).where(
                StockMaster.is_active.is_(True),
                StockMaster.market_type.in_(["kospi", "kosdaq"]),
            )
        )
        active = {row[0] for row in active_rows.all()}
        have_rows = await session.execute(
            select(DailyOHLCV.symbol).where(DailyOHLCV.date == prev_day).distinct()
        )
        have = {row[0] for row in have_rows.all()}
    return sorted(active - have)


async def _execute_buy_decisions(
    *,
    buy_decisions: list[TradeDecision],
    meta_by_symbol: dict[str, dict],
    order_executor: OrderExecutor,
    strategy_type: str,
    account_id: str,
    account_label: str,
    allocator: BatchBudgetAllocator | None = None,
    alloc_session_id: uuid.UUID | None = None,
) -> list[tuple[str, bool, int | None]]:
    """BUY 결정 리스트를 배분→발주하는 공유 실행 코어.

    결정/실행 분리 후 ``job_execution_drain``이 호출한다. 각 결정은
    ``meta_by_symbol[symbol]``에서 session_id/snapshot을 가져온다. 장 운영시간
    가드는 호출자(드레인)가 담당한다.

    Returns:
        (symbol, success, order_id) 튜플 리스트.
    """
    if not buy_decisions:
        return []

    # 배치 예산 배분: 후보들을 가용 현금에 맞춰 순위·재사이징·필터.
    if allocator is not None:
        original_count = len(buy_decisions)
        buy_decisions = await allocator.allocate(
            buy_decisions,
            account_id=account_id,
            session_id=alloc_session_id or uuid.uuid4(),
        )
        logger.info(
            "job.buy_execution.batch_allocated",
            account_id=account_id,
            original_count=original_count,
            allocated_count=len(buy_decisions),
        )
        if not buy_decisions:
            return []

    # 배치 누적 한도 게이트용 in-flight 예약 (F-04). 이 배치에서 접수한 진입을
    # 누적해 후속 후보의 MAX_HOLDINGS/MAX_DAILY_TRADES/섹터 한도 검증에 반영한다.
    reservation = BatchReservation()
    results: list[tuple[str, bool, int | None]] = []
    for td in buy_decisions:
        meta = meta_by_symbol.get(td.symbol, {})
        try:
            exec_result = await order_executor.execute_entry(
                trade_decision=td,
                session_id=meta.get("session_id") or uuid.uuid4(),
                strategy_type=strategy_type,
                account_id=account_id,
                account_label=account_label,
                batch_reservation=reservation,
                entry_analysis_snapshot=meta.get("snapshot"),
                reference_price=meta.get("reference_price"),
            )
            order_id = getattr(exec_result, "order_id", None)
            results.append((td.symbol, exec_result.success, order_id))
            if exec_result.success:
                logger.info(
                    "job.buy_execution.success",
                    symbol=td.symbol,
                    quantity=td.quantity,
                    price=str(td.price),
                    order_id=order_id,
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
            results.append((td.symbol, False, None))
    return results


async def _enqueue_buy_decisions(
    result: PipelineResult,
    *,
    queue: TradeDecisionQueueManager,
    account_id: str,
    strategy_type: str,
    market_close: str,
    holidays: str = "",
) -> int:
    """PipelineResult의 BUY 결정을 결정 큐에 적재(발주 안 함). 적재 수 반환.

    진입 분석 스냅샷(메모리 학습용)을 결정 시점에 만들어 함께 보관해, 실행
    드레인이 PipelineResult 없이도 스냅샷을 확보하게 한다. 만료 기준은 오늘 장
    마감 시각 — 다음날 드레인이 미실행분을 stale로 만료시킨다.
    """
    from src.strategy.memory_manager import build_entry_snapshot

    buy_decisions = [
        td for td in result.trade_decisions if td.action == DecisionAction.BUY
    ]
    if not buy_decisions:
        return 0
    expires = _market_close_dt(market_close)
    enqueued = 0
    for td in buy_decisions:
        snapshot = build_entry_snapshot(td.symbol, result)
        try:
            await queue.enqueue(
                account_id=account_id,
                decision=td,
                session_id=result.session_id,
                strategy_type=strategy_type,
                entry_snapshot=snapshot,
                expires_at=expires,
            )
            enqueued += 1
        except Exception:
            logger.exception(
                "job.decision.enqueue_error", symbol=td.symbol, account_id=account_id
            )
    return enqueued


async def job_swing_decision(
    *,
    orchestrator: PipelineOrchestrator,
    symbols: list[str],
    queue: TradeDecisionQueueManager,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    strategy: Strategy | None = None,
    account_id: str = "default",
    investment_prompt: str = "",
    risk_tolerance: str = "moderate",
    account_label: str = "",
    market_close: str = "15:30",
    holidays: str = "",
    telegram_bot: TelegramBot | None = None,
) -> None:
    """스윙 전략 결정 — 개장 전 08:30 KST. scan→LLM 결정→결정 큐 적재(발주 안 함).

    실행은 개장 후 job_execution_drain이 당일가/갭 게이트를 통과한 건만 처리한다.
    진입부에서 데이터 신선도를 독립 검증해 stale 데이터 매매를 차단한다. 계좌별.
    """
    ok, detail = await _check_data_freshness(
        session_factory,
        holidays=holidays,
        min_coverage_pct=settings.DATA_FRESHNESS_MIN_COVERAGE_PCT,
    )
    if not ok:
        logger.warning("job.swing_decision.skip_stale", account_id=account_id, **detail)
        if telegram_bot is not None:
            await telegram_bot.send_message(
                f"⛔ <b>스윙 결정 보류</b> — 데이터 신선도 미달(커버리지 "
                f"{detail['coverage_pct']}%, 최신일 {detail['max_date']}). {account_label}"
            )
        return

    # scan_universe()로 필터링, 없으면 전체 watchlist fallback
    if strategy is not None:
        target_symbols = await strategy.scan_universe()
        logger.info(
            "job.swing_decision.filtered",
            total=len(symbols),
            filtered=len(target_symbols),
            account_id=account_id,
        )
    else:
        target_symbols = symbols

    if not target_symbols:
        logger.info("job.swing_decision.skip", reason="no_target_symbols", account_id=account_id)
        return

    result = await orchestrator.execute(
        target_symbols,
        investment_prompt=investment_prompt,
        risk_tolerance=risk_tolerance,
        account_id=account_id,
    )
    enqueued = await _enqueue_buy_decisions(
        result,
        queue=queue,
        account_id=account_id,
        strategy_type="swing",
        market_close=market_close,
        holidays=holidays,
    )
    logger.info(
        "job.swing_decision.enqueued",
        count=enqueued,
        session_id=str(result.session_id),
        symbols_count=len(target_symbols),
        account_id=account_id,
    )


async def job_position_decision(
    *,
    orchestrator: PipelineOrchestrator,
    position_manager: PositionManager,
    queue: TradeDecisionQueueManager,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    strategy: Strategy | None = None,
    account_id: str = "default",
    investment_prompt: str = "",
    risk_tolerance: str = "moderate",
    account_label: str = "",
    market_close: str = "15:30",
    holidays: str = "",
    telegram_bot: TelegramBot | None = None,
) -> None:
    """포지션 전략 결정 — 개장 전 08:30 KST(평일). 신규 후보+보유 분석→결정 큐 적재.

    유니버스 스캔(scan_universe, top-N + SMA20>SMA60)으로 신규 진입 후보를 발굴하고
    보유 종목과 합쳐 한 번에 분석한다. 실행은 개장 후 job_execution_drain이 처리하며,
    신선도 게이트로 stale 매매를 차단한다.
    """
    ok, detail = await _check_data_freshness(
        session_factory,
        holidays=holidays,
        min_coverage_pct=settings.DATA_FRESHNESS_MIN_COVERAGE_PCT,
    )
    if not ok:
        logger.warning("job.position_decision.skip_stale", account_id=account_id, **detail)
        if telegram_bot is not None:
            await telegram_bot.send_message(
                f"⛔ <b>포지션 결정 보류</b> — 데이터 신선도 미달(커버리지 "
                f"{detail['coverage_pct']}%, 최신일 {detail['max_date']}). {account_label}"
            )
        return

    positions = await position_manager.get_open(account_id=account_id)
    held = {p.symbol for p in positions}

    # 신규 진입 후보 — 유니버스 스캔(top-N + SMA20>SMA60). 보유 종목과 합쳐 한 번에 분석.
    candidates: set[str] = set()
    if strategy is not None:
        candidates = set(await strategy.scan_universe())

    symbols = list(held | candidates)
    if not symbols:
        logger.info(
            "job.position_decision.skip", reason="no_symbols", account_id=account_id
        )
        return

    result = await orchestrator.execute(
        symbols,
        investment_prompt=investment_prompt,
        risk_tolerance=risk_tolerance,
        account_id=account_id,
    )
    enqueued = await _enqueue_buy_decisions(
        result,
        queue=queue,
        account_id=account_id,
        strategy_type="position",
        market_close=market_close,
        holidays=holidays,
    )
    logger.info(
        "job.position_decision.enqueued",
        count=enqueued,
        session_id=str(result.session_id),
        held=len(held),
        candidates=len(candidates),
        symbols=len(symbols),
        account_id=account_id,
    )


async def job_execution_drain(
    *,
    queue: TradeDecisionQueueManager,
    order_executor: OrderExecutor,
    broker: BrokerInterface,
    strategy_type: str,
    allocator: BatchBudgetAllocator | None = None,
    account_id: str = "default",
    account_label: str = "",
    market_open: str = "09:00",
    market_close: str = "15:30",
    holidays: str = "",
    gap_guard_pct: float = 3.0,
) -> None:
    """개장 후 실행 드레인 — 결정 큐 pending을 당일가/갭 게이트 통과분만 발주. 계좌별.

    장중 주기적으로 실행한다. 각 pending에 대해 실시간가를 조회하여 결정 시점
    기준가(reference_price) 대비 갭이 한도(gap_guard_pct%) 안이면 라이브가로 진입가를
    갱신해 발주하고, 벗어나면 expired(gap_guard) 처리한다. 만료(전일 이월)분은 먼저
    정리한다.
    """
    if not _is_market_open(market_open, market_close, holidays):
        logger.debug("job.execution_drain.skip", reason="market_closed", account_id=account_id)
        return

    pending = await queue.get_pending(account_id)
    if not pending:
        return

    now = datetime.now(UTC)
    # 1. 만료(전일 이월 등) 정리 — stale 결정이 다음날 발주되는 것 방지.
    expired_ids = [
        p.id for p in pending if p.expires_at is not None and now >= p.expires_at
    ]
    if expired_ids:
        await queue.mark_expired(expired_ids, "expired_eod")
        expired_set = set(expired_ids)
        pending = [p for p in pending if p.id not in expired_set]

    # 2. 당일가/갭 게이트 + 라이브가로 진입가 갱신.
    gap_limit = Decimal(str(gap_guard_pct))
    passed: list[TradeDecision] = []
    meta_by_symbol: dict[str, dict] = {}
    gap_failed_ids: list[int] = []
    for p in pending:
        try:
            price_info = await broker.get_price(p.symbol)
            live = price_info.current_price
        except Exception:
            logger.warning(
                "job.execution_drain.price_error", symbol=p.symbol, account_id=account_id
            )
            continue
        ref = p.reference_price
        gap = (abs(live - ref) / ref * _HUNDRED) if ref > _ZERO else _ZERO
        if gap > gap_limit:
            gap_failed_ids.append(p.id)
            logger.info(
                "job.execution_drain.gap_skip",
                symbol=p.symbol,
                reference=str(ref),
                live=str(live),
                gap_pct=str(gap.quantize(_Q2, rounding=ROUND_HALF_UP)),
                account_id=account_id,
            )
            continue
        # 라이브가로 진입가 갱신(밴드 내) — 지정가 체결성 확보.
        passed.append(p.decision.model_copy(update={"price": live}))
        meta_by_symbol[p.symbol] = {
            "session_id": p.session_id,
            "snapshot": p.entry_analysis_snapshot,
            "pending_id": p.id,
            # F-16: 전략이 손절/익절을 산출한 기준가(=결정가). 갭으로 진입가가
            # 벌어졌을 때 executor가 손절/익절 비율 보존 + 수량 재사이징에 사용.
            "reference_price": p.reference_price,
        }

    if gap_failed_ids:
        await queue.mark_expired(gap_failed_ids, "gap_guard")
    if not passed:
        return

    alloc_session = next(iter(meta_by_symbol.values()))["session_id"]
    results = await _execute_buy_decisions(
        buy_decisions=passed,
        meta_by_symbol=meta_by_symbol,
        order_executor=order_executor,
        strategy_type=strategy_type,
        account_id=account_id,
        account_label=account_label,
        allocator=allocator,
        alloc_session_id=alloc_session,
    )

    executed = 0
    for symbol, success, order_id in results:
        if success:
            pid = meta_by_symbol.get(symbol, {}).get("pending_id")
            if pid is not None:
                await queue.mark_executed(pid, order_id)
            executed += 1
    logger.info(
        "job.execution_drain.done",
        account_id=account_id,
        pending=len(pending),
        passed=len(passed),
        gap_skipped=len(gap_failed_ids),
        executed=executed,
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
    account_id: str = "default",
    account_label: str = "",
    market_open: str = "09:00",
    market_close: str = "15:30",
    holidays: str = "",
    coordinator: ExitCoordinator | None = None,
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

            # 트레일링 활성 여부(F-10 B2): 미실현 수익이 전략 임계 이상일 때만 트레일링이
            # bite. 임계 미달이면 익절가 도달 시 즉시 매도(아래 분기)로 처리.
            trailing_on = (
                position.trailing_stop_pct is not None
                and is_trailing_active(position.strategy_type, unrealized_pnl_pct)
            )

            signal = exit_checker.check_take_profit(position, current_price, unrealized_pnl_pct)
            if signal:
                pq = partial_tp_quantity(position.strategy_type, position.quantity)
                if pq is not None and position.take_profit_price is not None:
                    # F-10 Phase 2: POSITION이 +3ATR(익절가) 도달 → pq주 부분익절 후
                    # 잔량은 트레일링으로 전환(체결 시 take_profit_price 소거 → 재발화 차단).
                    signal.exit_quantity = pq
                    signal.reason = ExitReason.PARTIAL_TAKE_PROFIT
                    logger.info(
                        "job.partial_take_profit",
                        symbol=position.symbol,
                        current_price=str(current_price),
                        take_profit_price=str(position.take_profit_price),
                        position_qty=position.quantity,
                        partial_qty=pq,
                    )
                    exit_signals.append(signal)
                    continue
                if trailing_on:
                    # 트레일링 활성 + 익절가 도달: 즉시 매도가 아니라 고점 추적을 계속해
                    # 추가 상승을 노린다(트레일링 스톱 모드 전환).
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

            # 트레일링 스톱 (활성 상태에서만). POSITION은 ATR×배수로 폭을 동적 산출(B3),
            # SWING/ATR 결측은 저장된 trailing_stop_pct(고정/폴백) 사용.
            if trailing_on:
                atr = await _position_trailing_atr(broker, position)
                baseline = position.highest_price or position.entry_price
                ts_price = trailing_stop_price(
                    position.strategy_type,
                    entry_price=position.entry_price,
                    baseline_high=baseline,
                    stored_pct=position.trailing_stop_pct,
                    atr=atr,
                )
                if ts_price is not None:
                    signal = exit_checker.check_trailing_stop(
                        position,
                        current_price,
                        unrealized_pnl_pct,
                        ts_price,
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
        sym_to_pos = {p.symbol: p for p in positions}

        # 이중 청산 방지: WS(StopLossStreamService)/이전 사이클이 in-flight로 잡은
        # 포지션은 스킵하고, 선점 성공한 것만 발주한다(coordinator 미주입 시 전량 발주).
        signals_to_run = exit_signals
        # symbol → (position_id, phase) — 복합키(F-10 Phase 2)로 부분익절/보호 레그 분리.
        claimed: dict[str, tuple[int, str]] = {}
        if coordinator is not None:
            signals_to_run = []
            for sig in exit_signals:
                pos = sym_to_pos.get(sig.symbol)
                if pos is None:
                    signals_to_run.append(sig)  # 매칭 실패는 exit_service가 스킵
                    continue
                phase = exit_phase(sig.reason)
                if await coordinator.try_claim(pos.id, phase):
                    claimed[sig.symbol] = (pos.id, phase)
                    signals_to_run.append(sig)
                else:
                    logger.info(
                        "job.stop_loss_check.skip_inflight",
                        symbol=sig.symbol, account_id=account_id,
                    )

        results: list = []
        try:
            if signals_to_run:
                results = await exit_service.process_exit_signals(
                    signals_to_run,
                    positions,
                    session_id=session_id,
                    account_id=account_id,
                    account_label=account_label,
                )
        finally:
            # 발주 실패(또는 예외) 클레임만 해제해 재시도 허용. 성공 클레임은 TTL까지 보유.
            if coordinator is not None and claimed:
                failed = {r.symbol for r in results if not r.success}
                for sym, (pid, phase) in claimed.items():
                    if not results or sym in failed:
                        await coordinator.release(pid, phase)
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


async def job_cleanup_expired_memories(
    *,
    memory_manager: AgentMemoryManager,
) -> None:
    """만료된 에이전트 학습 메모리 비활성화(is_active=False). Daily 1회."""
    count = await memory_manager.cleanup_expired()
    logger.info("job.cleanup_expired_memories.done", deactivated=count)
