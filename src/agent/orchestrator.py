"""PipelineOrchestrator — 4-agent pipeline execution.

MarketAnalyst → StockAnalyst(병렬) → RiskManager → Trader 순서로
전체 분석 파이프라인을 하나의 execute() 호출로 실행한다.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from src.agent.agents.market_analyst import MarketAnalyst
from src.agent.agents.risk_manager import RiskManager
from src.agent.agents.stock_analyst import StockAnalyst
from src.agent.agents.trader import Trader
from src.agent.decision_recorder import DecisionRecorder
from src.core.enums import DecisionAction
from src.core.models import (
    MarketCondition,
    PipelineResult,
    RiskAssessment,
    StockAnalysis,
    TradeDecision,
)

logger = logging.getLogger(__name__)


@dataclass
class _SymbolResult:
    """종목별 파이프라인 결과."""

    symbol: str
    stock_analysis: StockAnalysis | None = None
    risk_assessment: RiskAssessment | None = None
    trade_decision: TradeDecision | None = None
    error: str | None = None


class PipelineOrchestrator:
    """4개 에이전트를 순차/병렬로 체이닝하여 전체 분석 파이프라인을 실행."""

    def __init__(
        self,
        *,
        market_analyst: MarketAnalyst,
        stock_analyst: StockAnalyst,
        risk_manager: RiskManager,
        trader: Trader,
        recorder: DecisionRecorder,
        max_concurrency: int = 5,
    ) -> None:
        self._market_analyst = market_analyst
        self._stock_analyst = stock_analyst
        self._risk_manager = risk_manager
        self._trader = trader
        self._recorder = recorder
        self._max_concurrency = max_concurrency

    async def execute(
        self,
        symbols: list[str],
        *,
        session_id: uuid.UUID | None = None,
        investment_prompt: str | None = None,
        risk_tolerance: str = "moderate",
        account_id: str = "default",
        names: dict[str, str] | None = None,
    ) -> PipelineResult:
        """전체 파이프라인 실행.

        Args:
            symbols: 분석 대상 종목 코드 리스트
            session_id: 세션 ID (None이면 자동 생성)
            names: 종목코드→종목명 매핑(F-19). 분석/리스크 프롬프트에 종목명 노출용.
                누락 종목은 코드만 사용.

        Returns:
            PipelineResult with all analysis results
        """
        sid = session_id or uuid.uuid4()
        started_at = datetime.now(UTC)
        result = PipelineResult(
            session_id=sid,
            started_at=started_at,
            symbols_requested=list(symbols),
            account_id=account_id,
        )

        # 1. 시장 분석 — 실패 시 전체 중단
        try:
            market_condition, market_decision_id = await self._run_market_analysis(
                sid, account_id=account_id,
            )
            result.market_condition = market_condition
        except Exception as exc:
            logger.error("Market analysis failed: %s", exc, exc_info=True)
            result.success = False
            result.errors.append(f"Market analysis failed: {exc}")
            result.completed_at = datetime.now(UTC)
            return result

        # 2. 종목별 파이프라인 병렬 실행
        semaphore = asyncio.Semaphore(self._max_concurrency)
        tasks = [
            self._run_symbol_pipeline(
                symbol,
                session_id=sid,
                market_condition=market_condition,
                market_decision_id=market_decision_id,
                semaphore=semaphore,
                investment_prompt=investment_prompt,
                risk_tolerance=risk_tolerance,
                account_id=account_id,
                name=(names or {}).get(symbol),
            )
            for symbol in symbols
        ]
        symbol_results: list[_SymbolResult | BaseException] = await asyncio.gather(
            *tasks, return_exceptions=True,
        )

        # 3. 결과 집계
        for sr in symbol_results:
            if isinstance(sr, BaseException):
                # gather에서 잡지 못한 예외 (일반적으로 발생하지 않음)
                result.errors.append(str(sr))
                continue

            if sr.error is not None:
                result.symbols_skipped.append(sr.symbol)
                result.errors.append(f"{sr.symbol}: {sr.error}")
                continue

            result.symbols_analyzed.append(sr.symbol)
            if sr.stock_analysis is not None:
                result.stock_analyses.append(sr.stock_analysis)
            if sr.risk_assessment is not None:
                result.risk_assessments.append(sr.risk_assessment)
            if sr.trade_decision is not None:
                result.trade_decisions.append(sr.trade_decision)

        # 4. 비용 집계
        try:
            decisions = await self._recorder.get_session_decisions(sid)
            total_cost = Decimal(0)
            for d in decisions:
                if d.llm_cost_usd is not None:
                    total_cost += d.llm_cost_usd
            result.total_llm_cost_usd = total_cost
            result.total_llm_calls = len(decisions)
        except Exception as exc:
            logger.warning("Cost aggregation failed: %s", exc, exc_info=True)

        result.completed_at = datetime.now(UTC)
        return result

    # ── 내부 메서드 ─────────────────────────────────────────

    async def _run_market_analysis(
        self, session_id: uuid.UUID, *, account_id: str = "default",
    ) -> tuple[MarketCondition, uuid.UUID]:
        """시장 분석 실행."""
        mc, decision_id = await self._market_analyst.analyze(
            data={}, session_id=session_id, parent_id=None,
            account_id=account_id,
        )
        return mc, decision_id  # type: ignore[return-value]

    async def _run_symbol_pipeline(
        self,
        symbol: str,
        *,
        session_id: uuid.UUID,
        market_condition: MarketCondition,
        market_decision_id: uuid.UUID,
        semaphore: asyncio.Semaphore,
        investment_prompt: str | None = None,
        risk_tolerance: str = "moderate",
        account_id: str = "default",
        name: str | None = None,
    ) -> _SymbolResult:
        """종목별 Stock → Risk → Trade 파이프라인 실행."""
        async with semaphore:
            sr = _SymbolResult(symbol=symbol)
            # F-19: 종목명이 있으면 분석/리스크/트레이드 데이터에 실어 프롬프트에 노출.
            _name_kv = {"name": name} if name else {}

            # ── StockAnalyst ──
            try:
                sa, stock_decision_id = await self._stock_analyst.analyze(
                    data={
                        "symbol": symbol,
                        **_name_kv,
                        "market_condition": market_condition.model_dump(),
                    },
                    session_id=session_id,
                    parent_id=market_decision_id,
                    symbol=symbol,
                    investment_prompt=investment_prompt,
                    account_id=account_id,
                )
                sr.stock_analysis = sa  # type: ignore[assignment]
            except Exception as exc:
                sr.error = f"Stock analysis failed: {exc}"
                return sr

            # HOLD → 정상 종료 (Risk/Trade skip)
            if sa.action == DecisionAction.HOLD:  # type: ignore[union-attr]
                return sr

            # ── RiskManager ──
            try:
                ra, risk_decision_id = await self._risk_manager.analyze(
                    data={
                        "symbol": symbol,
                        **_name_kv,
                        "stock_analysis": sa.model_dump(),  # type: ignore[union-attr]
                        "market_condition": market_condition.model_dump(),
                        "risk_tolerance": risk_tolerance,
                    },
                    session_id=session_id,
                    parent_id=stock_decision_id,
                    symbol=symbol,
                    investment_prompt=investment_prompt,
                    account_id=account_id,
                )
                sr.risk_assessment = ra  # type: ignore[assignment]
            except Exception as exc:
                sr.error = f"Risk assessment failed: {exc}"
                return sr

            # Risk 거부 → 정상 종료 (Trade skip)
            if not ra.approved:  # type: ignore[union-attr]
                return sr

            # ── Trader ──
            try:
                td, _trade_decision_id = await self._trader.analyze(
                    data={
                        "symbol": symbol,
                        **_name_kv,
                        "risk_assessment": ra.model_dump(),  # type: ignore[union-attr]
                        "stock_analysis": sa.model_dump(),  # type: ignore[union-attr]
                        "market_condition": market_condition.model_dump(),
                    },
                    session_id=session_id,
                    parent_id=risk_decision_id,
                    symbol=symbol,
                    investment_prompt=investment_prompt,
                    account_id=account_id,
                )
                sr.trade_decision = td  # type: ignore[assignment]
            except Exception as exc:
                sr.error = f"Trade decision failed: {exc}"
                return sr

            return sr
