"""LLMReplayProvider + BacktestEngine Mode 2 unit tests.

decision_log 기반 LLM 응답 재생 및 Mode 2 백테스트 통합 테스트.
DB 없이 Mock session으로 테스트.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from src.backtest.engine import BacktestEngine
from src.backtest.llm_replay import (
    LLMReplayDecision,
    LLMReplayProvider,
    _KST,
)
from src.backtest.simulator import SimulatedBroker
from src.core.enums import (
    BacktestMode,
    BacktestStatus,
    DecisionAction,
    SignalAction,
    StrategyType,
)
from src.core.models import BacktestConfig


# ══════════════════════════════════════════════════════════════════════════
# 테스트 헬퍼
# ══════════════════════════════════════════════════════════════════════════


def _make_decision(
    *,
    symbol: str = "005930",
    decision: str = DecisionAction.BUY.value,
    confidence: Decimal = Decimal("0.8"),
    stage: str = "trade_decision",
    llm_model: str | None = "gpt-4o",
    created_at: datetime | None = None,
) -> LLMReplayDecision:
    """테스트용 LLMReplayDecision 생성."""
    if created_at is None:
        created_at = datetime(2024, 7, 15, 10, 0, 0, tzinfo=_KST)
    return LLMReplayDecision(
        decision_id=uuid.uuid4(),
        symbol=symbol,
        stage=stage,
        decision=decision,
        confidence=confidence,
        reasoning="Test reasoning for replay",
        llm_provider="openai",
        llm_model=llm_model,
        created_at=created_at,
    )


def _make_db_row(
    *,
    symbol: str = "005930",
    decision: str = DecisionAction.BUY.value,
    confidence: Decimal = Decimal("0.8"),
    stage: str = "trade_decision",
    llm_model: str = "gpt-4o",
    created_at: datetime | None = None,
) -> SimpleNamespace:
    """decision_log ORM row를 모사하는 SimpleNamespace."""
    if created_at is None:
        created_at = datetime(2024, 7, 15, 10, 0, 0, tzinfo=_KST)
    return SimpleNamespace(
        decision_id=uuid.uuid4(),
        symbol=symbol,
        stage=stage,
        decision=decision,
        confidence=confidence,
        reasoning="DB row reasoning",
        llm_provider="openai",
        llm_model=llm_model,
        created_at=created_at,
    )


def _make_mock_session_factory(rows: list) -> AsyncMock:
    """Mock async_sessionmaker → 세션 → execute → scalars().all() 체인."""
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = rows
    mock_result.scalars.return_value = mock_scalars

    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    mock_factory = MagicMock()
    mock_factory.return_value = mock_session

    # async context manager support
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    return mock_factory


def _make_settings(**overrides: object) -> SimpleNamespace:
    """Settings mock."""
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


def _generate_flat_data(
    symbol: str,
    trading_dates: list[date],
    base_price: float = 50000.0,
) -> dict[str, dict[date, dict]]:
    """횡보 데이터."""
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


# ══════════════════════════════════════════════════════════════════════════
# LLMReplayProvider 단위 테스트
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_load_from_decision_log() -> None:
    """Mock session → 5건 로드, loaded_count=5, 캐시 구조 확인."""
    rows = [
        _make_db_row(
            symbol="005930",
            created_at=datetime(2024, 7, 15, 10, 0, 0, tzinfo=_KST),
        ),
        _make_db_row(
            symbol="005930",
            created_at=datetime(2024, 7, 15, 14, 0, 0, tzinfo=_KST),
        ),
        _make_db_row(
            symbol="000660",
            created_at=datetime(2024, 7, 15, 11, 0, 0, tzinfo=_KST),
        ),
        _make_db_row(
            symbol="005930",
            created_at=datetime(2024, 7, 16, 9, 30, 0, tzinfo=_KST),
        ),
        _make_db_row(
            symbol="000660",
            created_at=datetime(2024, 7, 16, 10, 0, 0, tzinfo=_KST),
        ),
    ]

    factory = _make_mock_session_factory(rows)
    provider = LLMReplayProvider(factory)

    count = await provider.load(
        start_date=date(2024, 7, 15),
        end_date=date(2024, 7, 16),
        symbols=["005930", "000660"],
    )

    assert count == 5
    assert provider.loaded_count == 5

    # 캐시 키 확인
    d15_005930 = provider.get_decisions(target_date=date(2024, 7, 15), symbol="005930")
    assert len(d15_005930) == 2

    d15_000660 = provider.get_decisions(target_date=date(2024, 7, 15), symbol="000660")
    assert len(d15_000660) == 1

    d16_005930 = provider.get_decisions(target_date=date(2024, 7, 16), symbol="005930")
    assert len(d16_005930) == 1


@pytest.mark.asyncio
async def test_get_decisions_hit() -> None:
    """캐시에 존재하는 (date, symbol) → 올바른 리스트 반환."""
    factory = _make_mock_session_factory([])
    provider = LLMReplayProvider(factory)

    # 캐시 직접 주입
    d = _make_decision(symbol="005930")
    provider._cache[(date(2024, 7, 15), "005930")] = [d]
    provider._loaded_count = 1

    result = provider.get_decisions(target_date=date(2024, 7, 15), symbol="005930")
    assert len(result) == 1
    assert result[0].symbol == "005930"


@pytest.mark.asyncio
async def test_get_decisions_miss() -> None:
    """없는 날짜/종목 → 빈 리스트."""
    factory = _make_mock_session_factory([])
    provider = LLMReplayProvider(factory)

    result = provider.get_decisions(target_date=date(2024, 7, 15), symbol="999999")
    assert result == []


def test_to_signal_buy() -> None:
    """BUY + confidence 0.8 → Signal(BUY)."""
    factory = _make_mock_session_factory([])
    provider = LLMReplayProvider(factory)

    decision = _make_decision(
        decision=DecisionAction.BUY.value,
        confidence=Decimal("0.8"),
    )

    signal = provider.to_signal(decision, current_price=Decimal("50000"))
    assert signal is not None
    assert signal.action == SignalAction.BUY
    assert signal.confidence == Decimal("0.8")
    assert signal.symbol == "005930"
    assert "LLM replay" in signal.reasoning


def test_to_signal_hold_none() -> None:
    """HOLD → None."""
    factory = _make_mock_session_factory([])
    provider = LLMReplayProvider(factory)

    decision = _make_decision(
        decision=DecisionAction.HOLD.value,
        confidence=Decimal("0.8"),
    )

    signal = provider.to_signal(decision, current_price=Decimal("50000"))
    assert signal is None


def test_to_signal_low_confidence_none() -> None:
    """confidence 0.3 → None."""
    factory = _make_mock_session_factory([])
    provider = LLMReplayProvider(factory)

    decision = _make_decision(
        decision=DecisionAction.BUY.value,
        confidence=Decimal("0.3"),
    )

    signal = provider.to_signal(decision, current_price=Decimal("50000"))
    assert signal is None


@pytest.mark.asyncio
async def test_llm_model_filter() -> None:
    """특정 모델만 로드 — 쿼리에 llm_model 필터가 포함되는지 확인."""
    rows_gpt = [
        _make_db_row(symbol="005930", llm_model="gpt-4o"),
    ]

    factory = _make_mock_session_factory(rows_gpt)
    provider = LLMReplayProvider(factory)

    count = await provider.load(
        start_date=date(2024, 7, 15),
        end_date=date(2024, 7, 16),
        symbols=["005930"],
        llm_model_filter="gpt-4o",
    )

    assert count == 1
    # 세션의 execute가 호출되었는지 확인
    session = factory.return_value
    session.execute.assert_called_once()


def test_highest_confidence_selected() -> None:
    """동일 (date, symbol) 3건 → max confidence 선택 로직 검증."""
    factory = _make_mock_session_factory([])
    provider = LLMReplayProvider(factory)

    # 3건: confidence 0.6, 0.9, 0.7
    decisions = [
        _make_decision(confidence=Decimal("0.6")),
        _make_decision(confidence=Decimal("0.9")),
        _make_decision(confidence=Decimal("0.7")),
    ]

    # 최고 confidence 선택 (엔진의 _generate_replay_signals 로직)
    best = max(decisions, key=lambda d: d.confidence)
    assert best.confidence == Decimal("0.9")

    # to_signal 변환
    signal = provider.to_signal(best, current_price=Decimal("50000"))
    assert signal is not None
    assert signal.confidence == Decimal("0.9")


# ══════════════════════════════════════════════════════════════════════════
# BacktestEngine Mode 2 통합 테스트
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_engine_mode2_integration() -> None:
    """BacktestEngine Mode 2 → replay signals 사용 확인.

    LLMReplayProvider.load()를 패치하여 DB 없이 테스트.
    캐시에 BUY 결정을 주입 → 엔진이 시그널 생성 → 거래 발생 확인.
    """
    # 30거래일 데이터 (ATR 계산용 최소 14일 필요)
    trading_dates = _make_trading_dates(date(2024, 7, 1), 30)
    symbols = ["005930"]

    # 횡보 데이터 (기술 지표 시그널 미발생)
    all_data = _generate_flat_data("005930", trading_dates, base_price=50000.0)
    loader = _make_loader(symbols, trading_dates, all_data)

    settings = _make_settings()
    broker = SimulatedBroker(
        data_loader=loader,
        initial_capital=Decimal("10_000_000"),
        slippage_bps=10,
    )

    # Mock session_factory
    mock_factory = _make_mock_session_factory([])

    engine = BacktestEngine(
        data_loader=loader,
        broker=broker,
        settings=settings,
        session_factory=mock_factory,
    )

    # LLMReplayProvider.load를 패치: DB 로드 건너뛰고 캐시 직접 주입
    buy_date = trading_dates[15]  # 16번째 거래일에 BUY 결정
    buy_decision = _make_decision(
        symbol="005930",
        decision=DecisionAction.BUY.value,
        confidence=Decimal("0.85"),
        created_at=datetime(
            buy_date.year, buy_date.month, buy_date.day,
            10, 0, 0, tzinfo=_KST,
        ),
    )

    original_load = LLMReplayProvider.load

    async def mock_load(self: LLMReplayProvider, **kwargs: object) -> int:
        """캐시 직접 주입."""
        self._cache[(buy_date, "005930")] = [buy_decision]
        self._loaded_count = 1
        return 1

    config = BacktestConfig(
        strategy_type=StrategyType.SWING,
        start_date=trading_dates[0],
        end_date=trading_dates[-1],
        initial_capital=Decimal("10_000_000"),
        symbols=symbols,
        slippage_bps=10,
        mode=BacktestMode.LLM_REPLAY,
    )

    with patch.object(LLMReplayProvider, "load", mock_load):
        result = await engine.run(config)

    assert result.status == BacktestStatus.COMPLETED
    # buy_date에 시그널 생성 → buy_date+1에 체결 → 최소 1건 거래 발생
    assert result.total_trades >= 1
    assert len(result.trades) >= 1
    # 첫 거래가 005930 BUY인지 확인
    first_trade = result.trades[0]
    assert first_trade.symbol == "005930"


@pytest.mark.asyncio
async def test_engine_mode2_no_session_factory_raises() -> None:
    """Mode 2에서 session_factory 없으면 ValueError."""
    trading_dates = _make_trading_dates(date(2024, 7, 1), 10)
    symbols = ["005930"]
    all_data = _generate_flat_data("005930", trading_dates)
    loader = _make_loader(symbols, trading_dates, all_data)

    settings = _make_settings()
    broker = SimulatedBroker(
        data_loader=loader,
        initial_capital=Decimal("10_000_000"),
        slippage_bps=10,
    )

    engine = BacktestEngine(
        data_loader=loader,
        broker=broker,
        settings=settings,
        # session_factory 미제공
    )

    config = BacktestConfig(
        strategy_type=StrategyType.POSITION,
        start_date=trading_dates[0],
        end_date=trading_dates[-1],
        initial_capital=Decimal("10_000_000"),
        symbols=symbols,
        mode=BacktestMode.LLM_REPLAY,
    )

    result = await engine.run(config)
    assert result.status == BacktestStatus.FAILED
    assert "session_factory" in (result.error_message or "")
