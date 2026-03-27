"""SimulatedBroker unit tests.

Mock HistoricalDataLoader를 사용하여 DB 없이 테스트.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.backtest.simulator import SimulatedBroker
from src.core.enums import OrderSide, OrderStatus, OrderType, PositionStatus
from src.core.exceptions import InsufficientFundsError, OrderError
from src.core.models import OrderRequest


# ── Fixtures ──────────────────────────────────────────────────────────


def _make_loader(
    symbols: list[str] | None = None,
    trading_dates: list[date] | None = None,
    ohlcv_data: dict[str, dict[date, dict]] | None = None,
) -> MagicMock:
    """Mock HistoricalDataLoader with pre-populated data.

    Args:
        symbols: 종목 목록
        trading_dates: 거래일 목록 (정렬)
        ohlcv_data: {symbol: {date: {"open": ..., "high": ..., ...}}}
    """
    if trading_dates is None:
        trading_dates = [
            date(2025, 1, 2),
            date(2025, 1, 3),
            date(2025, 1, 6),
            date(2025, 1, 7),
            date(2025, 1, 8),
        ]
    if symbols is None:
        symbols = ["005930"]
    if ohlcv_data is None:
        # 기본 삼성전자 5일 데이터
        ohlcv_data = {
            "005930": {
                date(2025, 1, 2): {
                    "open": Decimal("70000"),
                    "high": Decimal("72000"),
                    "low": Decimal("69500"),
                    "close": Decimal("71000"),
                    "volume": 10_000_000,
                },
                date(2025, 1, 3): {
                    "open": Decimal("71500"),
                    "high": Decimal("73000"),
                    "low": Decimal("71000"),
                    "close": Decimal("72500"),
                    "volume": 12_000_000,
                },
                date(2025, 1, 6): {
                    "open": Decimal("72000"),
                    "high": Decimal("74000"),
                    "low": Decimal("71500"),
                    "close": Decimal("73500"),
                    "volume": 15_000_000,
                },
                date(2025, 1, 7): {
                    "open": Decimal("73000"),
                    "high": Decimal("73500"),
                    "low": Decimal("72000"),
                    "close": Decimal("72500"),
                    "volume": 8_000_000,
                },
                date(2025, 1, 8): {
                    "open": Decimal("72500"),
                    "high": Decimal("74500"),
                    "low": Decimal("72000"),
                    "close": Decimal("74000"),
                    "volume": 11_000_000,
                },
            }
        }

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
        symbol: str, start_date: date, end_date: date
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
                "open": [filtered[d]["open"] for d in dates],
                "high": [filtered[d]["high"] for d in dates],
                "low": [filtered[d]["low"] for d in dates],
                "close": [filtered[d]["close"] for d in dates],
                "volume": [filtered[d]["volume"] for d in dates],
            },
            index=pd.Index(dates, name="date"),
        )

    loader.get_ohlcv.side_effect = _get_ohlcv
    loader.get_close_price.side_effect = _get_close_price
    loader.get_open_price.side_effect = _get_open_price
    loader.get_ohlcv_range.side_effect = _get_ohlcv_range

    return loader


@pytest.fixture
def loader() -> MagicMock:
    return _make_loader()


@pytest.fixture
def broker(loader: MagicMock) -> SimulatedBroker:
    return SimulatedBroker(
        data_loader=loader,
        initial_capital=Decimal("10_000_000"),
    )


# ── Tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_initial_state(broker: SimulatedBroker) -> None:
    """초기 자본 1000만원, positions 빈 리스트, cash == initial_capital."""
    assert broker.get_total_value() == Decimal("10_000_000")
    assert broker.get_realized_pnl() == Decimal("0")
    assert broker.current_date is None
    positions = await broker.get_positions()
    assert positions == []


@pytest.mark.asyncio
async def test_set_current_date(broker: SimulatedBroker) -> None:
    """날짜 설정 후 current_date 확인."""
    broker.set_current_date(date(2025, 1, 2))
    assert broker.current_date == date(2025, 1, 2)

    broker.set_current_date(date(2025, 1, 6))
    assert broker.current_date == date(2025, 1, 6)


@pytest.mark.asyncio
async def test_get_price_from_ohlcv(broker: SimulatedBroker) -> None:
    """OHLCV → PriceInfo 필드 매핑 확인."""
    broker.set_current_date(date(2025, 1, 3))
    price = await broker.get_price("005930")

    # current_price == 당일 종가
    assert price.current_price == Decimal("72500")
    # previous_close == 전일(1/2) 종가
    assert price.previous_close == Decimal("71000")
    assert price.high == Decimal("73000")
    assert price.low == Decimal("71000")
    assert price.volume == 12_000_000
    assert price.change_price == Decimal("1500")
    assert price.timestamp == datetime.combine(date(2025, 1, 3), time(15, 30))


@pytest.mark.asyncio
async def test_place_buy_order(broker: SimulatedBroker) -> None:
    """매수 → cash 감소, position 생성, commission 적용."""
    broker.set_current_date(date(2025, 1, 3))
    order = OrderRequest(
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
    )
    result = await broker.place_order(order)

    assert result.status == OrderStatus.FILLED
    assert result.filled_quantity == 100
    assert result.commission > 0

    # 포지션 생성 확인
    positions = await broker.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "005930"
    assert positions[0].quantity == 100

    # cash 감소 확인 (fill_price * qty + commission)
    expected_cost = result.filled_price * 100 + result.commission
    assert broker._cash == Decimal("10_000_000") - expected_cost


@pytest.mark.asyncio
async def test_place_sell_order(broker: SimulatedBroker) -> None:
    """매도 → cash 증가, position 제거, realized_pnl 갱신."""
    # 먼저 매수
    broker.set_current_date(date(2025, 1, 3))
    buy_order = OrderRequest(
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
    )
    buy_result = await broker.place_order(buy_order)
    cash_after_buy = broker._cash

    # 다음 날 매도 (가격 상승)
    broker.set_current_date(date(2025, 1, 6))
    sell_order = OrderRequest(
        symbol="005930",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=100,
    )
    sell_result = await broker.place_order(sell_order)

    assert sell_result.status == OrderStatus.FILLED
    assert sell_result.commission > 0

    # 포지션 제거
    positions = await broker.get_positions()
    assert len(positions) == 0

    # cash 증가 (매도 대금 - 수수료)
    expected_cash = cash_after_buy + sell_result.filled_price * 100 - sell_result.commission
    assert broker._cash == expected_cash

    # 실현 손익 확인
    assert broker.get_realized_pnl() == (
        sell_result.filled_price - buy_result.filled_price
    ) * 100


@pytest.mark.asyncio
async def test_slippage_buy_unfavorable(broker: SimulatedBroker) -> None:
    """BUY 슬리피지 → 체결가 > 시가."""
    # 시가 = 71500 (1/3)
    fill_price = broker.calculate_slippage(
        "005930", OrderSide.BUY, Decimal("71500")
    )
    # 10bps = 0.1% → 71500 * 1.001 = 71571.5 → 72 (반올림)
    assert fill_price > Decimal("71500")
    expected = (Decimal("71500") * Decimal("1.001")).quantize(Decimal("1"))
    assert fill_price == expected


@pytest.mark.asyncio
async def test_slippage_sell_unfavorable(broker: SimulatedBroker) -> None:
    """SELL 슬리피지 → 체결가 < 시가."""
    fill_price = broker.calculate_slippage(
        "005930", OrderSide.SELL, Decimal("71500")
    )
    assert fill_price < Decimal("71500")
    # 71500 * 0.999 = 71428.5 → ROUND_HALF_UP → 71429
    assert fill_price == Decimal("71429")


@pytest.mark.asyncio
async def test_commission_buy_0015pct(broker: SimulatedBroker) -> None:
    """매수 100만원 → 수수료 150원 (0.015%)."""
    commission = broker.calculate_commission(
        OrderSide.BUY, Decimal("1_000_000")
    )
    # 1,000,000 * 0.00015 = 150
    assert commission == Decimal("150")


@pytest.mark.asyncio
async def test_commission_sell_0195pct(broker: SimulatedBroker) -> None:
    """매도 100만원 → 수수료 1,950원 (0.195%)."""
    commission = broker.calculate_commission(
        OrderSide.SELL, Decimal("1_000_000")
    )
    # 1,000,000 * 0.00195 = 1950
    assert commission == Decimal("1950")


@pytest.mark.asyncio
async def test_get_balance_with_positions(broker: SimulatedBroker) -> None:
    """현금 + 평가액 합산 확인."""
    broker.set_current_date(date(2025, 1, 3))
    order = OrderRequest(
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=10,
    )
    await broker.place_order(order)

    balance = await broker.get_balance()
    assert balance.cash == broker._cash
    assert balance.positions_count == 1
    assert balance.total_assets == broker.get_total_value()


@pytest.mark.asyncio
async def test_insufficient_cash(broker: SimulatedBroker) -> None:
    """잔고 부족 매수 → InsufficientFundsError."""
    broker.set_current_date(date(2025, 1, 3))
    order = OrderRequest(
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100_000,  # 71500원 * 10만주 = 약 71.5억
    )
    with pytest.raises(InsufficientFundsError):
        await broker.place_order(order)


@pytest.mark.asyncio
async def test_insufficient_quantity(broker: SimulatedBroker) -> None:
    """보유 수량 초과 매도 → OrderError."""
    broker.set_current_date(date(2025, 1, 3))
    # 10주 매수
    buy = OrderRequest(
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=10,
    )
    await broker.place_order(buy)

    # 20주 매도 시도
    sell = OrderRequest(
        symbol="005930",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=20,
    )
    with pytest.raises(OrderError, match="Insufficient quantity"):
        await broker.place_order(sell)


@pytest.mark.asyncio
async def test_get_daily_ohlcv_period(broker: SimulatedBroker) -> None:
    """period_days로 날짜 범위 내 OHLCV 반환, look-ahead bias 방지."""
    # 1/7 기준 3일 요청 → 1/3, 1/6, 1/7 (1/8은 미포함)
    broker.set_current_date(date(2025, 1, 7))
    bars = await broker.get_daily_ohlcv("005930", period_days=3)

    assert len(bars) == 3
    dates = [bar.date for bar in bars]
    assert dates == [date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7)]
    # 미래 데이터(1/8) 미포함 확인
    assert date(2025, 1, 8) not in dates


@pytest.mark.asyncio
async def test_get_price_no_date_set(broker: SimulatedBroker) -> None:
    """날짜 미설정 시 OrderError."""
    with pytest.raises(OrderError, match="Current date not set"):
        await broker.get_price("005930")


@pytest.mark.asyncio
async def test_get_price_unknown_symbol(broker: SimulatedBroker) -> None:
    """존재하지 않는 종목 → OrderError."""
    broker.set_current_date(date(2025, 1, 3))
    with pytest.raises(OrderError, match="No price data"):
        await broker.get_price("999999")


@pytest.mark.asyncio
async def test_position_valuation_update(broker: SimulatedBroker) -> None:
    """날짜 변경 시 포지션 평가액 업데이트."""
    # 1/3에 매수 (시가 71500 + 슬리피지)
    broker.set_current_date(date(2025, 1, 3))
    order = OrderRequest(
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
    )
    await broker.place_order(order)

    # 1/6으로 이동 (종가 73500)
    broker.set_current_date(date(2025, 1, 6))
    positions = await broker.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.current_price == Decimal("73500")
    assert pos.market_value == Decimal("73500") * 100
    assert pos.unrealized_pnl == (Decimal("73500") - pos.average_cost) * 100
