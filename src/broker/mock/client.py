"""In-memory broker implementing BrokerInterface for testing.

All state is held in plain dicts — no network calls, no database, no Redis.
Orders execute instantly at the current mock price (MARKET) or limit price.

Usage::

    async with InMemoryBroker() as broker:
        price = await broker.get_price("005930")
        order = OrderRequest(symbol="005930", side=OrderSide.BUY,
                             order_type=OrderType.MARKET, quantity=10)
        result = await broker.place_order(order)
        balance = await broker.get_balance()
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from src.broker.base import BrokerInterface
from src.core.enums import (
    MarketType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
)
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
    from src.core.models import OrderRequest

logger = structlog.get_logger(__name__)


# ── Default test fixtures ──────────────────────────────────────────────


def _default_stocks() -> dict[str, StockInfo]:
    """Return 3 well-known KOSPI stocks for testing."""
    return {
        "005930": StockInfo(
            symbol="005930",
            name="삼성전자",
            market_type=MarketType.KOSPI,
        ),
        "000660": StockInfo(
            symbol="000660",
            name="SK하이닉스",
            market_type=MarketType.KOSPI,
        ),
        "035420": StockInfo(
            symbol="035420",
            name="NAVER",
            market_type=MarketType.KOSPI,
        ),
    }


def _default_prices() -> dict[str, PriceInfo]:
    """Return current-price snapshots for the 3 default stocks."""
    now = datetime.now()
    return {
        "005930": PriceInfo(
            symbol="005930",
            current_price=Decimal("72000"),
            previous_close=Decimal("71500"),
            change_price=Decimal("500"),
            change_percent=Decimal("0.70"),
            high=Decimal("72500"),
            low=Decimal("71000"),
            volume=15_000_000,
            timestamp=now,
        ),
        "000660": PriceInfo(
            symbol="000660",
            current_price=Decimal("185000"),
            previous_close=Decimal("183000"),
            change_price=Decimal("2000"),
            change_percent=Decimal("1.09"),
            high=Decimal("186000"),
            low=Decimal("182000"),
            volume=3_500_000,
            timestamp=now,
        ),
        "035420": PriceInfo(
            symbol="035420",
            current_price=Decimal("210000"),
            previous_close=Decimal("208000"),
            change_price=Decimal("2000"),
            change_percent=Decimal("0.96"),
            high=Decimal("212000"),
            low=Decimal("207000"),
            volume=800_000,
            timestamp=now,
        ),
    }


# ── InMemoryBroker ─────────────────────────────────────────────────────


class InMemoryBroker(BrokerInterface):
    """Test broker that executes orders instantly against in-memory state.

    Args:
        initial_cash: Starting cash balance (default 100,000,000 KRW).
        stocks: Stock master dict (symbol → StockInfo). ``None`` → 3 defaults.
        prices: Price snapshot dict (symbol → PriceInfo). ``None`` → 3 defaults.
        ohlcv_data: Pre-loaded OHLCV dict (symbol → list[OHLCV]). ``None`` → empty.
        positions: Pre-loaded positions dict (symbol → Position). ``None`` → empty.
    """

    def __init__(
        self,
        *,
        initial_cash: Decimal = Decimal("100_000_000"),
        stocks: dict[str, StockInfo] | None = None,
        prices: dict[str, PriceInfo] | None = None,
        ohlcv_data: dict[str, list[OHLCV]] | None = None,
        positions: dict[str, Position] | None = None,
    ) -> None:
        self._stocks = _default_stocks() if stocks is None else stocks
        self._prices = _default_prices() if prices is None else prices
        self._ohlcv_data: dict[str, list[OHLCV]] = ohlcv_data if ohlcv_data is not None else {}
        self._positions: dict[str, Position] = positions if positions is not None else {}
        self._orders: dict[str, OrderResult] = {}
        self._cash = initial_cash
        self._order_counter = 0
        self._realized_pnl = Decimal(0)
        self._connected = False

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._connected = True
        logger.info("mock_broker_connected", cash=str(self._cash))

    async def disconnect(self) -> None:
        self._connected = False
        logger.info("mock_broker_disconnected")

    # ── Market Data ───────────────────────────────────────────────────

    async def get_price(self, symbol: str) -> PriceInfo:
        """Return the mock price snapshot, raising ``OrderError`` if unknown."""
        if symbol not in self._prices:
            raise OrderError(f"No price data for symbol: {symbol}")
        return self._prices[symbol].model_copy(
            update={"timestamp": datetime.now()},
        )

    async def get_daily_ohlcv(self, symbol: str, *, period_days: int = 100) -> list[OHLCV]:
        """Return stored OHLCV bars, or generate flat synthetic candles."""
        if symbol in self._ohlcv_data and self._ohlcv_data[symbol]:
            return self._ohlcv_data[symbol][-period_days:]

        # Generate flat candles from current price
        base_price = (
            self._prices[symbol].current_price if symbol in self._prices else Decimal("50000")
        )
        return self._generate_synthetic_ohlcv(symbol, base_price, period_days)

    async def get_stock_master(self) -> list[StockInfo]:
        """Return all registered mock stocks."""
        return list(self._stocks.values())

    # ── Trading ───────────────────────────────────────────────────────

    async def place_order(self, order: OrderRequest) -> OrderResult:
        """Execute an order instantly against in-memory state.

        - MARKET orders fill at ``_prices[symbol].current_price``.
        - LIMIT orders fill at ``order.price``.
        - BUY: deducts cash, creates or updates position (weighted avg cost).
        - SELL: validates holdings, adds cash, calculates realized PnL.
        """
        fill_price = self._get_fill_price(order)
        order_id = self._next_order_id()

        if order.side == OrderSide.BUY:
            total_cost = fill_price * order.quantity
            self._execute_buy(order, fill_price, total_cost)
        else:
            self._execute_sell(order, fill_price)

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
            timestamp=datetime.now(),
        )
        self._orders[order_id] = result

        logger.info(
            "mock_order_filled",
            order_id=order_id,
            symbol=order.symbol,
            side=order.side.value,
            quantity=order.quantity,
            price=str(fill_price),
            cash=str(self._cash),
        )
        return result

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending/submitted order. Returns ``True`` on success."""
        if order_id not in self._orders:
            return False
        existing = self._orders[order_id]
        if existing.status not in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
            return False
        self._orders[order_id] = existing.model_copy(
            update={"status": OrderStatus.CANCELLED},
        )
        logger.info("mock_order_cancelled", order_id=order_id)
        return True

    # ── Account ───────────────────────────────────────────────────────

    async def get_balance(self) -> AccountBalance:
        """Calculate account balance from in-memory positions and cash."""
        invested = Decimal(0)
        unrealized = Decimal(0)

        for pos in self._positions.values():
            if pos.quantity > 0:
                invested += pos.average_cost * pos.quantity
                unrealized += pos.unrealized_pnl

        total_assets = self._cash + invested + unrealized
        open_count = sum(1 for p in self._positions.values() if p.quantity > 0)

        return AccountBalance(
            total_assets=total_assets,
            cash=self._cash,
            invested=invested,
            unrealized_pnl=unrealized,
            realized_pnl=self._realized_pnl,
            positions_count=open_count,
            timestamp=datetime.now(),
        )

    async def get_positions(self) -> list[Position]:
        """Return all open positions (quantity > 0)."""
        return [p for p in self._positions.values() if p.quantity > 0]

    # ── Internal helpers ──────────────────────────────────────────────

    def _next_order_id(self) -> str:
        self._order_counter += 1
        return f"MOCK-{self._order_counter:06d}"

    def _get_fill_price(self, order: OrderRequest) -> Decimal:
        """Determine fill price based on order type."""
        if order.order_type == OrderType.MARKET:
            if order.symbol not in self._prices:
                raise OrderError(f"No price data for symbol: {order.symbol}")
            return self._prices[order.symbol].current_price

        # LIMIT order
        if order.price is None:
            raise OrderError("LIMIT order requires a price")
        return order.price

    def _execute_buy(self, order: OrderRequest, fill_price: Decimal, total_cost: Decimal) -> None:
        """Deduct cash, create or update position with weighted average cost."""
        if total_cost > self._cash:
            raise InsufficientFundsError(
                f"Insufficient funds: need {total_cost}, have {self._cash}"
            )
        self._cash -= total_cost

        if order.symbol in self._positions and self._positions[order.symbol].quantity > 0:
            # Weighted average cost
            existing = self._positions[order.symbol]
            old_total = existing.average_cost * existing.quantity
            new_total = old_total + total_cost
            new_qty = existing.quantity + order.quantity
            new_avg = new_total / new_qty

            current = fill_price
            market_value = current * new_qty
            unrealized = market_value - (new_avg * new_qty)
            pnl_pct = ((current - new_avg) / new_avg * 100) if new_avg != 0 else Decimal(0)

            self._positions[order.symbol] = existing.model_copy(
                update={
                    "quantity": new_qty,
                    "average_cost": new_avg,
                    "current_price": current,
                    "market_value": market_value,
                    "unrealized_pnl": unrealized,
                    "unrealized_pnl_pct": pnl_pct,
                    "status": PositionStatus.OPEN,
                },
            )
        else:
            # New position
            market_value = fill_price * order.quantity
            self._positions[order.symbol] = Position(
                symbol=order.symbol,
                quantity=order.quantity,
                average_cost=fill_price,
                current_price=fill_price,
                market_value=market_value,
                unrealized_pnl=Decimal(0),
                unrealized_pnl_pct=Decimal(0),
                status=PositionStatus.OPEN,
                entry_date=datetime.now(),
            )

    def _execute_sell(self, order: OrderRequest, fill_price: Decimal) -> None:
        """Validate holdings, add cash, calculate realized PnL."""
        if order.symbol not in self._positions or self._positions[order.symbol].quantity <= 0:
            raise OrderError(f"No position to sell for symbol: {order.symbol}")

        existing = self._positions[order.symbol]
        if order.quantity > existing.quantity:
            raise OrderError(
                f"Insufficient quantity: held {existing.quantity}, requested {order.quantity}"
            )

        # Realized PnL
        pnl = (fill_price - existing.average_cost) * order.quantity
        self._realized_pnl += pnl
        self._cash += fill_price * order.quantity

        new_qty = existing.quantity - order.quantity
        if new_qty == 0:
            status = PositionStatus.CLOSED
            market_value = Decimal(0)
            unrealized = Decimal(0)
            pnl_pct = Decimal(0)
        else:
            status = PositionStatus.PARTIALLY_CLOSED
            market_value = fill_price * new_qty
            unrealized = (fill_price - existing.average_cost) * new_qty
            pnl_pct = (
                ((fill_price - existing.average_cost) / existing.average_cost * 100)
                if existing.average_cost != 0
                else Decimal(0)
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

    @staticmethod
    def _generate_synthetic_ohlcv(
        symbol: str, base_price: Decimal, period_days: int
    ) -> list[OHLCV]:
        """Generate flat synthetic candles for testing."""
        today = date.today()
        bars: list[OHLCV] = []
        for i in range(period_days, 0, -1):
            bars.append(
                OHLCV(
                    symbol=symbol,
                    date=today - timedelta(days=i),
                    open=base_price,
                    high=base_price,
                    low=base_price,
                    close=base_price,
                    volume=1_000_000,
                )
            )
        return bars
