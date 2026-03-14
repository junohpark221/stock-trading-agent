"""Market data tool functions — current price and summary statistics."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agent.tools.context import ToolContext
from src.db.models.market_data import DailyOHLCV

logger = structlog.get_logger(__name__)


async def get_current_price(ctx: ToolContext, symbol: str) -> dict[str, Any]:
    """Fetch the latest OHLCV row for *symbol* from DB.

    Returns:
        {symbol, close, date, change_rate, volume, open, high, low}
    """
    try:
        async with ctx.session_factory() as session:
            stmt = (
                select(DailyOHLCV)
                .where(DailyOHLCV.symbol == symbol)
                .order_by(DailyOHLCV.date.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalars().first()

        if row is None:
            return {"error": f"No price data for {symbol}", "symbol": symbol, "tool": "get_current_price"}

        return {
            "symbol": symbol,
            "close": float(row.close),
            "date": str(row.date),
            "change_rate": float(row.change_rate) if row.change_rate is not None else None,
            "volume": int(row.volume) if row.volume is not None else None,
            "open": float(row.open) if row.open is not None else None,
            "high": float(row.high) if row.high is not None else None,
            "low": float(row.low) if row.low is not None else None,
        }
    except Exception as exc:
        logger.error("tool.get_current_price.error", symbol=symbol, error=str(exc))
        return {"error": str(exc), "symbol": symbol, "tool": "get_current_price"}


async def get_market_data_summary(
    ctx: ToolContext, symbol: str, days: int = 60
) -> dict[str, Any]:
    """Compute summary statistics for *symbol* over the last *days* trading days.

    Returns:
        {symbol, period_days, period_high, period_low, avg_close, avg_volume,
         volatility, current_close, price_change_pct, volume_trend}
    """
    try:
        async with ctx.session_factory() as session:
            stmt = (
                select(DailyOHLCV)
                .where(DailyOHLCV.symbol == symbol)
                .order_by(DailyOHLCV.date.desc())
                .limit(days)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        if not rows:
            return {"error": f"No data for {symbol}", "symbol": symbol, "tool": "get_market_data_summary"}

        # rows are newest-first; reverse for chronological order
        rows.reverse()

        closes = [float(r.close) for r in rows]
        volumes = [int(r.volume) for r in rows if r.volume is not None]

        period_high = max(float(r.high) for r in rows if r.high is not None) if rows else None
        period_low = min(float(r.low) for r in rows if r.low is not None) if rows else None
        avg_close = sum(closes) / len(closes) if closes else None
        avg_volume = sum(volumes) / len(volumes) if volumes else None

        # Daily return volatility (std dev of pct changes)
        volatility: float | None = None
        if len(closes) >= 2:
            returns = [
                (closes[i] - closes[i - 1]) / closes[i - 1]
                for i in range(1, len(closes))
                if closes[i - 1] != 0
            ]
            if returns:
                mean_ret = sum(returns) / len(returns)
                variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
                volatility = round(variance**0.5, 6)

        current_close = closes[-1] if closes else None
        first_close = closes[0] if closes else None
        price_change_pct: float | None = None
        if current_close is not None and first_close is not None and first_close != 0:
            price_change_pct = round(
                (current_close - first_close) / first_close * 100, 2
            )

        # Volume trend: avg volume of last 5 days vs overall avg
        volume_trend: str | None = None
        if avg_volume and len(volumes) >= 5:
            recent_vol = sum(volumes[-5:]) / 5
            if recent_vol > avg_volume * 1.2:
                volume_trend = "increasing"
            elif recent_vol < avg_volume * 0.8:
                volume_trend = "decreasing"
            else:
                volume_trend = "stable"

        return {
            "symbol": symbol,
            "period_days": len(rows),
            "period_high": period_high,
            "period_low": period_low,
            "avg_close": round(avg_close, 2) if avg_close else None,
            "avg_volume": int(avg_volume) if avg_volume else None,
            "volatility": volatility,
            "current_close": current_close,
            "price_change_pct": price_change_pct,
            "volume_trend": volume_trend,
        }
    except Exception as exc:
        logger.error("tool.get_market_data_summary.error", symbol=symbol, error=str(exc))
        return {"error": str(exc), "symbol": symbol, "tool": "get_market_data_summary"}
