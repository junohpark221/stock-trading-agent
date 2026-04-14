"""Simulated broker for backtesting.

Date-aware BrokerInterface implementation that uses HistoricalDataLoader
for OHLCV price data. Models slippage and commission costs.

Usage::

    loader = HistoricalDataLoader(session_factory)
    await loader.load(symbols=["005930"], start_date=..., end_date=...)

    async with SimulatedBroker(data_loader=loader, initial_capital=Decimal("10_000_000")) as broker:
        broker.set_current_date(date(2025, 1, 2))
        price = await broker.get_price("005930")
        result = await broker.place_order(order)
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

import structlog

from src.broker.base import BrokerInterface
from src.core.enums import OrderSide, OrderStatus, OrderType, PositionStatus
from src.core.exceptions import InsufficientFundsError, OrderError
from src.core.models import (
    OHLCV,
    AccountBalance,
    OrderResult,
    Position,
    PriceInfo,
    StockInfo,
)

if TYPE_CHECKING:
    from src.backtest.data_loader import HistoricalDataLoader
    from src.core.models import OrderRequest

logger = structlog.get_logger(__name__)

# KRX 장 마감 시각 (15:30)
_KRX_CLOSE = time(15, 30)
# KRX 장 시작 시각 (09:00)
_KRX_OPEN = time(9, 0)


class SimulatedBroker(BrokerInterface):
    """백테스트용 가상 브로커.

    날짜 인식(date-aware) BrokerInterface 구현.
    HistoricalDataLoader에서 OHLCV를 조회하여 가격 제공.
    슬리피지 + 수수료 모델링.
    """

    def __init__(
        self,
        *,
        data_loader: HistoricalDataLoader,
        initial_capital: Decimal,
        slippage_bps: int = 10,
        commission_buy_pct: Decimal = Decimal("0.015"),
        commission_sell_pct: Decimal = Decimal("0.195"),
    ) -> None:
        self._data_loader = data_loader
        self._initial_capital = initial_capital
        self._cash = initial_capital
        self._slippage_bps = slippage_bps
        # 퍼센트 → 비율 변환 (0.015% → 0.00015)
        self._commission_buy_rate = commission_buy_pct / Decimal("100")
        self._commission_sell_rate = commission_sell_pct / Decimal("100")
        self._current_date: date | None = None
        self._positions: dict[str, Position] = {}
        self._orders: list[OrderResult] = []
        self._order_counter = 0
        self._realized_pnl = Decimal("0")
        self._trading_dates: list[date] = data_loader.get_trading_dates()

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def connect(self) -> None:
        """No-op (SimulatedBroker는 연결 불필요)."""
        logger.info("simulated_broker_connected", capital=str(self._initial_capital))

    async def disconnect(self) -> None:
        """No-op."""
        logger.info("simulated_broker_disconnected")

    # ── 백테스트 전용 메서드 ──────────────────────────────────────────

    @property
    def current_date(self) -> date | None:
        """현재 시뮬레이션 날짜."""
        return self._current_date

    def set_current_date(self, target_date: date) -> None:
        """시뮬레이션 날짜 설정. 보유 포지션 평가액 업데이트."""
        self._current_date = target_date
        self._update_position_valuations(target_date)
        logger.debug("sim_date_set", date=str(target_date))

    def calculate_slippage(
        self, symbol: str, side: OrderSide, base_price: Decimal
    ) -> Decimal:
        """슬리피지 계산.

        BUY: +슬리피지 (불리한 방향, 더 비싸게 매수)
        SELL: -슬리피지 (불리한 방향, 더 싸게 매도)
        """
        slippage_rate = Decimal(self._slippage_bps) / Decimal("10000")
        if side == OrderSide.BUY:
            price = base_price * (Decimal("1") + slippage_rate)
        else:
            price = base_price * (Decimal("1") - slippage_rate)
        # 한국 주식 가격은 정수 원 단위
        return price.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    def calculate_commission(self, side: OrderSide, amount: Decimal) -> Decimal:
        """수수료 계산.

        BUY: 0.015% (증권사 수수료)
        SELL: 0.195% (증권사 수수료 0.015% + 거래세 0.18%)
        """
        if side == OrderSide.BUY:
            commission = amount * self._commission_buy_rate
        else:
            commission = amount * self._commission_sell_rate
        return commission.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    def get_total_value(self) -> Decimal:
        """총 자산 = 현금 + Sigma(보유 포지션 평가액)."""
        position_value = sum(
            (pos.market_value for pos in self._positions.values() if pos.quantity > 0),
            Decimal("0"),
        )
        return self._cash + position_value

    def get_realized_pnl(self) -> Decimal:
        """누적 실현 손익."""
        return self._realized_pnl

    # ── Market Data (BrokerInterface) ────────────────────────────────

    async def get_price(self, symbol: str) -> PriceInfo:
        """현재 날짜의 가격 정보 반환 (OHLCV 기반)."""
        self._ensure_date_set()
        assert self._current_date is not None  # for type checker

        ohlcv = self._data_loader.get_ohlcv(symbol, self._current_date)
        if ohlcv is None:
            raise OrderError(
                f"No price data for {symbol} on {self._current_date}"
            )

        close = Decimal(str(ohlcv["close"]))
        prev_close = self._get_previous_close(symbol)
        change_price = close - prev_close
        change_pct = (
            (change_price / prev_close * Decimal("100"))
            if prev_close != 0
            else Decimal("0")
        )

        return PriceInfo(
            symbol=symbol,
            current_price=close,
            previous_close=prev_close,
            change_price=change_price,
            change_percent=change_pct,
            high=Decimal(str(ohlcv["high"])),
            low=Decimal(str(ohlcv["low"])),
            volume=int(ohlcv["volume"]),
            timestamp=datetime.combine(self._current_date, _KRX_CLOSE),
        )

    async def get_daily_ohlcv(
        self, symbol: str, *, period_days: int = 100
    ) -> list[OHLCV]:
        """현재 날짜 기준 과거 period_days일 OHLCV 반환.

        look-ahead bias 방지: 현재 날짜 이후 데이터 미포함.
        """
        self._ensure_date_set()
        assert self._current_date is not None

        idx = self._find_date_index(self._current_date)
        start_idx = max(0, idx - period_days + 1)
        start_date = self._trading_dates[start_idx]

        df = self._data_loader.get_ohlcv_range(symbol, start_date, self._current_date)
        if df.empty:
            return []

        bars: list[OHLCV] = []
        for row_date, row in df.iterrows():
            bars.append(
                OHLCV(
                    symbol=symbol,
                    date=row_date,  # type: ignore[arg-type]
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=int(row["volume"]),
                )
            )
        return bars

    async def get_stock_master(self) -> list[StockInfo]:
        """미지원 — 빈 리스트 반환."""
        return []

    # ── Trading (BrokerInterface) ────────────────────────────────────

    async def place_order(self, order: OrderRequest) -> OrderResult:
        """가상 주문 체결.

        1. 시가(open) 기반 체결가 산정 (MARKET) 또는 지정가 (LIMIT)
        2. 슬리피지 적용 (MARKET만)
        3. 수수료 계산
        4. 잔고/포지션 업데이트
        5. OrderResult 반환
        """
        self._ensure_date_set()
        assert self._current_date is not None

        # 체결가 결정
        base_price = self._get_fill_price(order)

        # 슬리피지: MARKET 주문에만 적용
        if order.order_type == OrderType.MARKET:
            fill_price = self.calculate_slippage(order.symbol, order.side, base_price)
        else:
            fill_price = base_price

        # 수수료
        trade_amount = fill_price * order.quantity
        commission = self.calculate_commission(order.side, trade_amount)

        # 체결 실행
        if order.side == OrderSide.BUY:
            self._execute_buy(order, fill_price, commission)
        else:
            self._execute_sell(order, fill_price, commission)

        # OrderResult 생성
        order_id = self._next_order_id()
        result = OrderResult(
            order_id=order_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            price=fill_price,
            status=OrderStatus.FILLED,
            filled_quantity=order.quantity,
            filled_price=fill_price,
            commission=commission,
            timestamp=datetime.combine(self._current_date, _KRX_OPEN),
        )
        self._orders.append(result)

        logger.info(
            "sim_order_filled",
            order_id=order_id,
            symbol=order.symbol,
            side=order.side.value,
            quantity=order.quantity,
            fill_price=str(fill_price),
            commission=str(commission),
            cash=str(self._cash),
        )
        return result

    async def cancel_order(self, order_id: str) -> bool:
        """미지원 — 백테스트 주문은 즉시 체결. False 반환."""
        return False

    async def get_order_status(
        self, broker_order_id: str, *, order_date: object = None
    ) -> OrderResult:
        """백테스트 주문은 즉시 FILLED. 미조회 주문은 빈 SUBMITTED placeholder."""
        from datetime import datetime as _dt

        from src.core.enums import OrderSide, OrderType

        for record in self._orders:
            if record.order_id == broker_order_id:
                return record
        return OrderResult(
            order_id=broker_order_id,
            symbol="",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=0,
            price=Decimal(0),
            status=OrderStatus.SUBMITTED,
            filled_quantity=0,
            filled_price=None,
            commission=Decimal(0),
            timestamp=_dt.now(),
        )

    # ── Account (BrokerInterface) ────────────────────────────────────

    async def get_balance(self) -> AccountBalance:
        """현금 + 포지션 평가액 반환."""
        invested = Decimal("0")
        unrealized = Decimal("0")

        for pos in self._positions.values():
            if pos.quantity > 0:
                invested += pos.average_cost * pos.quantity
                unrealized += pos.unrealized_pnl

        total_assets = self._cash + invested + unrealized
        open_count = sum(1 for p in self._positions.values() if p.quantity > 0)

        ts = (
            datetime.combine(self._current_date, _KRX_CLOSE)
            if self._current_date
            else datetime.now()
        )

        return AccountBalance(
            total_assets=total_assets,
            cash=self._cash,
            invested=invested,
            unrealized_pnl=unrealized,
            realized_pnl=self._realized_pnl,
            positions_count=open_count,
            timestamp=ts,
        )

    async def get_positions(self) -> list[Position]:
        """현재 보유 포지션 목록 (quantity > 0)."""
        return [p for p in self._positions.values() if p.quantity > 0]

    # ── Internal helpers ─────────────────────────────────────────────

    def _ensure_date_set(self) -> None:
        """현재 날짜가 설정되었는지 검증."""
        if self._current_date is None:
            raise OrderError("Current date not set. Call set_current_date() first.")

    def _next_order_id(self) -> str:
        self._order_counter += 1
        return f"SIM-{self._order_counter:06d}"

    def _find_date_index(self, target_date: date) -> int:
        """_trading_dates에서 target_date의 인덱스 반환 (bisect O(log n))."""
        idx = bisect_left(self._trading_dates, target_date)
        if idx < len(self._trading_dates) and self._trading_dates[idx] == target_date:
            return idx
        raise OrderError(f"Date {target_date} is not a valid trading date")

    def _get_previous_close(self, symbol: str) -> Decimal:
        """전일 종가 조회. 첫 거래일이면 당일 시가 반환."""
        assert self._current_date is not None
        idx = self._find_date_index(self._current_date)
        if idx > 0:
            prev_date = self._trading_dates[idx - 1]
            prev_close = self._data_loader.get_close_price(symbol, prev_date)
            if prev_close is not None:
                return Decimal(str(prev_close))
        # 첫 거래일 → 당일 시가
        open_price = self._data_loader.get_open_price(symbol, self._current_date)
        return Decimal(str(open_price)) if open_price is not None else Decimal("0")

    def _get_fill_price(self, order: OrderRequest) -> Decimal:
        """주문 체결가 결정."""
        assert self._current_date is not None

        if order.order_type == OrderType.MARKET:
            # MARKET: 당일 시가로 체결
            open_price = self._data_loader.get_open_price(
                order.symbol, self._current_date
            )
            if open_price is None:
                raise OrderError(
                    f"No price data for {order.symbol} on {self._current_date}"
                )
            return Decimal(str(open_price))

        # LIMIT: 지정가로 체결
        if order.price is None:
            raise OrderError("LIMIT order requires a price")
        return order.price

    def _execute_buy(
        self, order: OrderRequest, fill_price: Decimal, commission: Decimal
    ) -> None:
        """매수 실행: 현금 차감, 포지션 생성/업데이트."""
        assert self._current_date is not None
        total_cost = fill_price * order.quantity + commission

        if total_cost > self._cash:
            raise InsufficientFundsError(
                f"Insufficient funds: need {total_cost}, have {self._cash}"
            )
        self._cash -= total_cost

        if order.symbol in self._positions and self._positions[order.symbol].quantity > 0:
            # 기존 포지션에 가중평균 매입단가 적용
            existing = self._positions[order.symbol]
            old_total = existing.average_cost * existing.quantity
            new_total = old_total + fill_price * order.quantity
            new_qty = existing.quantity + order.quantity
            new_avg = new_total / new_qty

            market_value = fill_price * new_qty
            unrealized = (fill_price - new_avg) * new_qty
            pnl_pct = (
                ((fill_price - new_avg) / new_avg * Decimal("100"))
                if new_avg != 0
                else Decimal("0")
            )

            self._positions[order.symbol] = existing.model_copy(
                update={
                    "quantity": new_qty,
                    "average_cost": new_avg,
                    "current_price": fill_price,
                    "market_value": market_value,
                    "unrealized_pnl": unrealized,
                    "unrealized_pnl_pct": pnl_pct,
                    "status": PositionStatus.OPEN,
                },
            )
        else:
            # 신규 포지션
            market_value = fill_price * order.quantity
            self._positions[order.symbol] = Position(
                symbol=order.symbol,
                quantity=order.quantity,
                average_cost=fill_price,
                current_price=fill_price,
                market_value=market_value,
                unrealized_pnl=Decimal("0"),
                unrealized_pnl_pct=Decimal("0"),
                status=PositionStatus.OPEN,
                entry_date=datetime.combine(self._current_date, _KRX_OPEN),
            )

    def _execute_sell(
        self, order: OrderRequest, fill_price: Decimal, commission: Decimal
    ) -> None:
        """매도 실행: 현금 증가, 실현손익 계산, 포지션 축소/제거."""
        if order.symbol not in self._positions or self._positions[order.symbol].quantity <= 0:
            raise OrderError(f"No position to sell for symbol: {order.symbol}")

        existing = self._positions[order.symbol]
        if order.quantity > existing.quantity:
            raise OrderError(
                f"Insufficient quantity: held {existing.quantity}, requested {order.quantity}"
            )

        # 실현 손익 (수수료 별도 — 현금에서 차감)
        pnl = (fill_price - existing.average_cost) * order.quantity
        self._realized_pnl += pnl

        # 현금 = 매도 대금 - 수수료
        self._cash += fill_price * order.quantity - commission

        # 포지션 업데이트
        new_qty = existing.quantity - order.quantity
        if new_qty == 0:
            status = PositionStatus.CLOSED
            market_value = Decimal("0")
            unrealized = Decimal("0")
            pnl_pct = Decimal("0")
        else:
            status = PositionStatus.PARTIALLY_CLOSED
            market_value = fill_price * new_qty
            unrealized = (fill_price - existing.average_cost) * new_qty
            pnl_pct = (
                ((fill_price - existing.average_cost) / existing.average_cost * Decimal("100"))
                if existing.average_cost != 0
                else Decimal("0")
            )

        self._positions[order.symbol] = existing.model_copy(
            update={
                "quantity": new_qty,
                "current_price": fill_price,
                "market_value": market_value,
                "unrealized_pnl": unrealized,
                "unrealized_pnl_pct": pnl_pct,
                "status": status,
            },
        )

    def _update_position_valuations(self, target_date: date) -> None:
        """모든 보유 포지션의 평가액을 target_date 종가로 업데이트."""
        for symbol, pos in self._positions.items():
            if pos.quantity <= 0:
                continue
            close_price = self._data_loader.get_close_price(symbol, target_date)
            if close_price is None:
                # 해당 날짜에 거래 데이터 없으면 마지막 평가액 유지
                continue
            close = Decimal(str(close_price))
            market_value = close * pos.quantity
            unrealized = (close - pos.average_cost) * pos.quantity
            pnl_pct = (
                ((close - pos.average_cost) / pos.average_cost * Decimal("100"))
                if pos.average_cost != 0
                else Decimal("0")
            )
            self._positions[symbol] = pos.model_copy(
                update={
                    "current_price": close,
                    "market_value": market_value,
                    "unrealized_pnl": unrealized,
                    "unrealized_pnl_pct": pnl_pct,
                },
            )
