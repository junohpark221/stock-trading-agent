"""HistoricalDataLoader 수급 확장(PRJ-03 단계 7) 단위 테스트.

Mock 주의 2건:
- session.execute는 COUNT→배치 SELECT 순서라 side_effect 리스트로 mock.
- 수급 행 mock에 MagicMock 금지 — auto-attribute가 90개 필드에 MagicMock을
  반환해 model_validate가 ValidationError로 죽는다. 실제 레코드 인스턴스를
  ORM row 대용으로 사용(from_attributes가 인스턴스 속성을 그대로 읽음).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.analysis.investor_flow import compute_flow_summary, flow_intensity
from src.backtest.data_loader import HistoricalDataLoader
from src.core.models import InvestorFlowRecord, MarketInvestorFlowRecord


def _make_flow_row(symbol: str, dt: date, **fields) -> InvestorFlowRecord:
    """InvestorFlowDaily ORM row 대용 — 실제 레코드 인스턴스."""
    return InvestorFlowRecord(symbol=symbol, date=dt, **fields)


def _make_market_row(market: str, dt: date, **fields) -> MarketInvestorFlowRecord:
    """MarketInvestorFlowDaily ORM row 대용 — 실제 레코드 인스턴스."""
    return MarketInvestorFlowRecord(market=market, date=dt, **fields)


def _count_result(n: int) -> MagicMock:
    """COUNT 쿼리 결과 mock."""
    result = MagicMock()
    result.scalar_one.return_value = n
    return result


def _select_result(rows: list) -> MagicMock:
    """SELECT 쿼리 결과 mock (scalars().all() 패턴)."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


def _mock_session_factory(results: list) -> tuple[MagicMock, AsyncMock]:
    """execute 호출 순서대로 results를 반환하는 세션 팩토리 mock."""
    mock_session = AsyncMock()
    mock_session.execute.side_effect = results

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_factory, mock_session


def _build_flow_rows(symbol: str, days: int = 5) -> list[InvestorFlowRecord]:
    """symbol × days건 — frgn 순매수 양수, orgn 음수."""
    return [
        _make_flow_row(
            symbol,
            date(2025, 1, 2 + i),
            frgn_net_qty=100 + i,
            frgn_net_amt=Decimal("1000000") + i,
            orgn_net_qty=-(50 + i),
            orgn_net_amt=-(Decimal("500000") + i),
        )
        for i in range(days)
    ]


