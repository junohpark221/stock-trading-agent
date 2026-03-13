"""Unit tests for pykrx market helpers (Phase 2 Step 9).

pykrx and DB calls are fully mocked.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from src.data.utils.market_helpers import (
    PRICE_MISMATCH_THRESHOLD_PCT,
    VOLUME_MISMATCH_THRESHOLD_PCT,
    fetch_pykrx_ohlcv,
    validate_ohlcv_with_pykrx,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_pykrx_df(data: list[dict], dates: list[str] | None = None) -> pd.DataFrame:
    """Build a DataFrame mimicking pykrx output (한글 컬럼, DatetimeIndex)."""
    if dates is None:
        dates = [f"2025-01-{i + 1:02d}" for i in range(len(data))]
    idx = pd.to_datetime(dates)
    return pd.DataFrame(data, index=idx)


def _make_db_row(*, dt: date, close: float, volume: int) -> MagicMock:
    row = MagicMock()
    row.date = dt
    row.close = close
    row.volume = volume
    return row


def _mock_session_factory(rows: list | None = None):
    """Create a mock async session factory returning given rows."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = rows or []
    mock_result.scalars.return_value = mock_scalars
    mock_session.execute = AsyncMock(return_value=mock_result)

    async def _factory():
        return mock_session

    # Make it work as async context manager
    class SessionCtx:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            pass

    return lambda: SessionCtx()


# ── TestFetchPykrxOhlcv ──────────────────────────────────────────────


class TestFetchPykrxOhlcv:
    """fetch_pykrx_ohlcv 테스트."""

    @pytest.mark.asyncio()
    @patch("src.data.utils.market_helpers.asyncio.to_thread")
    async def test_returns_dataframe(self, mock_to_thread):
        raw_df = _make_pykrx_df(
            [{"시가": 70000, "고가": 72000, "저가": 69000, "종가": 71000, "거래량": 1000}]
        )
        mock_to_thread.return_value = raw_df

        result = await fetch_pykrx_ohlcv("005930", date(2025, 1, 1), date(2025, 1, 31))
        assert list(result.columns) == ["open", "high", "low", "close", "volume"]
        assert len(result) == 1
        assert result.iloc[0]["close"] == 71000

    @pytest.mark.asyncio()
    @patch("src.data.utils.market_helpers.asyncio.to_thread")
    async def test_empty_result(self, mock_to_thread):
        mock_to_thread.return_value = pd.DataFrame()
        result = await fetch_pykrx_ohlcv("005930", date(2025, 1, 1), date(2025, 1, 31))
        assert result.empty
        assert list(result.columns) == ["open", "high", "low", "close", "volume"]

    @pytest.mark.asyncio()
    @patch("src.data.utils.market_helpers.asyncio.to_thread")
    async def test_column_rename(self, mock_to_thread):
        raw_df = _make_pykrx_df(
            [{"시가": 100, "고가": 200, "저가": 50, "종가": 150, "거래량": 500}]
        )
        mock_to_thread.return_value = raw_df
        result = await fetch_pykrx_ohlcv("005930", date(2025, 1, 1), date(2025, 1, 31))
        assert "시가" not in result.columns
        assert "open" in result.columns

    @pytest.mark.asyncio()
    @patch("src.data.utils.market_helpers.asyncio.to_thread")
    async def test_exception_returns_empty_df(self, mock_to_thread):
        mock_to_thread.side_effect = RuntimeError("pykrx error")
        result = await fetch_pykrx_ohlcv("005930", date(2025, 1, 1), date(2025, 1, 31))
        assert result.empty
        assert list(result.columns) == ["open", "high", "low", "close", "volume"]

    @pytest.mark.asyncio()
    @patch("src.data.utils.market_helpers.asyncio.to_thread")
    async def test_date_format_passed(self, mock_to_thread):
        mock_to_thread.return_value = pd.DataFrame()
        await fetch_pykrx_ohlcv("005930", date(2025, 3, 15), date(2025, 6, 30))
        # to_thread 호출 시 내부 _fetch 함수가 전달됨 — 호출 확인
        mock_to_thread.assert_called_once()


# ── TestValidateOhlcvWithPykrx ───────────────────────────────────────


