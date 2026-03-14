"""Technical analysis tool functions — indicators and chart patterns."""

from __future__ import annotations

from typing import Any

import pandas as pd
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.tools.context import ToolContext
from src.analysis.technical.indicators import compute_all_indicators
from src.analysis.technical.patterns import detect_support_resistance, scan_patterns
from src.db.models.market_data import DailyOHLCV

logger = structlog.get_logger(__name__)


async def _query_ohlcv_dataframe(
    session: AsyncSession, symbol: str, days: int
) -> pd.DataFrame:
    """Query DailyOHLCV and return a DataFrame with float columns.

    Columns: ``open, high, low, close, volume`` indexed by ``date``.
    """
    stmt = (
        select(DailyOHLCV)
        .where(DailyOHLCV.symbol == symbol)
        .order_by(DailyOHLCV.date.desc())
        .limit(days)
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())

    if not rows:
        return pd.DataFrame()

    # newest-first → chronological
    rows.reverse()

    data = {
        "date": [r.date for r in rows],
        "open": [float(r.open) for r in rows],
        "high": [float(r.high) for r in rows],
        "low": [float(r.low) for r in rows],
        "close": [float(r.close) for r in rows],
        "volume": [int(r.volume) if r.volume is not None else 0 for r in rows],
    }
    df = pd.DataFrame(data)
    df.set_index("date", inplace=True)
    return df


def _build_trend_summary(
    latest_close: float,
    sma_5: float | None,
    sma_20: float | None,
    sma_60: float | None,
) -> str:
    """Generate a one-line SMA alignment description."""
    vals: dict[str, float | None] = {
        "SMA5": sma_5,
        "SMA20": sma_20,
        "SMA60": sma_60,
    }
    available = {k: v for k, v in vals.items() if v is not None}
    if len(available) < 2:
        return "이동평균 데이터 부족"

    names = list(available.keys())
    values = list(available.values())

    if all(values[i] >= values[i + 1] for i in range(len(values) - 1)):
        return " > ".join(names) + " (상승 정배열)"
    if all(values[i] <= values[i + 1] for i in range(len(values) - 1)):
        return " < ".join(names) + " (하락 역배열)"
    return " / ".join(f"{k}={v:,.0f}" for k, v in available.items()) + " (혼조)"


async def get_technical_indicators(
    ctx: ToolContext, symbol: str, days: int = 200
) -> dict[str, Any]:
    """Compute all technical indicators and scan chart patterns.

    Returns:
        {symbol, data_points, latest: {...}, patterns: [...], trend_summary}
    """
    try:
        async with ctx.session_factory() as session:
            df = await _query_ohlcv_dataframe(session, symbol, days)

        if df.empty:
            return {
                "error": f"No OHLCV data for {symbol}",
                "symbol": symbol,
                "tool": "get_technical_indicators",
            }

        indicators = compute_all_indicators(df, symbol)
        patterns = scan_patterns(df, indicators)

        # Latest snapshot values
        last_close = float(df["close"].iloc[-1])
        last_volume = int(df["volume"].iloc[-1])

        # Volume SMA 20 latest
        vol_sma_20: float | None = None
        if indicators.volume_sma_20:
            valid = [v for v in indicators.volume_sma_20 if v is not None]
            if valid:
                vol_sma_20 = valid[-1]

        # SMA latest values for trend summary
        def _last(lst: list[float | None] | None) -> float | None:
            if not lst:
                return None
            valid = [v for v in lst if v is not None]
            return valid[-1] if valid else None

        sma_5 = _last(indicators.sma_5)
        sma_20 = _last(indicators.sma_20)
        sma_60 = _last(indicators.sma_60)
        sma_120 = _last(indicators.sma_120)

        return {
            "symbol": symbol,
            "data_points": len(df),
            "latest": {
                "rsi_14": indicators.latest_rsi,
                "macd_histogram": indicators.latest_macd_histogram,
                "stoch_k": indicators.latest_stoch_k,
                "bb_position": indicators.latest_bb_position,
                "sma_5": sma_5,
                "sma_20": sma_20,
                "sma_60": sma_60,
                "sma_120": sma_120,
                "close": last_close,
                "volume": last_volume,
                "volume_sma_20": vol_sma_20,
            },
            "patterns": [
                {
                    "pattern_name": p.pattern_name,
                    "signal_type": p.signal_type,
                    "confidence": float(p.confidence),
                    "description": p.description,
                }
                for p in patterns
            ],
            "trend_summary": _build_trend_summary(last_close, sma_5, sma_20, sma_60),
        }
    except Exception as exc:
        logger.error("tool.get_technical_indicators.error", symbol=symbol, error=str(exc))
        return {"error": str(exc), "symbol": symbol, "tool": "get_technical_indicators"}


async def scan_chart_patterns(
    ctx: ToolContext, symbol: str, days: int = 100
) -> dict[str, Any]:
    """Scan chart patterns and detect support/resistance levels.

    Returns:
        {symbol, patterns: [...], support_levels, resistance_levels, pattern_count}
    """
    try:
        async with ctx.session_factory() as session:
            df = await _query_ohlcv_dataframe(session, symbol, days)

        if df.empty:
            return {
                "error": f"No OHLCV data for {symbol}",
                "symbol": symbol,
                "tool": "scan_chart_patterns",
            }

        indicators = compute_all_indicators(df, symbol)
        patterns = scan_patterns(df, indicators)
        close = df["close"].astype(float)
        levels = detect_support_resistance(close)

        return {
            "symbol": symbol,
            "patterns": [
                {
                    "pattern_name": p.pattern_name,
                    "signal_type": p.signal_type,
                    "confidence": float(p.confidence),
                    "description": p.description,
                }
                for p in patterns
            ],
            "support_levels": levels["support"],
            "resistance_levels": levels["resistance"],
            "pattern_count": len(patterns),
        }
    except Exception as exc:
        logger.error("tool.scan_chart_patterns.error", symbol=symbol, error=str(exc))
        return {"error": str(exc), "symbol": symbol, "tool": "scan_chart_patterns"}
