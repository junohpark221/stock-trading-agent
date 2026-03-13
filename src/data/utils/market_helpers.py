"""pykrx-based market data helpers for validation and cross-checking.

Provides:
- ``fetch_pykrx_ohlcv``: async wrapper around pykrx synchronous API
- ``validate_ohlcv_with_pykrx``: cross-validate KIS OHLCV data against pykrx
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import TYPE_CHECKING, Any

import pandas as pd
import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────

PRICE_MISMATCH_THRESHOLD_PCT = 1.0
VOLUME_MISMATCH_THRESHOLD_PCT = 5.0

# pykrx 한글 → 영문 컬럼 매핑
_PYKRX_COL_MAP = {
    "시가": "open",
    "고가": "high",
    "저가": "low",
    "종가": "close",
    "거래량": "volume",
}


async def fetch_pykrx_ohlcv(
    symbol: str,
    start: date,
    end: date,
) -> pd.DataFrame:
    """Fetch OHLCV data from pykrx (runs in thread to avoid blocking).

    Args:
        symbol: Stock symbol (e.g. ``"005930"``).
        start: Start date (inclusive).
        end: End date (inclusive).

    Returns:
        DataFrame with columns ``[open, high, low, close, volume]``
        indexed by date. Empty DataFrame on error.
    """
    try:
        def _fetch() -> pd.DataFrame:
            from pykrx import stock  # lazy import

            start_str = start.strftime("%Y%m%d")
            end_str = end.strftime("%Y%m%d")
            return stock.get_market_ohlcv_by_date(start_str, end_str, symbol)

        df = await asyncio.to_thread(_fetch)

        if df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        # 한글 컬럼명 → 영문 변환
        df = df.rename(columns=_PYKRX_COL_MAP)
        # 필요 컬럼만 유지
        keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        return df[keep]

    except Exception:
        logger.exception("fetch_pykrx_ohlcv_failed", symbol=symbol)
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


async def validate_ohlcv_with_pykrx(
    symbol: str,
    start: date,
    end: date,
    session_factory: Callable[..., AsyncGenerator[AsyncSession, None]],
) -> dict[str, Any]:
    """Cross-validate KIS OHLCV data in DB against pykrx data.

    Args:
        symbol: Stock symbol.
        start: Start date.
        end: End date.
        session_factory: Async session context manager factory.

    Returns:
        Validation result dict with match/mismatch counts.
    """
    from sqlalchemy import select

    from src.db.models.market_data import DailyOHLCV

    # DB에서 KIS 데이터 조회
    async with session_factory() as session:
        stmt = (
            select(DailyOHLCV)
            .where(
                DailyOHLCV.symbol == symbol,
                DailyOHLCV.date >= start,
                DailyOHLCV.date <= end,
            )
            .order_by(DailyOHLCV.date)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    # KIS 데이터를 dict[date, row]로 변환
    kis_by_date: dict[date, Any] = {row.date: row for row in rows}

    # pykrx 데이터 조회
    pykrx_df = await fetch_pykrx_ohlcv(symbol, start, end)

    matched_dates = 0
    price_mismatches = 0
    volume_mismatches = 0
    mismatched_dates: list[str] = []

    for idx_date, pykrx_row in pykrx_df.iterrows():
        # pykrx index는 Timestamp → date 변환
        row_date = idx_date.date() if hasattr(idx_date, "date") else idx_date

        kis_row = kis_by_date.get(row_date)
        if kis_row is None:
            continue

        matched_dates += 1
        has_mismatch = False

        # 가격 비교 (close 기준)
        kis_close = float(kis_row.close)
        pykrx_close = float(pykrx_row["close"])
        if pykrx_close != 0:
            price_diff_pct = abs(kis_close - pykrx_close) / pykrx_close * 100
            if price_diff_pct > PRICE_MISMATCH_THRESHOLD_PCT:
                price_mismatches += 1
                has_mismatch = True

        # 거래량 비교
        kis_volume = int(kis_row.volume)
        pykrx_volume = int(pykrx_row["volume"])
        if pykrx_volume != 0:
            volume_diff_pct = abs(kis_volume - pykrx_volume) / pykrx_volume * 100
            if volume_diff_pct > VOLUME_MISMATCH_THRESHOLD_PCT:
                volume_mismatches += 1
                has_mismatch = True

        if has_mismatch:
            mismatched_dates.append(str(row_date))

    return {
        "symbol": symbol,
        "kis_rows": len(rows),
        "pykrx_rows": len(pykrx_df),
        "matched_dates": matched_dates,
        "price_mismatches": price_mismatches,
        "volume_mismatches": volume_mismatches,
        "mismatched_dates": mismatched_dates,
    }