class TestValidateOhlcvWithPykrx:
    """validate_ohlcv_with_pykrx 테스트."""

    @pytest.mark.asyncio()
    @patch("src.data.utils.market_helpers.fetch_pykrx_ohlcv")
    async def test_full_match(self, mock_fetch):
        """KIS와 pykrx 데이터가 완전 일치."""
        dt = date(2025, 1, 2)
        db_rows = [_make_db_row(dt=dt, close=70000, volume=1000)]
        session_factory = _mock_session_factory(db_rows)

        pykrx_df = pd.DataFrame(
            [{"open": 69000, "high": 71000, "low": 68000, "close": 70000, "volume": 1000}],
            index=pd.to_datetime(["2025-01-02"]),
        )
        mock_fetch.return_value = pykrx_df

        result = await validate_ohlcv_with_pykrx("005930", dt, dt, session_factory)
        assert result["matched_dates"] == 1
        assert result["price_mismatches"] == 0
        assert result["volume_mismatches"] == 0
        assert result["mismatched_dates"] == []

    @pytest.mark.asyncio()
    @patch("src.data.utils.market_helpers.fetch_pykrx_ohlcv")
    async def test_price_mismatch(self, mock_fetch):
        """종가 차이가 임계값 초과."""
        dt = date(2025, 1, 2)
        # KIS: 70000, pykrx: 72000 → 2.78% 차이 > 1%
        db_rows = [_make_db_row(dt=dt, close=70000, volume=1000)]
        session_factory = _mock_session_factory(db_rows)

        pykrx_df = pd.DataFrame(
            [{"open": 69000, "high": 73000, "low": 68000, "close": 72000, "volume": 1000}],
            index=pd.to_datetime(["2025-01-02"]),
        )
        mock_fetch.return_value = pykrx_df

        result = await validate_ohlcv_with_pykrx("005930", dt, dt, session_factory)
        assert result["price_mismatches"] == 1
        assert "2025-01-02" in result["mismatched_dates"]

    @pytest.mark.asyncio()
    @patch("src.data.utils.market_helpers.fetch_pykrx_ohlcv")
    async def test_volume_mismatch(self, mock_fetch):
        """거래량 차이가 임계값 초과."""
        dt = date(2025, 1, 2)
        # KIS: 1000, pykrx: 2000 → 50% 차이 > 5%
        db_rows = [_make_db_row(dt=dt, close=70000, volume=1000)]
        session_factory = _mock_session_factory(db_rows)

        pykrx_df = pd.DataFrame(
            [{"open": 69000, "high": 71000, "low": 68000, "close": 70000, "volume": 2000}],
            index=pd.to_datetime(["2025-01-02"]),
        )
        mock_fetch.return_value = pykrx_df

        result = await validate_ohlcv_with_pykrx("005930", dt, dt, session_factory)
        assert result["volume_mismatches"] == 1
        assert "2025-01-02" in result["mismatched_dates"]

    @pytest.mark.asyncio()
    @patch("src.data.utils.market_helpers.fetch_pykrx_ohlcv")
    async def test_within_threshold(self, mock_fetch):
        """차이가 임계값 이내면 불일치로 카운트하지 않음."""
        dt = date(2025, 1, 2)
        # KIS close: 70000, pykrx close: 70500 → 0.71% < 1%
        # KIS volume: 1000, pykrx volume: 1040 → 4% < 5%
        db_rows = [_make_db_row(dt=dt, close=70000, volume=1000)]
        session_factory = _mock_session_factory(db_rows)

        pykrx_df = pd.DataFrame(
            [{"open": 69000, "high": 71000, "low": 68000, "close": 70500, "volume": 1040}],
            index=pd.to_datetime(["2025-01-02"]),
        )
        mock_fetch.return_value = pykrx_df

        result = await validate_ohlcv_with_pykrx("005930", dt, dt, session_factory)
        assert result["price_mismatches"] == 0
        assert result["volume_mismatches"] == 0

    @pytest.mark.asyncio()
    @patch("src.data.utils.market_helpers.fetch_pykrx_ohlcv")
    async def test_pykrx_empty(self, mock_fetch):
        """pykrx 데이터 없음."""
        dt = date(2025, 1, 2)
        db_rows = [_make_db_row(dt=dt, close=70000, volume=1000)]
        session_factory = _mock_session_factory(db_rows)

        mock_fetch.return_value = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        result = await validate_ohlcv_with_pykrx("005930", dt, dt, session_factory)
        assert result["kis_rows"] == 1
        assert result["pykrx_rows"] == 0
        assert result["matched_dates"] == 0

    @pytest.mark.asyncio()
    @patch("src.data.utils.market_helpers.fetch_pykrx_ohlcv")
    async def test_db_empty(self, mock_fetch):
        """DB 데이터 없음."""
        dt = date(2025, 1, 2)
        session_factory = _mock_session_factory([])

        pykrx_df = pd.DataFrame(
            [{"open": 69000, "high": 71000, "low": 68000, "close": 70000, "volume": 1000}],
            index=pd.to_datetime(["2025-01-02"]),
        )
        mock_fetch.return_value = pykrx_df

        result = await validate_ohlcv_with_pykrx("005930", dt, dt, session_factory)
        assert result["kis_rows"] == 0
        assert result["pykrx_rows"] == 1
        assert result["matched_dates"] == 0
