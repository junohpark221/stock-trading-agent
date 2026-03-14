"""Macro economic indicator tool functions."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.sql import func

from src.agent.tools.context import ToolContext
from src.db.models.analysis import EconomicIndicator

logger = structlog.get_logger(__name__)

# Well-known indicator codes to query
_ECOS_INDICATORS = {
    "base_rate": ("722Y001", "0101000", "기준금리"),
    "usd_krw": ("731Y003", "0000001", "원/달러 환율"),
    "cpi": ("901Y009", "0", "소비자물가지수"),
    "gdp_growth": ("200Y002", "10111", "GDP 성장률"),
    "m2": ("101Y003", "BBGA00", "M2 통화량"),
}

_FRED_INDICATORS = {
    "fed_funds": ("FEDFUNDS", "Federal Funds Rate"),
    "cpi_us": ("CPIAUCSL", "US CPI"),
    "unemployment": ("UNRATE", "US Unemployment Rate"),
    "treasury_10y": ("GS10", "10-Year Treasury"),
    "vix": ("VIXCLS", "VIX"),
}


async def get_macro_indicators(ctx: ToolContext) -> dict[str, Any]:
    """Fetch the latest value for each well-known macro indicator from DB.

    Returns:
        {korean: {base_rate: {value, date, name}, ...},
         us: {fed_funds: {value, date, name}, ...}, fetched_at}
    """
    try:
        korean: dict[str, Any] = {}
        us: dict[str, Any] = {}

        async with ctx.session_factory() as session:
            # ECOS indicators
            for key, (stat_code, item_code, name) in _ECOS_INDICATORS.items():
                stmt = (
                    select(EconomicIndicator)
                    .where(
                        EconomicIndicator.source == "ecos",
                        EconomicIndicator.indicator_code == f"{stat_code}:{item_code}",
                    )
                    .order_by(EconomicIndicator.date.desc())
                    .limit(1)
                )
                result = await session.execute(stmt)
                row = result.scalars().first()
                if row is not None:
                    korean[key] = {
                        "value": float(row.value),
                        "date": str(row.date),
                        "name": name,
                    }

            # FRED indicators
            for key, (series_id, name) in _FRED_INDICATORS.items():
                stmt = (
                    select(EconomicIndicator)
                    .where(
                        EconomicIndicator.source == "fred",
                        EconomicIndicator.indicator_code == series_id,
                    )
                    .order_by(EconomicIndicator.date.desc())
                    .limit(1)
                )
                result = await session.execute(stmt)
                row = result.scalars().first()
                if row is not None:
                    us[key] = {
                        "value": float(row.value),
                        "date": str(row.date),
                        "name": name,
                    }

        from datetime import datetime, timezone

        return {
            "korean": korean,
            "us": us,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        logger.error("tool.get_macro_indicators.error", error=str(exc))
        return {"error": str(exc), "tool": "get_macro_indicators"}
