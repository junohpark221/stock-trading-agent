"""Investor flow tool functions — 수급(투자자별 순매수) 요약 통계 (PRJ-03 단계 8).

DB의 수급 원시 행을 조회해 `src.analysis.investor_flow`의 순수 지표 함수로
요약(N일 누적·스트릭·거래대금 대비 강도)을 계산하고, 프롬프트 주입 안전한
`model_dump(mode="json")` dict를 반환한다. 원시 시계열은 주입하지 않는다(확정 8).

⚠️ 소비 대상 = 주권(ST)·리츠(RT)만 — ETF·ETN 제외 근거 3종(영구 미검증·백필
결손·LP 혼입)은 `src/analysis/investor_flow.py` 모듈 독스트링 참조. 판별은
`stock_master.security_group`(mst 증권그룹코드) 기반이며, NULL(동기화 전/mst
미수록)은 보수적으로 제외한다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select

from src.agent.tools.context import ToolContext
from src.analysis.investor_flow import compute_flow_summary, compute_market_flow_summary
from src.core.models import InvestorFlowRecord, MarketInvestorFlowRecord
from src.db.models.investor_flow import InvestorFlowDaily, MarketInvestorFlowDaily
from src.db.models.market_data import DailyOHLCV, StockMaster

logger = structlog.get_logger(__name__)

# 수급 소비 허용 증권그룹 (07-24 사용자 확정: 주권 + 리츠)
_ALLOWED_GROUPS = frozenset({"ST", "RT"})
# 기본 조회 행수 = 최대 윈도(DEFAULT_WINDOWS=(5,20,60)). 상한은 payload 가드
# (수급 행은 90필드로 무거움 — LLM이 과대 days를 넘겨도 폭주 방지).
_DEFAULT_DAYS = 60
_MIN_DAYS = 5
_MAX_DAYS = 120
_VALID_MARKETS = ("kospi", "kosdaq")


def _clamp_days(days: int) -> int:
    return max(_MIN_DAYS, min(days, _MAX_DAYS))


async def get_investor_flow_summary(
    ctx: ToolContext, symbol: str, days: int = _DEFAULT_DAYS
) -> dict[str, Any]:
    """종목의 투자자별 순매수 수급 요약을 계산한다.

    Returns:
        정상: InvestorFlowSummary.model_dump(mode="json")
              {symbol, as_of, days_available, axes: [{axis, label, caveat,
               streak, windows: [{window, days_covered, net_amt, net_qty,
               intensity}]}]}
        소비 제외(비 ST/RT·미상): {excluded, reason, symbol, security_group, tool}
        데이터 없음/예외: {error, symbol, tool}
    """
    tool_name = "get_investor_flow_summary"
    try:
        days = _clamp_days(days)
        async with ctx.session_factory() as session:
            group_result = await session.execute(
                select(StockMaster.security_group).where(StockMaster.symbol == symbol)
            )
            security_group = group_result.scalar_one_or_none()
            if security_group not in _ALLOWED_GROUPS:
                # NULL(동기화 전/mst 미수록/마스터 부재)도 보수적 제외
                logger.info(
                    "tool.get_investor_flow_summary.excluded",
                    symbol=symbol,
                    security_group=security_group,
                )
                return {
                    "excluded": True,
                    "reason": (
                        "수급 지표는 주권(ST)·리츠(RT)만 소비 — "
                        f"security_group={security_group or '미상'}"
                    ),
                    "symbol": symbol,
                    "security_group": security_group,
                    "tool": tool_name,
                }

            stmt = (
                select(InvestorFlowDaily)
                .where(InvestorFlowDaily.symbol == symbol)
                .order_by(InvestorFlowDaily.date.desc())
                .limit(days)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

            if not rows:
                return {
                    "error": f"No investor flow data for {symbol}",
                    "symbol": symbol,
                    "tool": tool_name,
                }

            # newest-first → chronological (지표 함수의 오름차순 계약)
            rows.reverse()
            records = [InvestorFlowRecord.model_validate(r) for r in rows]

            # intensity 분모: 같은 날짜 창의 거래대금(원)
            tv_result = await session.execute(
                select(DailyOHLCV.date, DailyOHLCV.trading_value).where(
                    DailyOHLCV.symbol == symbol,
                    DailyOHLCV.date >= records[0].date,
                    DailyOHLCV.date <= records[-1].date,
                )
            )
            trading_values: dict[date, Decimal | None] = {
                d: tv for d, tv in tv_result.all()
            }

        summary = compute_flow_summary(records, trading_values)
        return summary.model_dump(mode="json")
    except Exception as exc:
        logger.error(
            "tool.get_investor_flow_summary.error", symbol=symbol, error=str(exc)
        )
        return {
            "error": f"Investor flow summary failed: {exc}",
            "symbol": symbol,
            "tool": tool_name,
        }


async def get_market_investor_flow_summary(
    ctx: ToolContext, market: str | None = None, days: int = _DEFAULT_DAYS
) -> dict[str, Any]:
    """시장(KOSPI/KOSDAQ) 단위 투자자별 순매수 수급 요약을 계산한다.

    market 생략 시 두 시장 모두 조회한다. 한쪽 결손은 해당 키에 error를 넣고
    나머지는 정상 제공(부분 성공 허용).

    Returns:
        정상: {markets: {kospi: MarketFlowSummary.model_dump(mode="json"), kosdaq: ...}, tool}
              (요약에는 지수 컨텍스트 index_close/index_change_rate/index_window_returns 포함)
        시장값 오류/전체 결손/예외: {error, tool}
    """
    tool_name = "get_market_investor_flow_summary"
    try:
        days = _clamp_days(days)
        if market is not None and market not in _VALID_MARKETS:
            return {
                "error": f"Invalid market: {market} (expected 'kospi' or 'kosdaq')",
                "tool": tool_name,
            }
        targets = [market] if market is not None else list(_VALID_MARKETS)

        markets: dict[str, dict[str, Any]] = {}
        async with ctx.session_factory() as session:
            for target in targets:
                stmt = (
                    select(MarketInvestorFlowDaily)
                    .where(MarketInvestorFlowDaily.market == target)
                    .order_by(MarketInvestorFlowDaily.date.desc())
                    .limit(days)
                )
                result = await session.execute(stmt)
                rows = list(result.scalars().all())
                if not rows:
                    markets[target] = {"error": f"No market flow data for {target}"}
                    continue
                rows.reverse()
                records = [MarketInvestorFlowRecord.model_validate(r) for r in rows]
                markets[target] = compute_market_flow_summary(records).model_dump(
                    mode="json"
                )

        if all("error" in m for m in markets.values()):
            return {"error": "No market investor flow data", "tool": tool_name}
        return {"markets": markets, "tool": tool_name}
    except Exception as exc:
        logger.error(
            "tool.get_market_investor_flow_summary.error",
            market=market,
            error=str(exc),
        )
        return {
            "error": f"Market investor flow summary failed: {exc}",
            "tool": tool_name,
        }