@pytest.mark.asyncio
async def test_load_investor_flow_basic():
    """2심볼×5일 적재 → 카운트·레코드 타입·필드 값."""
    rows = _build_flow_rows("005930") + _build_flow_rows("000660")
    factory, session = _mock_session_factory([_count_result(10), _select_result(rows)])
    loader = HistoricalDataLoader(factory)

    count = await loader.load_investor_flow(
        symbols=["005930", "000660"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    assert count == 10
    got = loader.get_investor_flow_range("005930", date(2025, 1, 2), date(2025, 1, 6))
    assert len(got) == 5
    assert all(isinstance(r, InvestorFlowRecord) for r in got)
    assert got[0].frgn_net_qty == 100
    assert got[0].frgn_net_amt == Decimal("1000000")
    assert got[0].orgn_net_qty == -50
    # 미지정 축은 None 유지
    assert got[0].prsn_net_qty is None


@pytest.mark.asyncio
async def test_load_investor_flow_guard_exceeded():
    """상한 초과 → ValueError + 기존 캐시 보존 + SELECT 미실행."""
    rows = _build_flow_rows("005930")
    factory, session = _mock_session_factory(
        [_count_result(5), _select_result(rows), _count_result(60_000)]
    )
    loader = HistoricalDataLoader(factory)

    await loader.load_investor_flow(
        symbols=["005930"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    with pytest.raises(ValueError, match="상한"):
        await loader.load_investor_flow(
            symbols=["005930", "000660"],
            start_date=date(2020, 1, 1),
            end_date=date(2025, 1, 6),
            max_rows=50_000,
        )

    # 기존 캐시 보존 (거부가 파괴가 아님)
    got = loader.get_investor_flow_range("005930", date(2025, 1, 2), date(2025, 1, 6))
    assert len(got) == 5
    # 2차 호출은 COUNT 1회에서 중단 — 총 execute 3회 (count+select+count)
    assert session.execute.call_count == 3


@pytest.mark.asyncio
async def test_load_investor_flow_guard_boundary():
    """COUNT == max_rows는 통과."""
    rows = _build_flow_rows("005930")
    factory, _ = _mock_session_factory([_count_result(5), _select_result(rows)])
    loader = HistoricalDataLoader(factory)

    count = await loader.load_investor_flow(
        symbols=["005930"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
        max_rows=5,
    )
    assert count == 5


@pytest.mark.asyncio
async def test_load_investor_flow_batching():
    """심볼 450개 → COUNT 1회 + 배치 SELECT 3회(200/200/50)."""
    symbols = [f"{i:06d}" for i in range(450)]
    factory, session = _mock_session_factory(
        [_count_result(0), _select_result([]), _select_result([]), _select_result([])]
    )
    loader = HistoricalDataLoader(factory)

    count = await loader.load_investor_flow(
        symbols=symbols,
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    assert count == 0
    assert session.execute.call_count == 4


@pytest.mark.asyncio
async def test_load_investor_flow_replace():
    """재호출 = 종목 수급 캐시 전체 교체 — 이전 청크 심볼은 []."""
    factory, _ = _mock_session_factory(
        [
            _count_result(5),
            _select_result(_build_flow_rows("005930")),
            _count_result(5),
            _select_result(_build_flow_rows("000660")),
        ]
    )
    loader = HistoricalDataLoader(factory)
    window = (date(2025, 1, 2), date(2025, 1, 6))

    await loader.load_investor_flow(symbols=["005930"], start_date=window[0], end_date=window[1])
    assert len(loader.get_investor_flow_range("005930", *window)) == 5

    await loader.load_investor_flow(symbols=["000660"], start_date=window[0], end_date=window[1])
    assert loader.get_investor_flow_range("005930", *window) == []
    assert len(loader.get_investor_flow_range("000660", *window)) == 5


@pytest.mark.asyncio
async def test_load_investor_flow_empty():
    """행 없음 → 0 반환, range []."""
    factory, _ = _mock_session_factory([_count_result(0), _select_result([])])
    loader = HistoricalDataLoader(factory)

    count = await loader.load_investor_flow(
        symbols=["005930"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )
    assert count == 0
    assert loader.get_investor_flow_range("005930", date(2025, 1, 2), date(2025, 1, 6)) == []


@pytest.mark.asyncio
async def test_get_investor_flow_range_ascending():
    """뒤섞인 입력 → 방어 정렬로 오름차순 보장 (지표 모듈 계약)."""
    rows = _build_flow_rows("005930")
    scrambled = [rows[3], rows[0], rows[4], rows[2], rows[1]]
    factory, _ = _mock_session_factory([_count_result(5), _select_result(scrambled)])
    loader = HistoricalDataLoader(factory)

    await loader.load_investor_flow(
        symbols=["005930"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    got = loader.get_investor_flow_range("005930", date(2025, 1, 2), date(2025, 1, 6))
    assert [r.date for r in got] == [date(2025, 1, 2 + i) for i in range(5)]


@pytest.mark.asyncio
async def test_get_investor_flow_range_boundaries():
    """경계 포함·부분 범위·미적재 심볼."""
    factory, _ = _mock_session_factory(
        [_count_result(5), _select_result(_build_flow_rows("005930"))]
    )
    loader = HistoricalDataLoader(factory)
    await loader.load_investor_flow(
        symbols=["005930"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    partial = loader.get_investor_flow_range("005930", date(2025, 1, 3), date(2025, 1, 5))
    assert [r.date for r in partial] == [date(2025, 1, 3), date(2025, 1, 4), date(2025, 1, 5)]

    # 데이터 밖 범위·미적재 심볼 → []
    assert loader.get_investor_flow_range("005930", date(2025, 2, 1), date(2025, 2, 28)) == []
    assert loader.get_investor_flow_range("NONEXIST", date(2025, 1, 2), date(2025, 1, 6)) == []


def _build_market_rows(days: int = 5) -> list[MarketInvestorFlowRecord]:
    rows = []
    for market in ("kospi", "kosdaq"):
        for i in range(days):
            rows.append(
                _make_market_row(
                    market,
                    date(2025, 1, 2 + i),
                    index_close=Decimal("2500.10") + i,
                    frgn_net_amt=Decimal("300000000") + i,
                )
            )
    return rows


@pytest.mark.asyncio
async def test_load_market_investor_flow_basic():
    """기본 markets=None → kospi+kosdaq 적재."""
    factory, session = _mock_session_factory([_select_result(_build_market_rows())])
    loader = HistoricalDataLoader(factory)

    count = await loader.load_market_investor_flow(
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    assert count == 10
    assert session.execute.call_count == 1  # COUNT·배치 없이 단일 SELECT
    for market in ("kospi", "kosdaq"):
        got = loader.get_market_investor_flow_range(market, date(2025, 1, 2), date(2025, 1, 6))
        assert len(got) == 5
        assert all(isinstance(r, MarketInvestorFlowRecord) for r in got)
        assert got[0].index_close == Decimal("2500.10")


@pytest.mark.asyncio
async def test_get_market_investor_flow_range():
    """부분 범위 슬라이스 + 미적재 market []."""
    factory, _ = _mock_session_factory([_select_result(_build_market_rows())])
    loader = HistoricalDataLoader(factory)
    await loader.load_market_investor_flow(
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 6),
    )

    partial = loader.get_market_investor_flow_range("kospi", date(2025, 1, 4), date(2025, 1, 6))
    assert [r.date for r in partial] == [date(2025, 1, 4), date(2025, 1, 5), date(2025, 1, 6)]
    assert loader.get_market_investor_flow_range("nasdaq", date(2025, 1, 2), date(2025, 1, 6)) == []


@pytest.mark.asyncio
async def test_market_flow_independent_of_stock_flow():
    """종목 수급 재로드가 시장 수급 캐시를 건드리지 않음 (캐시 상호 독립)."""
    factory, _ = _mock_session_factory(
        [
            _select_result(_build_market_rows()),
            _count_result(5),
            _select_result(_build_flow_rows("005930")),
            _count_result(5),
            _select_result(_build_flow_rows("000660")),
        ]
    )
    loader = HistoricalDataLoader(factory)
    window = (date(2025, 1, 2), date(2025, 1, 6))

    await loader.load_market_investor_flow(start_date=window[0], end_date=window[1])
    await loader.load_investor_flow(symbols=["005930"], start_date=window[0], end_date=window[1])
    await loader.load_investor_flow(symbols=["000660"], start_date=window[0], end_date=window[1])

    assert len(loader.get_market_investor_flow_range("kospi", *window)) == 5
    assert len(loader.get_market_investor_flow_range("kosdaq", *window)) == 5


@pytest.mark.asyncio
async def test_flow_range_feeds_indicator_module():
    """단계 6↔7 계약 회귀 가드 — 로더 산출물을 지표 함수에 그대로 투입."""
    flow_rows = [
        _make_flow_row(
            "005930",
            date(2025, 1, 2 + i),
            frgn_net_qty=100,
            frgn_net_amt=Decimal("100"),
        )
        for i in range(5)
    ]
    # OHLCV mock row (기존 load() 경로 — trading_value 포함)
    ohlcv_rows = []
    for i in range(5):
        row = MagicMock()
        row.symbol = "005930"
        row.date = date(2025, 1, 2 + i)
        row.open = row.high = row.low = row.close = Decimal("70000")
        row.volume = 1_000_000
        row.trading_value = Decimal("1000")
        ohlcv_rows.append(row)

    factory, _ = _mock_session_factory(
        [_select_result(ohlcv_rows), _count_result(5), _select_result(flow_rows)]
    )
    loader = HistoricalDataLoader(factory)
    window = (date(2025, 1, 2), date(2025, 1, 6))

    await loader.load(symbols=["005930"], start_date=window[0], end_date=window[1])
    await loader.load_investor_flow(symbols=["005930"], start_date=window[0], end_date=window[1])

    rows = loader.get_investor_flow_range("005930", *window)
    trading_values = loader.get_trading_values("005930", *window)

    # flow_intensity: Σ100×5 ÷ Σ1000×5 = 0.1
    intensity = flow_intensity(rows, trading_values, "frgn", window=5)
    assert intensity == Decimal("0.1000")

    # compute_flow_summary: 무예외 + 요약 필드 정합
    summary = compute_flow_summary(rows, trading_values, windows=[5])
    assert summary.symbol == "005930"
    assert summary.as_of == date(2025, 1, 6)
    assert summary.days_available == 5
    frgn = next(a for a in summary.axes if a.axis == "frgn")
    assert frgn.streak == 5
    assert frgn.windows[0].net_amt == Decimal("500")
    assert frgn.windows[0].intensity == Decimal("0.1000")
