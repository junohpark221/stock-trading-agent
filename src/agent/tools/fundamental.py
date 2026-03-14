"""Fundamental analysis tool functions — scores and financial statements."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.tools.context import ToolContext
from src.db.models.analysis import FinancialStatement

logger = structlog.get_logger(__name__)


async def get_fundamental_score(ctx: ToolContext, symbol: str) -> dict[str, Any]:
    """Compute fundamental score for *symbol* via FundamentalAnalyzer.

    Returns:
        {symbol, overall_score, valuation_score, growth_score, profitability_score, reasoning}
    """
    try:
        score = await ctx.fundamental_analyzer.analyze(symbol)
        return score.model_dump(mode="json")
    except Exception as exc:
        logger.error("tool.get_fundamental_score.error", symbol=symbol, error=str(exc))
        return {"error": str(exc), "symbol": symbol, "tool": "get_fundamental_score"}


async def get_financial_statements(
    ctx: ToolContext, symbol: str
) -> dict[str, Any]:
    """Fetch the 3 most recent annual financial statements from DB.

    Returns:
        {symbol, statements: [{fiscal_year, revenue, operating_income, ...}], count}
    """
    try:
        async with ctx.session_factory() as session:
            stmt = (
                select(FinancialStatement)
                .where(
                    FinancialStatement.symbol == symbol,
                    FinancialStatement.report_type == "annual",
                )
                .order_by(FinancialStatement.fiscal_year.desc())
                .limit(3)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        statements = []
        for r in rows:
            statements.append({
                "fiscal_year": r.fiscal_year,
                "revenue": float(r.revenue) if r.revenue is not None else None,
                "operating_income": float(r.operating_income) if r.operating_income is not None else None,
                "net_income": float(r.net_income) if r.net_income is not None else None,
                "total_assets": float(r.total_assets) if r.total_assets is not None else None,
                "total_equity": float(r.total_equity) if r.total_equity is not None else None,
                "total_liabilities": float(r.total_liabilities) if r.total_liabilities is not None else None,
                "per": float(r.per) if r.per is not None else None,
                "pbr": float(r.pbr) if r.pbr is not None else None,
                "roe": float(r.roe) if r.roe is not None else None,
                "eps": float(r.eps) if r.eps is not None else None,
                "bps": float(r.bps) if r.bps is not None else None,
            })

        return {
            "symbol": symbol,
            "statements": statements,
            "count": len(statements),
        }
    except Exception as exc:
        logger.error("tool.get_financial_statements.error", symbol=symbol, error=str(exc))
        return {"error": str(exc), "symbol": symbol, "tool": "get_financial_statements"}
