"""HistoricalDataLoader 단위 테스트."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from src.backtest.data_loader import HistoricalDataLoader


def _make_ohlcv_row(
    symbol: str,
    dt: date,
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: int,
    trading_value: Decimal | None = None,
) -> MagicMock:
    """DailyOHLCV ORM 객체를 흉내내는 Mock 생성."""
    row = MagicMock()
    row.symbol = symbol
    row.date = dt
    row.open = open_
    row.high = high
    row.low = low
    row.close = close
    row.volume = volume
    row.trading_value = trading_value
    return row


def _build_sample_rows() -> list[MagicMock]:
    """2종목 × 5일 = 10건 샘플 데이터."""
    rows: list[MagicMock] = []
    base_dates = [date(2025, 1, d) for d in range(2, 7)]  # 1/2 ~ 1/6

    for i, dt in enumerate(base_dates):
        rows.append(
            _make_ohlcv_row(
                "005930",
                dt,
                Decimal("70000") + i * 100,
                Decimal("71000") + i * 100,
                Decimal("69000") + i * 100,
                Decimal("70500") + i * 100,
                1_000_000 + i * 10_000,
            )
        )
        rows.append(
            _make_ohlcv_row(
                "000660",
                dt,
                Decimal("150000") + i * 200,
                Decimal("152000") + i * 200,
                Decimal("148000") + i * 200,
                Decimal("151000") + i * 200,
                500_000 + i * 5_000,
            )
        )

    # date 순 정렬 (DB ORDER BY date 시뮬레이션)
    rows.sort(key=lambda r: (r.date, r.symbol))
    return rows


def _mock_session_factory(rows: list[MagicMock]) -> AsyncMock:
    """async_sessionmaker를 mock하여 rows를 반환."""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = rows

    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_factory


@pytest.mark.asyncio
async def test_load_from_db():
    """DB에서 10건 로드 → loaded=True, total_records=10."""
    rows = _build_sample_rows()
    factory = _mock_session_factory(rows)
    loader = HistoricalDataLoader(factory)

    assert loader.loaded is False
    assert loader.total_records == 0

    count = await loader.load(
        symbols=["005930", "000660"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    assert count == 10
    assert loader.loaded is True
    assert loader.total_records == 10


@pytest.mark.asyncio
async def test_get_ohlcv_hit():
    """존재하는 (symbol, date) → OHLCV dict 반환, Decimal 타입."""
    rows = _build_sample_rows()
    factory = _mock_session_factory(rows)
    loader = HistoricalDataLoader(factory)
    await loader.load(
        symbols=["005930", "000660"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    ohlcv = loader.get_ohlcv("005930", date(2025, 1, 2))
    assert ohlcv is not None
    assert ohlcv["open"] == Decimal("70000")
    assert ohlcv["high"] == Decimal("71000")
    assert ohlcv["low"] == Decimal("69000")
    assert ohlcv["close"] == Decimal("70500")
    assert ohlcv["volume"] == 1_000_000
    # Decimal 타입 확인
    assert isinstance(ohlcv["open"], Decimal)
    assert isinstance(ohlcv["close"], Decimal)


@pytest.mark.asyncio
async def test_get_ohlcv_miss():
    """존재하지 않는 날짜 → None."""
    rows = _build_sample_rows()
    factory = _mock_session_factory(rows)
    loader = HistoricalDataLoader(factory)
    await loader.load(
        symbols=["005930"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    assert loader.get_ohlcv("005930", date(2025, 12, 31)) is None
    assert loader.get_ohlcv("NONEXIST", date(2025, 1, 2)) is None


@pytest.mark.asyncio
async def test_get_ohlcv_range():
    """날짜 범위 → DataFrame 행 수 확인."""
    rows = _build_sample_rows()
    factory = _mock_session_factory(rows)
    loader = HistoricalDataLoader(factory)
    await loader.load(
        symbols=["005930", "000660"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    df = loader.get_ohlcv_range("005930", date(2025, 1, 3), date(2025, 1, 5))
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 3  # 1/3, 1/4, 1/5

    # 존재하지 않는 종목 → 빈 DataFrame
    empty_df = loader.get_ohlcv_range("NONEXIST", date(2025, 1, 2), date(2025, 1, 6))
    assert len(empty_df) == 0


@pytest.mark.asyncio
async def test_get_close_price():
    """종가 Decimal 반환."""
    rows = _build_sample_rows()
    factory = _mock_session_factory(rows)
    loader = HistoricalDataLoader(factory)
    await loader.load(
        symbols=["005930"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    price = loader.get_close_price("005930", date(2025, 1, 2))
    assert price == Decimal("70500")
    assert isinstance(price, Decimal)

    assert loader.get_close_price("005930", date(2025, 12, 31)) is None


@pytest.mark.asyncio
async def test_get_open_price():
    """시가 Decimal 반환."""
    rows = _build_sample_rows()
    factory = _mock_session_factory(rows)
    loader = HistoricalDataLoader(factory)
    await loader.load(
        symbols=["000660"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    price = loader.get_open_price("000660", date(2025, 1, 2))
    assert price == Decimal("150000")
    assert isinstance(price, Decimal)

    assert loader.get_open_price("000660", date(2025, 12, 31)) is None


@pytest.mark.asyncio
async def test_get_trading_dates_sorted():
    """거래일 정렬 확인."""
    rows = _build_sample_rows()
    factory = _mock_session_factory(rows)
    loader = HistoricalDataLoader(factory)
    await loader.load(
        symbols=["005930", "000660"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    dates = loader.get_trading_dates()
    assert len(dates) == 5
    assert dates == sorted(dates)
    assert dates[0] == date(2025, 1, 2)
    assert dates[-1] == date(2025, 1, 6)


@pytest.mark.asyncio
async def test_get_symbols():
    """로드된 종목 목록."""
    rows = _build_sample_rows()
    factory = _mock_session_factory(rows)
    loader = HistoricalDataLoader(factory)
    await loader.load(
        symbols=["005930", "000660"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    symbols = loader.get_symbols()
    assert set(symbols) == {"005930", "000660"}


@pytest.mark.asyncio
async def test_get_trading_values():
    """거래대금 매핑 반환 — Decimal 값·None 결측 유지·범위 경계."""
    rows = [
        _make_ohlcv_row(
            "005930",
            date(2025, 1, 2 + i),
            Decimal("70000"),
            Decimal("71000"),
            Decimal("69000"),
            Decimal("70500"),
            1_000_000,
            trading_value=(None if i == 1 else Decimal("500000000000") + i),
        )
        for i in range(3)
    ]
    factory = _mock_session_factory(rows)
    loader = HistoricalDataLoader(factory)
    await loader.load(
        symbols=["005930"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 4),
    )

    tv = loader.get_trading_values("005930", date(2025, 1, 2), date(2025, 1, 4))
    assert tv == {
        date(2025, 1, 2): Decimal("500000000000"),
        date(2025, 1, 3): None,
        date(2025, 1, 4): Decimal("500000000002"),
    }
    assert isinstance(tv[date(2025, 1, 2)], Decimal)

    # 범위 경계 — 부분 범위는 해당 날짜만
    partial = loader.get_trading_values("005930", date(2025, 1, 3), date(2025, 1, 3))
    assert partial == {date(2025, 1, 3): None}


@pytest.mark.asyncio
async def test_get_trading_values_missing_symbol():
    """미적재 심볼 → 빈 dict."""
    rows = _build_sample_rows()
    factory = _mock_session_factory(rows)
    loader = HistoricalDataLoader(factory)
    await loader.load(
        symbols=["005930"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    assert loader.get_trading_values("NONEXIST", date(2025, 1, 2), date(2025, 1, 6)) == {}


@pytest.mark.asyncio
async def test_empty_data():
    """빈 결과 → trading_dates=[], loaded=True, total_records=0."""
    factory = _mock_session_factory([])
    loader = HistoricalDataLoader(factory)

    count = await loader.load(
        symbols=["005930"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    assert count == 0
    assert loader.loaded is True
    assert loader.total_records == 0
    assert loader.get_trading_dates() == []
    assert loader.get_symbols() == []
    assert loader.get_ohlcv("005930", date(2025, 1, 2)) is None
