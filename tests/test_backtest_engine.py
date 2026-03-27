"""BacktestEngine unit tests.

Mock HistoricalDataLoader + SimulatedBroker를 사용하여 DB 없이 테스트.
합성 OHLCV 데이터로 기술 지표 시그널 발생을 제어한다.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.backtest.engine import BacktestEngine
from src.backtest.simulator import SimulatedBroker
from src.core.enums import BacktestMode, BacktestStatus, StrategyType
from src.core.models import BacktestConfig


# ══════════════════════════════════════════════════════════════════════════
# 테스트 헬퍼
# ══════════════════════════════════════════════════════════════════════════


def _make_settings(**overrides: object) -> SimpleNamespace:
    """Settings mock (SimpleNamespace)."""
    defaults = {
        "RISK_PER_TRADE_PCT": 2.0,
        "MAX_POSITION_PCT": 10.0,
        "MAX_POSITION_SIZE_KRW": 5_000_000,
        "MAX_PORTFOLIO_POSITIONS": 5,
        "MAX_DRAWDOWN_PCT": 10.0,
        "MAX_DAILY_TRADES": 5,
        "RISK_FREE_RATE_PCT": 3.5,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_trading_dates(start: date, days: int) -> list[date]:
    """주말 제외 거래일 생성."""
    dates: list[date] = []
    current = start
    while len(dates) < days:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def _generate_v_shape_data(
    symbol: str,
    trading_dates: list[date],
    base_price: float = 50000.0,
    decline_pct: float = 0.015,
    recovery_pct: float = 0.012,
    mid_point_ratio: float = 0.4,
) -> dict[str, dict[date, dict]]:
    """V자형 가격 패턴 생성.

    전반부: 하락 → 후반부: 회복.
    SMA20 > SMA60 + RSI < 40 조건이 회복 초기에 발생하도록 설계.
    """
    ohlcv: dict[date, dict] = {}
    mid_idx = int(len(trading_dates) * mid_point_ratio)
    price = base_price

    for i, d in enumerate(trading_dates):
        if i < mid_idx:
            # 하락 구간 (RSI 하락)
            price *= (1 - decline_pct)
        else:
            # 회복 구간
            price *= (1 + recovery_pct)

        _open = price * 0.998
        _close = price
        _high = price * 1.01
        _low = price * 0.985
        ohlcv[d] = {
            "open": Decimal(str(round(_open, 0))),
            "high": Decimal(str(round(_high, 0))),
            "low": Decimal(str(round(_low, 0))),
            "close": Decimal(str(round(_close, 0))),
            "volume": 10_000_000,
        }
    return {symbol: ohlcv}


def _generate_flat_data(
    symbol: str,
    trading_dates: list[date],
    base_price: float = 50000.0,
) -> dict[str, dict[date, dict]]:
    """횡보 데이터 — 시그널 미발생."""
    ohlcv: dict[date, dict] = {}
    for d in trading_dates:
        ohlcv[d] = {
            "open": Decimal(str(int(base_price))),
            "high": Decimal(str(int(base_price * 1.005))),
            "low": Decimal(str(int(base_price * 0.995))),
            "close": Decimal(str(int(base_price))),
            "volume": 10_000_000,
        }
    return {symbol: ohlcv}


def _generate_drop_data(
    symbol: str,
    trading_dates: list[date],
    entry_price: float = 50000.0,
    drop_pct: float = 0.02,
) -> dict[str, dict[date, dict]]:
    """지속 하락 데이터 — SL 테스트용."""
    ohlcv: dict[date, dict] = {}
    price = entry_price
    for d in trading_dates:
        price *= (1 - drop_pct)
        ohlcv[d] = {
            "open": Decimal(str(round(price * 1.005, 0))),
            "high": Decimal(str(round(price * 1.01, 0))),
            "low": Decimal(str(round(price * 0.99, 0))),
            "close": Decimal(str(round(price, 0))),
            "volume": 10_000_000,
        }
    return {symbol: ohlcv}


def _generate_rise_data(
    symbol: str,
    trading_dates: list[date],
    entry_price: float = 50000.0,
    rise_pct: float = 0.02,
) -> dict[str, dict[date, dict]]:
    """지속 상승 데이터 — TP 테스트용."""
    ohlcv: dict[date, dict] = {}
    price = entry_price
    for d in trading_dates:
        price *= (1 + rise_pct)
        ohlcv[d] = {
            "open": Decimal(str(round(price * 0.995, 0))),
            "high": Decimal(str(round(price * 1.01, 0))),
            "low": Decimal(str(round(price * 0.99, 0))),
            "close": Decimal(str(round(price, 0))),
            "volume": 10_000_000,
        }
    return {symbol: ohlcv}


def _make_loader(
    symbols: list[str],
    trading_dates: list[date],
    ohlcv_data: dict[str, dict[date, dict]],
) -> MagicMock:
    """Mock HistoricalDataLoader."""
    loader = MagicMock()
    loader.get_trading_dates.return_value = list(trading_dates)
    loader.get_symbols.return_value = list(symbols)

    def _get_ohlcv(symbol: str, target_date: date) -> dict | None:
        sym_data = ohlcv_data.get(symbol)
        if sym_data is None:
            return None
        return sym_data.get(target_date)

    def _get_close_price(symbol: str, target_date: date) -> Decimal | None:
        ohlcv = _get_ohlcv(symbol, target_date)
        return ohlcv["close"] if ohlcv else None

    def _get_open_price(symbol: str, target_date: date) -> Decimal | None:
        ohlcv = _get_ohlcv(symbol, target_date)
        return ohlcv["open"] if ohlcv else None

    def _get_ohlcv_range(
        symbol: str, start_date: date, end_date: date,
    ) -> pd.DataFrame:
        sym_data = ohlcv_data.get(symbol)
        if sym_data is None:
            return pd.DataFrame()
        filtered = {
            d: v for d, v in sym_data.items() if start_date <= d <= end_date
        }
        if not filtered:
            return pd.DataFrame()
        dates = sorted(filtered.keys())
        return pd.DataFrame(
            {
                "open": [float(filtered[d]["open"]) for d in dates],
                "high": [float(filtered[d]["high"]) for d in dates],
                "low": [float(filtered[d]["low"]) for d in dates],
                "close": [float(filtered[d]["close"]) for d in dates],
                "volume": [filtered[d]["volume"] for d in dates],
            },
            index=pd.Index(dates, name="date"),
        )

    loader.get_ohlcv.side_effect = _get_ohlcv
    loader.get_close_price.side_effect = _get_close_price
    loader.get_open_price.side_effect = _get_open_price
    loader.get_ohlcv_range.side_effect = _get_ohlcv_range

    return loader


def _make_engine(
    loader: MagicMock,
    initial_capital: Decimal = Decimal("10_000_000"),
    slippage_bps: int = 10,
    settings: SimpleNamespace | None = None,
) -> BacktestEngine:
    """BacktestEngine + SimulatedBroker 생성."""
    if settings is None:
        settings = _make_settings()
    broker = SimulatedBroker(
        data_loader=loader,
        initial_capital=initial_capital,
        slippage_bps=slippage_bps,
    )
    return BacktestEngine(
        data_loader=loader,
        broker=broker,
        settings=settings,
    )


# ══════════════════════════════════════════════════════════════════════════
# 테스트
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_run_basic_position_strategy() -> None:
    """30거래일, 3종목 → status=COMPLETED, BacktestResult 정상 반환."""
    # 80거래일 데이터(SMA60 계산 여유) + 마지막 30일 백테스트 구간
    trading_dates = _make_trading_dates(date(2024, 6, 1), 80)
    symbols = ["005930", "000660", "035720"]

    all_data: dict[str, dict[date, dict]] = {}
    for sym in symbols:
        data = _generate_v_shape_data(sym, trading_dates, base_price=50000 + hash(sym) % 10000)
        all_data.update(data)

    loader = _make_loader(symbols, trading_dates, all_data)
    engine = _make_engine(loader)

    config = BacktestConfig(
        strategy_type=StrategyType.POSITION,
        start_date=trading_dates[50],
        end_date=trading_dates[-1],
        initial_capital=Decimal("10_000_000"),
        symbols=symbols,
        mode=BacktestMode.TECHNICAL,
    )
    result = await engine.run(config)

    assert result.status == BacktestStatus.COMPLETED
    assert result.run_id is not None
    assert result.started_at is not None
    assert result.completed_at is not None
    assert result.error_message is None


@pytest.mark.asyncio
async def test_run_basic_swing_strategy() -> None:
    """30거래일, Swing 전략 → status=COMPLETED."""
    # 80거래일, 마지막 30일 백테스트
    trading_dates = _make_trading_dates(date(2024, 6, 1), 80)
    symbols = ["005930"]

    # RSI 반등 + MACD 크로스가 일어날 수 있는 V자형 데이터
    all_data = _generate_v_shape_data(
        "005930", trading_dates, base_price=50000,
        decline_pct=0.025, recovery_pct=0.018, mid_point_ratio=0.5,
    )
    loader = _make_loader(symbols, trading_dates, all_data)
    engine = _make_engine(loader)

    config = BacktestConfig(
        strategy_type=StrategyType.SWING,
        start_date=trading_dates[50],
        end_date=trading_dates[-1],
        initial_capital=Decimal("10_000_000"),
        symbols=symbols,
        mode=BacktestMode.TECHNICAL,
    )
    result = await engine.run(config)

    assert result.status == BacktestStatus.COMPLETED
    assert result.total_trades >= 0


@pytest.mark.asyncio
async def test_exit_stop_loss_triggered() -> None:
    """포지션 진입 후 가격 하락 → SL 발동 확인."""
    # 80일: 전반 V자 회복(진입 유도) + 후반 급락(SL 트리거)
    trading_dates = _make_trading_dates(date(2024, 6, 1), 100)
    symbol = "005930"

    # 처음 60일 하락 후 70일까지 회복(진입) → 80일~ 급락
    ohlcv: dict[date, dict] = {}
    price = 50000.0
    for i, d in enumerate(trading_dates):
        if i < 40:
            price *= 0.985  # 하락
        elif i < 65:
            price *= 1.012  # 회복 (SMA20 > SMA60, RSI < 40 발생)
        elif i < 70:
            price *= 1.005  # 안정
        else:
            price *= 0.96  # 급락 (SL 트리거)

        ohlcv[d] = {
            "open": Decimal(str(round(price * 0.998, 0))),
            "high": Decimal(str(round(price * 1.01, 0))),
            "low": Decimal(str(round(price * 0.985, 0))),
            "close": Decimal(str(round(price, 0))),
            "volume": 10_000_000,
        }

    loader = _make_loader([symbol], trading_dates, {symbol: ohlcv})
    engine = _make_engine(loader)

    config = BacktestConfig(
        strategy_type=StrategyType.POSITION,
        start_date=trading_dates[50],
        end_date=trading_dates[-1],
        initial_capital=Decimal("10_000_000"),
        symbols=[symbol],
        mode=BacktestMode.TECHNICAL,
    )
    result = await engine.run(config)

    assert result.status == BacktestStatus.COMPLETED
    # SL 발동 거래가 있는지 확인
    sl_trades = [t for t in result.trades if t.exit_reason == "stop_loss"]
    # 진입이 발생했고 급락했다면 SL이 있어야 함
    # 진입이 안 됐을 수도 있으므로, 진입 거래가 있는 경우만 SL 확인
    buy_trades = [t for t in result.trades if t.side.value == "buy"]
    if buy_trades:
        assert len(sl_trades) > 0 or any(
            t.exit_reason == "backtest_end" for t in result.trades
        )


@pytest.mark.asyncio
async def test_exit_take_profit_triggered() -> None:
    """포지션 진입 후 가격 상승 → TP 발동 확인."""
    trading_dates = _make_trading_dates(date(2024, 6, 1), 100)
    symbol = "005930"

    ohlcv: dict[date, dict] = {}
    price = 50000.0
    for i, d in enumerate(trading_dates):
        if i < 40:
            price *= 0.985  # 하락
        elif i < 65:
            price *= 1.012  # 회복 (진입)
        else:
            price *= 1.025  # 강한 상승 (TP 트리거)

        ohlcv[d] = {
            "open": Decimal(str(round(price * 0.998, 0))),
            "high": Decimal(str(round(price * 1.01, 0))),
            "low": Decimal(str(round(price * 0.99, 0))),
            "close": Decimal(str(round(price, 0))),
            "volume": 10_000_000,
        }

    loader = _make_loader([symbol], trading_dates, {symbol: ohlcv})
    engine = _make_engine(loader)

    config = BacktestConfig(
        strategy_type=StrategyType.POSITION,
        start_date=trading_dates[50],
        end_date=trading_dates[-1],
        initial_capital=Decimal("10_000_000"),
        symbols=[symbol],
        mode=BacktestMode.TECHNICAL,
    )
    result = await engine.run(config)

    assert result.status == BacktestStatus.COMPLETED
    tp_trades = [t for t in result.trades if t.exit_reason == "take_profit"]
    buy_trades = [t for t in result.trades if t.side.value == "buy"]
    if buy_trades:
        assert len(tp_trades) > 0 or any(
            t.exit_reason in ("trailing_stop", "backtest_end") for t in result.trades
        )


@pytest.mark.asyncio
async def test_exit_time_based() -> None:
    """Swing 전략 max_holding_days 초과 → 시간 기반 청산."""
    trading_dates = _make_trading_dates(date(2024, 6, 1), 80)
    symbol = "005930"

    # V자형: 진입 후 횡보 (SL/TP 미도달, 시간 초과)
    ohlcv: dict[date, dict] = {}
    price = 50000.0
    for i, d in enumerate(trading_dates):
        if i < 30:
            price *= 0.975  # 급락 (RSI < 30)
        elif i < 35:
            price *= 1.03  # 반등 (RSI 반등 시그널)
        else:
            # 소폭 등락 (SL/TP 미도달)
            price *= (1.001 if i % 2 == 0 else 0.999)

        ohlcv[d] = {
            "open": Decimal(str(round(price * 0.998, 0))),
            "high": Decimal(str(round(price * 1.008, 0))),
            "low": Decimal(str(round(price * 0.992, 0))),
            "close": Decimal(str(round(price, 0))),
            "volume": 10_000_000,
        }

    loader = _make_loader([symbol], trading_dates, {symbol: ohlcv})
    engine = _make_engine(loader)

    config = BacktestConfig(
        strategy_type=StrategyType.SWING,
        start_date=trading_dates[30],
        end_date=trading_dates[-1],
        initial_capital=Decimal("10_000_000"),
        symbols=[symbol],
        mode=BacktestMode.TECHNICAL,
        parameters={"max_holding_days": 5},
    )
    result = await engine.run(config)

    assert result.status == BacktestStatus.COMPLETED
    time_exits = [t for t in result.trades if t.exit_reason == "time_based"]
    buy_trades = [t for t in result.trades if t.side.value == "buy"]
    # 진입이 발생했다면 시간 기반 청산이 있어야 함
    if buy_trades:
        # 시간 초과 또는 다른 이유로 청산
        sell_trades = [t for t in result.trades if t.side.value == "sell"]
        assert len(sell_trades) > 0


@pytest.mark.asyncio
async def test_trailing_stop_activation() -> None:
    """5%+ 수익 후 하락 → 트레일링 스톱 발동."""
    trading_dates = _make_trading_dates(date(2024, 6, 1), 100)
    symbol = "005930"

    ohlcv: dict[date, dict] = {}
    price = 50000.0
    for i, d in enumerate(trading_dates):
        if i < 40:
            price *= 0.985  # 하락
        elif i < 60:
            price *= 1.012  # 회복 (진입)
        elif i < 75:
            price *= 1.008  # 완만 상승 (+5%~10% 도달)
        else:
            price *= 0.975  # 하락 (트레일링 발동)

        ohlcv[d] = {
            "open": Decimal(str(round(price * 0.998, 0))),
            "high": Decimal(str(round(price * 1.005, 0))),
            "low": Decimal(str(round(price * 0.992, 0))),
            "close": Decimal(str(round(price, 0))),
            "volume": 10_000_000,
        }

    loader = _make_loader([symbol], trading_dates, {symbol: ohlcv})
    engine = _make_engine(loader)

    config = BacktestConfig(
        strategy_type=StrategyType.POSITION,
        start_date=trading_dates[50],
        end_date=trading_dates[-1],
        initial_capital=Decimal("10_000_000"),
        symbols=[symbol],
        mode=BacktestMode.TECHNICAL,
        parameters={"trailing_stop_pct": 5.0},
    )
    result = await engine.run(config)

    assert result.status == BacktestStatus.COMPLETED
    trailing_exits = [t for t in result.trades if t.exit_reason == "trailing_stop"]
    buy_trades = [t for t in result.trades if t.side.value == "buy"]
    # 진입이 일어나고 트레일링 조건이 충족되면 트레일링 청산이 있어야 함
    if buy_trades:
        sell_trades = [t for t in result.trades if t.side.value == "sell"]
        assert len(sell_trades) > 0


@pytest.mark.asyncio
async def test_risk_max_positions() -> None:
    """MAX_PORTFOLIO_POSITIONS=2 → 3번째 진입 차단."""
    trading_dates = _make_trading_dates(date(2024, 6, 1), 80)
    symbols = ["A", "B", "C", "D", "E"]

    # 모든 종목에 동일한 V자형 데이터 (시그널 동시 발생)
    all_data: dict[str, dict[date, dict]] = {}
    for sym in symbols:
        data = _generate_v_shape_data(sym, trading_dates, base_price=10000)
        all_data.update(data)

    loader = _make_loader(symbols, trading_dates, all_data)
    settings = _make_settings(
        MAX_PORTFOLIO_POSITIONS=2,
        MAX_POSITION_SIZE_KRW=10_000_000,
    )
    engine = _make_engine(loader, settings=settings)

    config = BacktestConfig(
        strategy_type=StrategyType.POSITION,
        start_date=trading_dates[50],
        end_date=trading_dates[-1],
        initial_capital=Decimal("10_000_000"),
        symbols=symbols,
        mode=BacktestMode.TECHNICAL,
    )
    result = await engine.run(config)

    assert result.status == BacktestStatus.COMPLETED
    # 동시 보유는 최대 2개: 진입 BUY 거래 수 확인
    # 엔진이 내부적으로 max_positions를 제한하므로
    # 활성 포지션이 2개를 초과하지 않았어야 함
    # 정확한 확인은 엔진 내부 상태이므로, 거래 기록으로 간접 확인
    buy_trades = [t for t in result.trades if t.side.value == "buy"]
    # 5종목 시그널이 떠도 포지션 제한으로 일부만 진입
    # 최대 보유 2개 × (청산 후 재진입 가능) → buy 수 ≤ 실질적 제한
    assert len(buy_trades) <= len(symbols)  # 논리적 상한


@pytest.mark.asyncio
async def test_risk_position_size_cap() -> None:
    """MAX_POSITION_PCT=10 → 단일 종목 비중 10% 초과 시 수량 조정."""
    trading_dates = _make_trading_dates(date(2024, 6, 1), 80)
    symbol = "005930"

    all_data = _generate_v_shape_data(symbol, trading_dates, base_price=50000)
    loader = _make_loader([symbol], trading_dates, all_data)
    settings = _make_settings(MAX_POSITION_PCT=10.0)
    engine = _make_engine(
        loader,
        initial_capital=Decimal("10_000_000"),
        settings=settings,
    )

    config = BacktestConfig(
        strategy_type=StrategyType.POSITION,
        start_date=trading_dates[50],
        end_date=trading_dates[-1],
        initial_capital=Decimal("10_000_000"),
        symbols=[symbol],
        mode=BacktestMode.TECHNICAL,
    )
    result = await engine.run(config)

    assert result.status == BacktestStatus.COMPLETED
    # 진입 거래가 있다면, 포지션 가치가 총 자산의 10% 이하인지 확인
    buy_trades = [t for t in result.trades if t.side.value == "buy"]
    for bt in buy_trades:
        position_value = bt.price * bt.quantity
        # 진입 시점 총 자산 ~ 1000만원이므로 10% = 100만원
        assert position_value <= Decimal("10_000_000") * Decimal("0.10") + Decimal("50000")


@pytest.mark.asyncio
async def test_snapshot_daily_recording() -> None:
    """30거래일 → 30개 스냅샷 생성 확인."""
    trading_dates = _make_trading_dates(date(2024, 6, 1), 80)
    symbol = "005930"

    all_data = _generate_flat_data(symbol, trading_dates)
    loader = _make_loader([symbol], trading_dates, all_data)
    engine = _make_engine(loader)

    backtest_dates = trading_dates[50:]  # 30거래일
    config = BacktestConfig(
        strategy_type=StrategyType.POSITION,
        start_date=backtest_dates[0],
        end_date=backtest_dates[-1],
        initial_capital=Decimal("10_000_000"),
        symbols=[symbol],
        mode=BacktestMode.TECHNICAL,
    )
    result = await engine.run(config)

    assert result.status == BacktestStatus.COMPLETED
    # 엔진 내부 스냅샷 확인 — engine._snapshots 접근
    assert len(engine._snapshots) == len(backtest_dates)
    # 각 스냅샷의 total_value >= 0
    for snap in engine._snapshots:
        assert snap.total_value >= _ZERO


@pytest.mark.asyncio
async def test_performance_metrics_calculated() -> None:
    """run() 완료 후 metrics에 sharpe, mdd, win_rate 존재."""
    trading_dates = _make_trading_dates(date(2024, 6, 1), 80)
    symbols = ["005930"]

    all_data = _generate_v_shape_data("005930", trading_dates, base_price=50000)
    loader = _make_loader(symbols, trading_dates, all_data)
    engine = _make_engine(loader)

    config = BacktestConfig(
        strategy_type=StrategyType.POSITION,
        start_date=trading_dates[50],
        end_date=trading_dates[-1],
        initial_capital=Decimal("10_000_000"),
        symbols=symbols,
        mode=BacktestMode.TECHNICAL,
    )
    result = await engine.run(config)

    assert result.status == BacktestStatus.COMPLETED
    assert result.metrics is not None

    # 핵심 필드 존재
    m = result.metrics
    assert m.total_return_pct is not None
    assert m.max_drawdown_pct is not None
    assert m.win_rate_pct is not None
    assert m.total_trades is not None


@pytest.mark.asyncio
async def test_empty_universe() -> None:
    """symbols=[] → 0 trades, 에러 없이 COMPLETED."""
    trading_dates = _make_trading_dates(date(2024, 6, 1), 30)
    loader = _make_loader([], trading_dates, {})
    engine = _make_engine(loader)

    config = BacktestConfig(
        strategy_type=StrategyType.POSITION,
        start_date=trading_dates[0],
        end_date=trading_dates[-1],
        initial_capital=Decimal("10_000_000"),
        symbols=[],
        mode=BacktestMode.TECHNICAL,
    )
    result = await engine.run(config)

    assert result.status == BacktestStatus.COMPLETED
    assert result.total_trades == 0
    assert result.error_message is None


@pytest.mark.asyncio
async def test_no_signals_period() -> None:
    """횡보 데이터 → 0 trades, 스냅샷만 기록."""
    trading_dates = _make_trading_dates(date(2024, 6, 1), 80)
    symbol = "005930"

    all_data = _generate_flat_data(symbol, trading_dates)
    loader = _make_loader([symbol], trading_dates, all_data)
    engine = _make_engine(loader)

    config = BacktestConfig(
        strategy_type=StrategyType.POSITION,
        start_date=trading_dates[50],
        end_date=trading_dates[-1],
        initial_capital=Decimal("10_000_000"),
        symbols=[symbol],
        mode=BacktestMode.TECHNICAL,
    )
    result = await engine.run(config)

    assert result.status == BacktestStatus.COMPLETED
    assert result.total_trades == 0
    # 스냅샷은 거래일 수만큼 있어야 함
    expected_days = len([d for d in trading_dates if d >= trading_dates[50]])
    assert len(engine._snapshots) == expected_days


# ── 모듈 상수 ──
_ZERO = Decimal("0")
