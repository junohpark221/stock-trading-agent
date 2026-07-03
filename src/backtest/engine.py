"""BacktestEngine — 날짜별 시뮬레이션 루프.

기존 전략/리스크/지표 모듈을 재사용하되,
PipelineOrchestrator(LLM) 의존성을 제거하고
기술 지표로 직접 시그널을 생성한다.

Usage::

    loader = HistoricalDataLoader(session_factory)
    await loader.load(symbols=["005930", "000660"], start_date=..., end_date=...)

    broker = SimulatedBroker(data_loader=loader, initial_capital=Decimal("10_000_000"))
    engine = BacktestEngine(data_loader=loader, broker=broker, settings=get_settings())
    result = await engine.run(config)
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from math import isnan
from uuid import uuid4

import pandas as pd
import structlog
from ta.volatility import AverageTrueRange

from src.analysis.technical.indicators import (
    calculate_bollinger_bands,
    calculate_macd,
    calculate_rsi,
    calculate_sma,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.enums import (
    AgentType,
    BacktestMode,
    BacktestStatus,
    OrderSide,
    OrderType,
    SignalAction,
    StrategyType,
)
from src.core.models import (
    BacktestConfig,
    BacktestResult,
    BacktestTradeRecord,
    OrderRequest,
    PerformanceMetrics,
    Signal,
)
from src.db.models.strategy import PortfolioSnapshot, PositionRecord
from src.report.metrics import PerformanceCalculator
from src.strategy.exit_calculator import ExitPriceCalculator
from src.strategy.exit_checker import ExitConditionChecker
from src.strategy.sizing import PositionSizer
from src.strategy.trailing import is_trailing_active, trailing_stop_price

from .data_loader import HistoricalDataLoader
from .llm_replay import LLMReplayProvider
from .simulator import SimulatedBroker

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_Q2 = Decimal("0.01")

# ── Position 전략 기본 파라미터 ──────────────────────────────────────────
_POS_DEFAULTS: dict[str, int | float] = {
    "sma_short": 20,
    "sma_long": 60,
    "rsi_threshold": 40,
    "atr_stop_mult": 2.0,
    "atr_tp_mult": 3.0,
    "trailing_stop_pct": 5.0,
    "max_holding_days": 60,
}

# ── Swing 전략 기본 파라미터 ────────────────────────────────────────────
_SWING_DEFAULTS: dict[str, int | float] = {
    "stop_pct": 3.0,
    "tp_pct": 5.0,
    "min_signals": 2,
    "max_holding_days": 10,
    # F-10: 라이브 SWING 트레일링(고정 5%)과 정합. 이전엔 백테스트만 트레일링 부재.
    "trailing_stop_pct": 5.0,
    # F-13: ATR 연동 청산(clamp+R:R). params["swing_atr_exit"]=True 일 때만 적용
    # (게이트 측정용 처리군). 라이브 SwingTradingStrategy 상수와 동일 기본값.
    "atr_stop_mult": 1.5,
    "stop_floor_pct": 2.5,
    "stop_cap_pct": 6.0,
    "rr_ratio": 1.67,
}


class BacktestEngine:
    """백테스팅 엔진 — 날짜별 시뮬레이션 루프.

    기존 전략/리스크/지표 모듈을 재사용하되,
    PipelineOrchestrator(LLM) 의존성을 제거하고
    기술 지표로 직접 시그널을 생성한다.
    """

    def __init__(
        self,
        *,
        data_loader: HistoricalDataLoader,
        broker: SimulatedBroker,
        settings: object,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._data_loader = data_loader
        self._broker = broker
        self._settings = settings
        self._session_factory = session_factory

        # 재사용 모듈
        self._sizer = PositionSizer(settings)  # type: ignore[arg-type]
        self._exit_checker = ExitConditionChecker(
            max_drawdown_pct=Decimal(str(settings.MAX_DRAWDOWN_PCT)),  # type: ignore[attr-defined]
        )

        # 인메모리 상태 — run() 시작 시 _reset_state()로 초기화
        self._positions: dict[str, PositionRecord] = {}
        self._closed_positions: list[PositionRecord] = []
        self._snapshots: list[PortfolioSnapshot] = []
        self._trades: list[BacktestTradeRecord] = []
        self._pending_signals: list[Signal] = []
        self._highest_prices: dict[str, Decimal] = {}
        self._position_id_counter: int = 0
        self._snapshot_id_counter: int = 0
        self._peak_value: Decimal = _ZERO
        self._daily_trade_count: int = 0
        self._daily_realized_pnl: Decimal = _ZERO
        self._llm_replay: LLMReplayProvider | None = None

    # ══════════════════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════════════════

    async def run(self, config: BacktestConfig) -> BacktestResult:
        """백테스트 메인 루프.

        1. 데이터 로드 확인
        2. 거래일 루프:
           a. broker.set_current_date()
           b. 청산 조건 체크 (open positions)
           c. 전일 시그널 주문 실행 (T-1 close → T open)
           d. 시그널 생성 (T close 기준)
           e. 일별 스냅샷 기록
        3. 잔여 포지션 강제 청산
        4. PerformanceCalculator로 성과 계산
        5. BacktestResult 반환
        """
        run_id = uuid4()
        started_at = datetime.now(timezone.utc)

        try:
            self._reset_state()
            self._peak_value = config.initial_capital

            # ── Mode 2: LLM Replay 초기화 ────────────────────────
            if config.mode == BacktestMode.LLM_REPLAY:
                if self._session_factory is None:
                    raise ValueError(
                        "session_factory is required for LLM_REPLAY mode"
                    )
                self._llm_replay = LLMReplayProvider(self._session_factory)
                loaded = await self._llm_replay.load(
                    start_date=config.start_date,
                    end_date=config.end_date,
                    symbols=config.symbols,
                    llm_model_filter=config.llm_model_filter,
                )
                logger.info("llm_replay_initialized", loaded_count=loaded)

            # 거래일 범위 필터
            all_dates = self._data_loader.get_trading_dates()
            trading_dates = [
                d for d in all_dates
                if config.start_date <= d <= config.end_date
            ]

            # 엣지 케이스: 거래일 없거나 종목 없음
            if not trading_dates or not config.symbols:
                return BacktestResult(
                    run_id=run_id,
                    config=config,
                    status=BacktestStatus.COMPLETED,
                    trades=[],
                    total_trades=0,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc),
                )

            logger.info(
                "backtest_started",
                run_id=str(run_id),
                strategy=config.strategy_type.value,
                mode=config.mode.value,
                symbols=len(config.symbols),
                trading_days=len(trading_dates),
                initial_capital=str(config.initial_capital),
            )

            # ── 메인 루프 ────────────────────────────────────────────
            for trading_date in trading_dates:
                self._daily_trade_count = 0
                self._daily_realized_pnl = _ZERO

                # 브로커 날짜 설정 → 포지션 평가 갱신
                self._broker.set_current_date(trading_date)

                # 1) 청산 조건 체크 + 즉시 실행
                await self._check_exits(trading_date, config)

                # 2) 전일 시그널 → 오늘 시가 체결
                await self._execute_pending_signals(trading_date, config)

                # 3) 새 시그널 생성 (오늘 종가 기준)
                self._generate_signals(trading_date, config)

                # 4) 일별 스냅샷
                self._record_snapshot(trading_date)

            # ── 잔여 포지션 강제 청산 ────────────────────────────────
            if self._positions:
                await self._force_close_all(trading_dates[-1], config)

            # ── 성과 계산 ────────────────────────────────────────────
            metrics = self._calculate_metrics(config)

            result = BacktestResult(
                run_id=run_id,
                config=config,
                status=BacktestStatus.COMPLETED,
                metrics=metrics,
                trades=list(self._trades),
                total_trades=len(self._trades),
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )

            logger.info(
                "backtest_completed",
                run_id=str(run_id),
                total_trades=result.total_trades,
                total_return=str(metrics.total_return_pct) if metrics else None,
            )
            return result

        except Exception as e:
            logger.exception("backtest_failed", run_id=str(run_id), error=str(e))
            return BacktestResult(
                run_id=run_id,
                config=config,
                status=BacktestStatus.FAILED,
                error_message=str(e),
                trades=list(self._trades),
                total_trades=len(self._trades),
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )

    # ══════════════════════════════════════════════════════════════════════
    # Read-only Properties (for BacktestReporter)
    # ══════════════════════════════════════════════════════════════════════

    @property
    def closed_positions(self) -> list[PositionRecord]:
        """청산된 포지션 (reporter용 읽기 전용)."""
        return list(self._closed_positions)

    @property
    def snapshots(self) -> list[PortfolioSnapshot]:
        """일별 스냅샷 (reporter용 읽기 전용)."""
        return list(self._snapshots)

    # ══════════════════════════════════════════════════════════════════════
    # 청산 체크
    # ══════════════════════════════════════════════════════════════════════

    async def _check_exits(self, trading_date: date, config: BacktestConfig) -> None:
        """보유 포지션의 청산 조건 체크 + 즉시 실행.

        각 포지션에 대해 ExitConditionChecker의 4가지 조건을 순차 체크한다.
        첫 번째 발생 시그널로 당일 종가에 LIMIT SELL 청산.
        """
        symbols_to_exit: list[tuple[str, str]] = []  # (symbol, exit_reason)

        for symbol, pos in list(self._positions.items()):
            close = self._data_loader.get_close_price(symbol, trading_date)
            if close is None:
                continue

            pnl_pct = (close - pos.entry_price) / pos.entry_price * _HUNDRED

            # 트레일링 스톱용 최고가 갱신
            prev_high = self._highest_prices.get(symbol, pos.entry_price)
            self._highest_prices[symbol] = max(prev_high, close)

            # 4가지 청산 조건 순차 체크 (우선순위: SL > Trailing > TP > Time)
            exit_signal = self._exit_checker.check_stop_loss(pos, close, pnl_pct)

            if (
                exit_signal is None
                and pos.trailing_stop_pct is not None
                and is_trailing_active(pos.strategy_type, pnl_pct)
            ):
                # 트레일링 활성화 게이트·폭 산출은 공유 헬퍼 사용(라이브와 동일, F-10).
                trailing_price = trailing_stop_price(
                    pos.strategy_type,
                    entry_price=pos.entry_price,
                    baseline_high=self._highest_prices[symbol],
                    stored_pct=pos.trailing_stop_pct,
                )
                if trailing_price is not None:
                    exit_signal = self._exit_checker.check_trailing_stop(
                        pos, close, pnl_pct, trailing_price,
                    )

            if exit_signal is None:
                exit_signal = self._exit_checker.check_take_profit(pos, close, pnl_pct)

            if exit_signal is None:
                time_urgency = (
                    "next_session"
                    if config.strategy_type == StrategyType.POSITION
                    else "end_of_day"
                )
                exit_signal = self._exit_checker.check_time_based(
                    pos, close, pnl_pct, trading_date, time_urgency=time_urgency,
                )

            if exit_signal is not None:
                symbols_to_exit.append((symbol, exit_signal.reason.value))

        # 청산 실행 (순회 중 dict 변경을 피하기 위해 분리)
        for symbol, exit_reason in symbols_to_exit:
            await self._execute_exit(symbol, trading_date, exit_reason)

    async def _execute_exit(
        self, symbol: str, trading_date: date, exit_reason: str,
    ) -> None:
        """단일 포지션 청산 — 당일 종가 LIMIT SELL."""
        pos = self._positions.get(symbol)
        if pos is None:
            return

        close = self._data_loader.get_close_price(symbol, trading_date)
        if close is None:
            return

        order = OrderRequest(
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=pos.quantity,
            price=close,
            reason=f"backtest_exit_{exit_reason}",
        )
        result = await self._broker.place_order(order)

        # 실현 손익 계산
        realized_pnl = (close - pos.avg_cost) * Decimal(pos.quantity) - result.commission

        # PositionRecord 업데이트
        pos.exit_price = close
        pos.exit_date = trading_date
        pos.exit_reason = exit_reason
        pos.realized_pnl = realized_pnl
        pos.status = "closed"

        # 이동
        self._closed_positions.append(pos)
        del self._positions[symbol]
        self._highest_prices.pop(symbol, None)

        # 일일 실현 손익 누적
        self._daily_realized_pnl += realized_pnl
        self._daily_trade_count += 1

        # 거래 기록
        self._trades.append(BacktestTradeRecord(
            symbol=symbol,
            side=OrderSide.SELL,
            quantity=pos.quantity,
            price=close,
            commission=result.commission,
            slippage=_ZERO,  # LIMIT 주문이므로 슬리피지 없음
            trade_date=trading_date,
            pnl=realized_pnl,
            exit_reason=exit_reason,
        ))

    # ══════════════════════════════════════════════════════════════════════
    # 진입 주문 실행
    # ══════════════════════════════════════════════════════════════════════

    async def _execute_pending_signals(
        self, trading_date: date, config: BacktestConfig,
    ) -> None:
        """전일 생성 시그널 → 오늘 시가 MARKET 주문 체결."""
        signals = self._pending_signals
        self._pending_signals = []

        for signal in signals:
            if signal.action != SignalAction.BUY:
                continue

            symbol = signal.symbol

            # 이미 보유 중이면 skip
            if symbol in self._positions:
                continue

            open_price = self._data_loader.get_open_price(symbol, trading_date)
            if open_price is None:
                continue

            # 리스크 체크 + 수량 조정
            passed, adjusted_qty = self._risk_check(
                symbol, signal.quantity, open_price,
            )
            if not passed:
                continue

            # MARKET 주문 (시가 + 슬리피지)
            order = OrderRequest(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=adjusted_qty,
                reason="backtest_entry",
            )

            try:
                result = await self._broker.place_order(order)
            except Exception:
                logger.warning("backtest_order_failed", symbol=symbol)
                continue

            fill_price = result.filled_price or open_price
            slippage_amount = abs(fill_price - open_price) * Decimal(adjusted_qty)

            # 인메모리 PositionRecord 생성
            params = config.parameters or {}
            if config.strategy_type == StrategyType.POSITION:
                trailing_pct = Decimal(
                    str(params.get("trailing_stop_pct", _POS_DEFAULTS["trailing_stop_pct"])),
                )
                max_days = int(params.get("max_holding_days", _POS_DEFAULTS["max_holding_days"]))
            else:
                trailing_pct = Decimal(
                    str(params.get("trailing_stop_pct", _SWING_DEFAULTS["trailing_stop_pct"])),
                )
                max_days = int(params.get("max_holding_days", _SWING_DEFAULTS["max_holding_days"]))

            self._position_id_counter += 1
            pos = PositionRecord(
                id=self._position_id_counter,
                symbol=symbol,
                strategy_type=config.strategy_type.value,
                quantity=adjusted_qty,
                avg_cost=fill_price,
                entry_price=fill_price,
                entry_date=trading_date,
                stop_loss_price=signal.stop_loss_price or _ZERO,
                take_profit_price=signal.target_price,
                trailing_stop_pct=trailing_pct,
                max_holding_days=max_days,
                status="open",
            )

            self._positions[symbol] = pos
            self._highest_prices[symbol] = fill_price
            self._daily_trade_count += 1

            # 거래 기록
            self._trades.append(BacktestTradeRecord(
                symbol=symbol,
                side=OrderSide.BUY,
                quantity=adjusted_qty,
                price=fill_price,
                commission=result.commission,
                slippage=slippage_amount,
                trade_date=trading_date,
            ))

    # ══════════════════════════════════════════════════════════════════════
    # 시그널 생성
    # ══════════════════════════════════════════════════════════════════════

    def _generate_signals(self, trading_date: date, config: BacktestConfig) -> None:
        """시그널 생성 — 모드에 따라 분기."""
        if config.mode == BacktestMode.LLM_REPLAY:
            self._generate_replay_signals(trading_date, config)
            return
        # Mode 1: TECHNICAL
        if config.strategy_type == StrategyType.POSITION:
            self._generate_position_signals(trading_date, config)
        else:
            self._generate_swing_signals(trading_date, config)

    def _generate_position_signals(
        self, trading_date: date, config: BacktestConfig,
    ) -> None:
        """Position 전략: SMA20 > SMA60 AND RSI(14) < 40 → BUY.

        ATR 기반 SL/TP, PositionSizer로 수량 결정.
        """
        params = config.parameters or {}
        sma_short = int(params.get("sma_short", _POS_DEFAULTS["sma_short"]))
        sma_long = int(params.get("sma_long", _POS_DEFAULTS["sma_long"]))
        rsi_threshold = float(params.get("rsi_threshold", _POS_DEFAULTS["rsi_threshold"]))
        atr_stop_mult = Decimal(str(params.get("atr_stop_mult", _POS_DEFAULTS["atr_stop_mult"])))
        atr_tp_mult = Decimal(str(params.get("atr_tp_mult", _POS_DEFAULTS["atr_tp_mult"])))

        total_value = self._broker.get_total_value()

        for symbol in config.symbols:
            try:
                self._try_position_signal(
                    symbol, trading_date, config,
                    sma_short, sma_long, rsi_threshold,
                    atr_stop_mult, atr_tp_mult, total_value,
                )
            except Exception:
                logger.warning(
                    "position_signal_failed", symbol=symbol, date=str(trading_date),
                )

    def _try_position_signal(
        self,
        symbol: str,
        trading_date: date,
        config: BacktestConfig,
        sma_short: int,
        sma_long: int,
        rsi_threshold: float,
        atr_stop_mult: Decimal,
        atr_tp_mult: Decimal,
        total_value: Decimal,
    ) -> None:
        """Position 전략 시그널 생성 (단일 종목)."""
        # 이미 보유 중이면 skip
        if symbol in self._positions:
            return

        # pending에 이미 있으면 skip
        if any(s.symbol == symbol for s in self._pending_signals):
            return

        # OHLCV 조회 (처음부터 현재까지)
        all_dates = self._data_loader.get_trading_dates()
        earliest = all_dates[0] if all_dates else trading_date
        df = self._data_loader.get_ohlcv_range(symbol, earliest, trading_date)

        # 최소 봉 수 체크 (SMA long 기간)
        if df.empty or len(df) < sma_long:
            return

        close = df["close"]
        high = df["high"]
        low = df["low"]

        # 기술 지표 계산
        sma_dict = calculate_sma(close, periods=[sma_short, sma_long])
        rsi = calculate_rsi(close, period=14)
        atr = AverageTrueRange(
            high=high, low=low, close=close, window=14,
        ).average_true_range()

        # 마지막 유효값 추출
        sma_s = sma_dict[sma_short].iloc[-1]
        sma_l = sma_dict[sma_long].iloc[-1]
        rsi_val = rsi.iloc[-1]
        atr_val = atr.iloc[-1]

        # NaN 체크
        if any(_is_nan(v) for v in (sma_s, sma_l, rsi_val, atr_val)):
            return

        # 진입 조건: SMA_short > SMA_long AND RSI < threshold
        if not (sma_s > sma_l and rsi_val < rsi_threshold):
            return

        entry_price = self._data_loader.get_close_price(symbol, trading_date)
        if entry_price is None:
            return

        atr_dec = Decimal(str(atr_val))
        sl, tp = ExitPriceCalculator.atr_based(
            entry_price, atr_dec,
            stop_mult=atr_stop_mult, tp_mult=atr_tp_mult,
        )

        # 포지션 사이징
        sizing = self._sizer.calculate(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss_price=sl,
            take_profit_price=tp,
            total_portfolio_value=total_value,
        )
        if sizing.quantity <= 0:
            return

        self._pending_signals.append(Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            confidence=Decimal("0.6"),
            target_price=tp,
            stop_loss_price=sl,
            quantity=sizing.quantity,
            position_value_krw=sizing.position_value_krw,
            reasoning=f"SMA{sma_short}>{sma_long} + RSI({rsi_val:.1f})<{rsi_threshold}",
            source_agent=AgentType.STOCK_ANALYST,
            timestamp=datetime.now(timezone.utc),
        ))

    def _generate_swing_signals(
        self, trading_date: date, config: BacktestConfig,
    ) -> None:
        """Swing 전략: RSI 반등 + MACD 골든크로스 + BB 하단 반등 (2+/3) → BUY.

        고정% SL/TP, PositionSizer로 수량 결정.
        """
        params = config.parameters or {}
        stop_pct = Decimal(str(params.get("stop_pct", _SWING_DEFAULTS["stop_pct"])))
        tp_pct = Decimal(str(params.get("tp_pct", _SWING_DEFAULTS["tp_pct"])))
        min_signals = int(params.get("min_signals", _SWING_DEFAULTS["min_signals"]))

        total_value = self._broker.get_total_value()

        for symbol in config.symbols:
            try:
                self._try_swing_signal(
                    symbol, trading_date, config,
                    stop_pct, tp_pct, min_signals, total_value,
                )
            except Exception:
                logger.warning(
                    "swing_signal_failed", symbol=symbol, date=str(trading_date),
                )

    def _try_swing_signal(
        self,
        symbol: str,
        trading_date: date,
        config: BacktestConfig,
        stop_pct: Decimal,
        tp_pct: Decimal,
        min_signals: int,
        total_value: Decimal,
    ) -> None:
        """Swing 전략 시그널 생성 (단일 종목)."""
        if symbol in self._positions:
            return
        if any(s.symbol == symbol for s in self._pending_signals):
            return

        all_dates = self._data_loader.get_trading_dates()
        earliest = all_dates[0] if all_dates else trading_date
        df = self._data_loader.get_ohlcv_range(symbol, earliest, trading_date)

        # MACD 최소 26봉 + 여유
        if df.empty or len(df) < 30:
            return

        close = df["close"]

        # 기술 지표 계산
        rsi = calculate_rsi(close, period=14)
        macd_line, macd_signal, _ = calculate_macd(close)
        _, _, bb_lower = calculate_bollinger_bands(close, period=20)

        # 최근 2개 값 필요
        if len(rsi) < 2 or len(macd_line) < 2 or len(bb_lower) < 2:
            return

        rsi_prev, rsi_curr = rsi.iloc[-2], rsi.iloc[-1]
        macd_prev, macd_curr = macd_line.iloc[-2], macd_line.iloc[-1]
        sig_prev, sig_curr = macd_signal.iloc[-2], macd_signal.iloc[-1]
        close_prev, close_curr = float(close.iloc[-2]), float(close.iloc[-1])
        bbl_prev, bbl_curr = bb_lower.iloc[-2], bb_lower.iloc[-1]

        # NaN 체크
        vals = [rsi_prev, rsi_curr, macd_prev, macd_curr, sig_prev, sig_curr, bbl_prev, bbl_curr]
        if any(_is_nan(v) for v in vals):
            return

        # 3가지 조건 체크
        signal_count = 0

        # 1) RSI 반등: 전일 < 30 → 금일 >= 30
        if rsi_prev < 30 and rsi_curr >= 30:
            signal_count += 1

        # 2) MACD 골든크로스: 전일 MACD <= Signal → 금일 MACD > Signal
        if macd_prev <= sig_prev and macd_curr > sig_curr:
            signal_count += 1

        # 3) BB 하단 반등: 전일 종가 <= BB 하단 → 금일 종가 > BB 하단
        if close_prev <= bbl_prev and close_curr > bbl_curr:
            signal_count += 1

        if signal_count < min_signals:
            return

        entry_price = self._data_loader.get_close_price(symbol, trading_date)
        if entry_price is None:
            return

        sl, tp = self._swing_exit_prices(
            symbol, trading_date, entry_price,
            config.parameters or {}, stop_pct, tp_pct,
        )

        sizing = self._sizer.calculate(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss_price=sl,
            take_profit_price=tp,
            total_portfolio_value=total_value,
        )
        if sizing.quantity <= 0:
            return

        self._pending_signals.append(Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            confidence=Decimal("0.5"),
            target_price=tp,
            stop_loss_price=sl,
            quantity=sizing.quantity,
            position_value_krw=sizing.position_value_krw,
            reasoning=f"Swing signals {signal_count}/{min_signals}",
            source_agent=AgentType.STOCK_ANALYST,
            timestamp=datetime.now(timezone.utc),
        ))

    # ══════════════════════════════════════════════════════════════════════
    # Mode 2: LLM Replay 시그널
    # ══════════════════════════════════════════════════════════════════════

    def _generate_replay_signals(
        self, trading_date: date, config: BacktestConfig,
    ) -> None:
        """Mode 2: decision_log 기반 시그널 재생.

        각 종목에 대해 최고 confidence 결정을 선택하여 시그널로 변환한다.
        SL/TP는 strategy_type에 따라 ATR 기반(Position) 또는 고정%(Swing) 적용.
        """
        assert self._llm_replay is not None  # noqa: S101

        params = config.parameters or {}
        total_value = self._broker.get_total_value()

        for symbol in config.symbols:
            try:
                self._try_replay_signal(
                    symbol, trading_date, config, params, total_value,
                )
            except Exception:
                logger.warning(
                    "replay_signal_failed",
                    symbol=symbol,
                    date=str(trading_date),
                )

    def _try_replay_signal(
        self,
        symbol: str,
        trading_date: date,
        config: BacktestConfig,
        params: dict,
        total_value: Decimal,
    ) -> None:
        """Mode 2 시그널 생성 (단일 종목)."""
        # 이미 보유 중이면 skip
        if symbol in self._positions:
            return

        # pending에 이미 있으면 skip
        if any(s.symbol == symbol for s in self._pending_signals):
            return

        assert self._llm_replay is not None  # noqa: S101
        decisions = self._llm_replay.get_decisions(
            target_date=trading_date, symbol=symbol,
        )
        if not decisions:
            return

        # 최고 confidence 선택
        best = max(decisions, key=lambda d: d.confidence)

        current_price = self._data_loader.get_close_price(symbol, trading_date)
        if current_price is None:
            return

        signal = self._llm_replay.to_signal(best, current_price=current_price)
        if signal is None:
            return

        # BUY만 처리 (SELL은 기존 exit 메커니즘이 담당)
        if signal.action != SignalAction.BUY:
            return

        # SL/TP 계산 — strategy_type에 따라 분기
        if config.strategy_type == StrategyType.POSITION:
            atr_val = self._compute_atr(symbol, trading_date)
            if atr_val is None:
                return
            atr_stop_mult = Decimal(
                str(params.get("atr_stop_mult", _POS_DEFAULTS["atr_stop_mult"]))
            )
            atr_tp_mult = Decimal(
                str(params.get("atr_tp_mult", _POS_DEFAULTS["atr_tp_mult"]))
            )
            sl, tp = ExitPriceCalculator.atr_based(
                current_price, atr_val,
                stop_mult=atr_stop_mult, tp_mult=atr_tp_mult,
            )
        else:
            stop_pct = Decimal(
                str(params.get("stop_pct", _SWING_DEFAULTS["stop_pct"]))
            )
            tp_pct = Decimal(
                str(params.get("tp_pct", _SWING_DEFAULTS["tp_pct"]))
            )
            sl, tp = self._swing_exit_prices(
                symbol, trading_date, current_price, params, stop_pct, tp_pct,
            )

        # 포지션 사이징
        sizing = self._sizer.calculate(
            symbol=symbol,
            entry_price=current_price,
            stop_loss_price=sl,
            take_profit_price=tp,
            total_portfolio_value=total_value,
        )
        if sizing.quantity <= 0:
            return

        self._pending_signals.append(Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            confidence=best.confidence,
            target_price=tp,
            stop_loss_price=sl,
            quantity=sizing.quantity,
            position_value_krw=sizing.position_value_krw,
            reasoning=signal.reasoning,
            source_agent=signal.source_agent,
            timestamp=signal.timestamp,
        ))

    # ══════════════════════════════════════════════════════════════════════
    # 공용 헬퍼
    # ══════════════════════════════════════════════════════════════════════

    def _swing_exit_prices(
        self,
        symbol: str,
        trading_date: date,
        entry_price: Decimal,
        params: dict,
        stop_pct: Decimal,
        tp_pct: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Swing 손절/익절가 산출 — 파라미터 구동 ATR clamp(F-13) 또는 고정%.

        ``params["swing_atr_exit"]`` 가 참이고 ATR>0 이면 라이브와 동일한
        ``ExitPriceCalculator.atr_clamped``(손절폭 clamp + R:R 보존), 아니면 기존
        ``fixed_percentage``. 동일 기간·심볼로 대조군(고정 3/5)과 처리군(ATR)을 모두
        실행해 게이트를 측정하기 위한 분기다(라이브 플래그와 무관, params로 제어).
        """
        if params.get("swing_atr_exit"):
            atr_val = self._compute_atr(symbol, trading_date)
            if atr_val is not None and atr_val > _ZERO:
                return ExitPriceCalculator.atr_clamped(
                    entry_price,
                    atr_val,
                    stop_mult=Decimal(
                        str(params.get("atr_stop_mult", _SWING_DEFAULTS["atr_stop_mult"]))
                    ),
                    floor_pct=Decimal(
                        str(params.get("stop_floor_pct", _SWING_DEFAULTS["stop_floor_pct"]))
                    ),
                    cap_pct=Decimal(
                        str(params.get("stop_cap_pct", _SWING_DEFAULTS["stop_cap_pct"]))
                    ),
                    rr_ratio=Decimal(
                        str(params.get("rr_ratio", _SWING_DEFAULTS["rr_ratio"]))
                    ),
                )
        return ExitPriceCalculator.fixed_percentage(entry_price, stop_pct, tp_pct)

    def _compute_atr(self, symbol: str, trading_date: date) -> Decimal | None:
        """ATR(14) 계산 — Position 전략 및 Mode 2에서 재사용."""
        all_dates = self._data_loader.get_trading_dates()
        earliest = all_dates[0] if all_dates else trading_date
        df = self._data_loader.get_ohlcv_range(symbol, earliest, trading_date)

        if df.empty or len(df) < 14:
            return None

        atr = AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"], window=14,
        ).average_true_range()
        val = atr.iloc[-1]

        if _is_nan(val):
            return None

        return Decimal(str(val))

    # ══════════════════════════════════════════════════════════════════════
    # 리스크 체크
    # ══════════════════════════════════════════════════════════════════════

    def _risk_check(
        self, symbol: str, quantity: int, price: Decimal,
    ) -> tuple[bool, int]:
        """간소화된 리스크 체크.

        AlgoRiskManager의 핵심 규칙만 직접 구현:
        포지션 수, 일일 매매, 중복, 비중, 현금.

        Returns
        -------
        (passed, adjusted_quantity)
        """
        settings = self._settings
        active_count = len(self._positions)

        # 최대 보유 종목
        max_positions = getattr(settings, "MAX_PORTFOLIO_POSITIONS", 5)
        if active_count >= max_positions:
            return (False, 0)

        # 일일 매매 한도
        max_daily = getattr(settings, "MAX_DAILY_TRADES", 5)
        if self._daily_trade_count >= max_daily:
            return (False, 0)

        # 중복 보유
        if symbol in self._positions:
            return (False, 0)

        total_value = self._broker.get_total_value()
        adjusted_qty = quantity

        # 단일 종목 비중 캡
        max_pos_pct = Decimal(str(getattr(settings, "MAX_POSITION_PCT", 10.0)))
        max_by_pct = total_value * max_pos_pct / _HUNDRED
        max_qty_pct = int(max_by_pct / price) if price > _ZERO else 0
        adjusted_qty = min(adjusted_qty, max_qty_pct)

        # 단일 종목 금액 캡
        max_pos_krw = Decimal(str(getattr(settings, "MAX_POSITION_SIZE_KRW", 1_000_000)))
        max_qty_krw = int(max_pos_krw / price) if price > _ZERO else 0
        adjusted_qty = min(adjusted_qty, max_qty_krw)

        # 현금 부족
        cash = self._broker._cash
        max_qty_cash = int(cash / price) if price > _ZERO else 0
        adjusted_qty = min(adjusted_qty, max_qty_cash)

        if adjusted_qty <= 0:
            return (False, 0)

        return (True, adjusted_qty)

    # ══════════════════════════════════════════════════════════════════════
    # 스냅샷 / 유틸리티
    # ══════════════════════════════════════════════════════════════════════

    def _record_snapshot(self, trading_date: date) -> None:
        """일별 포트폴리오 스냅샷 기록 (인메모리)."""
        total_value = self._broker.get_total_value()
        cash = self._broker._cash
        invested = total_value - cash

        # 미실현 손익
        unrealized_pnl = _ZERO
        for pos in self._positions.values():
            close = self._data_loader.get_close_price(pos.symbol, trading_date)
            if close is not None:
                unrealized_pnl += (close - pos.avg_cost) * Decimal(pos.quantity)

        # 드로다운
        self._peak_value = max(self._peak_value, total_value)
        drawdown_pct = _ZERO
        if self._peak_value > _ZERO:
            drawdown_pct = (
                (self._peak_value - total_value) / self._peak_value * _HUNDRED
            ).quantize(_Q2, rounding=ROUND_HALF_UP)

        self._snapshot_id_counter += 1
        snap = PortfolioSnapshot(
            id=self._snapshot_id_counter,
            snapshot_date=trading_date,
            total_value=total_value,
            cash=cash,
            invested=invested,
            unrealized_pnl=unrealized_pnl,
            realized_pnl_daily=self._daily_realized_pnl,
            peak_value=self._peak_value,
            drawdown_pct=drawdown_pct,
            positions_count=len(self._positions),
            sector_allocations=None,
            trade_count_daily=self._daily_trade_count,
        )
        self._snapshots.append(snap)

    async def _force_close_all(self, last_date: date, config: BacktestConfig) -> None:
        """잔여 포지션 강제 청산 (시뮬레이션 종료)."""
        symbols = list(self._positions.keys())
        for symbol in symbols:
            await self._execute_exit(symbol, last_date, "backtest_end")

    def _calculate_metrics(self, config: BacktestConfig) -> PerformanceMetrics | None:
        """PerformanceCalculator로 최종 성과 지표 계산."""
        if not self._snapshots:
            return None

        risk_free = Decimal(str(getattr(self._settings, "RISK_FREE_RATE_PCT", 3.5)))
        return PerformanceCalculator.calculate(
            closed_positions=self._closed_positions,
            snapshots=self._snapshots,
            period_start=config.start_date,
            period_end=config.end_date,
            risk_free_rate_pct=risk_free,
        )

    def _reset_state(self) -> None:
        """시뮬레이션 상태 초기화 (run() 시작 시 호출)."""
        self._positions.clear()
        self._closed_positions.clear()
        self._snapshots.clear()
        self._trades.clear()
        self._pending_signals.clear()
        self._highest_prices.clear()
        self._position_id_counter = 0
        self._snapshot_id_counter = 0
        self._peak_value = _ZERO
        self._daily_trade_count = 0
        self._daily_realized_pnl = _ZERO
        self._llm_replay = None


def _is_nan(val: object) -> bool:
    """NaN 체크 (float, numpy.float64 등)."""
    try:
        return isnan(float(val))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
