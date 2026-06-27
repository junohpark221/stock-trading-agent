"""AlgoRiskManager — 8개 정량적 리스크 규칙 기반 매매 검증.

Phase 3 LLM(정성적 평가) 이후, 주문 실행 전에 호출되는 알고리즘 리스크 게이트.
모든 BUY 주문에 대해 8개 하드 제약을 검증하고, 위반 시 수량 조정 또는 차단한다.
SELL/HOLD 액션은 항상 통과 (리스크 감소는 항상 허용).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd
import structlog
from sqlalchemy import select

from src.core.enums import SignalAction
from src.core.models import PortfolioState, RiskCheckResult
from src.db.models.market_data import DailyOHLCV

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.config import Settings
    from src.strategy.portfolio_state import PortfolioStateService

logger = structlog.get_logger(__name__)

_MIN_CORRELATION_DAYS = 20  # 상관계수 계산에 필요한 최소 거래일 수
_CORRELATION_LOOKBACK = 60  # 상관계수 계산 기간 (거래일)


@dataclass
class _RuleResult:
    """개별 규칙 검사 결과."""

    violation: str | None = None       # 위반 규칙명 (None = 통과)
    warning: str | None = None         # 비위반 경고
    max_quantity: int | None = None    # 수량 제약 (None = 제약 없음)


@dataclass
class BatchReservation:
    """한 배치 매수 실행 동안 누적되는 in-flight(접수·미체결) 진입 예약.

    배치 루프(`_execute_buy_decisions`)가 1회 호출 동안 소유하며, 각 진입을 접수할
    때마다 누적된다. 후속 후보의 누적 한도 검증
    (``MAX_DAILY_TRADES`` / ``MAX_PORTFOLIO_POSITIONS`` / ``SECTOR_CONCENTRATION``)에
    이 예약분을 더해, 같은 배치 안에서 한도가 동시 돌파되는 것을 막는다 (F-04).

    배치 경로는 지정가 주문이라 ``execute_entry`` 내에서 동기 체결·포지션 생성이
    없어 DB 카운트와 겹치지 않는다. 예약은 배치 종료와 함께 폐기되고, 다음 배치는
    DB(`PositionRecord`) 카운트가 이어받으므로 이중 카운트가 발생하지 않는다.
    """

    trade_count: int = 0
    new_symbols: set[str] = field(default_factory=set)
    sector_added_value: dict[str, Decimal] = field(default_factory=dict)

    def reserve(
        self,
        symbol: str,
        sector: str,
        value: Decimal,
        *,
        is_new_holding: bool,
    ) -> None:
        """이번 배치의 진입 1건을 예약에 누적한다.

        Parameters
        ----------
        symbol: 진입 종목 코드.
        sector: 종목 섹터 (집중도 누적용; 빈 문자열이면 "기타"로 취급되지 않고
            그대로 키로 사용 — check()의 섹터 조회와 동일 키를 써야 함).
        value: 추가 거래대금 (수량 × 단가).
        is_new_holding: 보유에 없던 신규 종목이면 True (보유 종목 수 누적).
        """
        self.trade_count += 1
        if is_new_holding:
            self.new_symbols.add(symbol)
        self.sector_added_value[sector] = (
            self.sector_added_value.get(sector, Decimal(0)) + value
        )


class AlgoRiskManager:
    """8개 정량적 리스크 규칙으로 BUY 주문을 검증한다.

    규칙 1-4: 수량 조정 가능 (위반 시 허용 범위 내 최대 수량 계산)
    규칙 5-6: 절대 차단 (위반 시 adjusted_quantity = 0)
    규칙 7: 상관계수 경고 (violation 아닌 warning)
    규칙 8: 일일 거래 횟수 차단
    """

    def __init__(
        self,
        portfolio_service: PortfolioStateService,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._portfolio_service = portfolio_service
        self._session_factory = session_factory
        self._settings = settings

    # ── 메인 메서드 ──────────────────────────────────────────────────────

    async def check(
        self,
        symbol: str,
        action: SignalAction,
        quantity: int,
        price: Decimal,
        stop_loss_price: Decimal | None,
        sector: str,
        *,
        account_id: str = "default",
        reservation: BatchReservation | None = None,
    ) -> RiskCheckResult:
        """8개 리스크 규칙을 순회하여 매매 가능 여부를 판정한다.

        SELL/HOLD → 즉시 통과. BUY → 8개 규칙 검증 후 수량 조정.

        ``reservation``이 주어지면 같은 배치에서 이미 접수한 진입(in-flight)을 누적
        반영하여 ``MAX_HOLDINGS`` / ``MAX_DAILY_TRADES`` / ``SECTOR_CONCENTRATION``
        한도를 검증한다 (F-04 배치 동시 돌파 방지). None이면 기존 동작과 동일.
        """
        # SELL/HOLD은 항상 통과 (리스크 감소는 항상 허용)
        if action in (SignalAction.SELL, SignalAction.HOLD):
            return RiskCheckResult(
                passed=True,
                symbol=symbol,
                violations=[],
                warnings=[],
                adjusted_quantity=quantity,
                adjusted_amount_krw=Decimal(quantity) * price,
                max_allowed_quantity=quantity,
                reasoning="SELL/HOLD 액션은 리스크 체크를 면제합니다.",
            )

        # price=0 가드
        if price <= Decimal(0):
            return RiskCheckResult(
                passed=False,
                symbol=symbol,
                violations=["INVALID_PRICE"],
                warnings=[],
                adjusted_quantity=0,
                adjusted_amount_krw=Decimal(0),
                max_allowed_quantity=0,
                reasoning="유효하지 않은 가격입니다 (price ≤ 0).",
            )

        # 포트폴리오 현재 상태 조회
        state = await self._portfolio_service.get_current_state()

        # total_value=0 가드: 비율 기반 규칙 의미 없음
        if state.total_value <= Decimal(0):
            return RiskCheckResult(
                passed=False,
                symbol=symbol,
                violations=["ZERO_PORTFOLIO"],
                warnings=["총자산이 0이어서 비율 기반 리스크 규칙을 적용할 수 없습니다."],
                adjusted_quantity=0,
                adjusted_amount_krw=Decimal(0),
                max_allowed_quantity=0,
                reasoning="총자산이 0원이므로 신규 매수가 불가합니다.",
            )

        # 8개 규칙 실행
        results: list[_RuleResult] = [
            self._check_position_sizing(state, quantity, price, stop_loss_price),
            self._check_max_position(state, symbol, quantity, price),
            self._check_max_holdings(state, symbol, reservation),
            self._check_sector_concentration(
                state, symbol, quantity, price, sector, reservation
            ),
            self._check_max_drawdown(state),
            self._check_daily_loss_limit(state),
            await self._check_correlation(state, symbol),
            self._check_daily_trades(state, reservation),
        ]

        # violations, warnings 수집
        violations = [r.violation for r in results if r.violation]
        warnings = [r.warning for r in results if r.warning]

        # 수량 조정
        qty_caps = [r.max_quantity for r in results if r.max_quantity is not None]
        has_block = any(
            r.violation is not None and r.max_quantity == 0 for r in results
        )

        if has_block:
            adjusted_quantity = 0
        elif qty_caps:
            adjusted_quantity = max(0, min(quantity, min(qty_caps)))
        else:
            adjusted_quantity = quantity

        max_allowed_quantity = min(qty_caps) if qty_caps else quantity
        max_allowed_quantity = max(0, max_allowed_quantity)
        adjusted_amount_krw = Decimal(adjusted_quantity) * price
        passed = len(violations) == 0

        # reasoning 생성
        reasoning = self._build_reasoning(
            passed, violations, warnings, quantity, adjusted_quantity
        )

        logger.info(
            "algo_risk.checked",
            account_id=account_id,
            symbol=symbol,
            passed=passed,
            violations=violations,
            adjusted_qty=adjusted_quantity,
            original_qty=quantity,
        )

        return RiskCheckResult(
            passed=passed,
            symbol=symbol,
            violations=violations,
            warnings=warnings,
            adjusted_quantity=adjusted_quantity,
            adjusted_amount_krw=adjusted_amount_krw,
            max_allowed_quantity=max_allowed_quantity,
            reasoning=reasoning,
        )

    # ── Rule 1: 고정비율법 (Position Sizing) ─────────────────────────────

    def _check_position_sizing(
        self,
        state: PortfolioState,
        quantity: int,
        price: Decimal,
        stop_loss_price: Decimal | None,
    ) -> _RuleResult:
        """1건당 리스크가 총 자산의 RISK_PER_TRADE_PCT를 초과하면 위반."""
        if stop_loss_price is None:
            return _RuleResult(
                warning="손절가 미설정 — 고정비율법 리스크 계산을 건너뜁니다."
            )

        risk_per_share = abs(price - stop_loss_price)
        if risk_per_share == Decimal(0):
            return _RuleResult(
                warning="진입가와 손절가가 동일 — 고정비율법 리스크 계산을 건너뜁니다."
            )

        risk_pct = Decimal(str(self._settings.RISK_PER_TRADE_PCT))
        max_risk = state.total_value * risk_pct / Decimal(100)
        risk_amount = Decimal(quantity) * risk_per_share
        max_qty = int(max_risk / risk_per_share)

        if risk_amount > max_risk:
            return _RuleResult(
                violation="RISK_PER_TRADE",
                max_quantity=max_qty,
            )
        return _RuleResult(max_quantity=max_qty)

    # ── Rule 2: 단일 종목 비중 제한 ──────────────────────────────────────

    def _check_max_position(
        self,
        state: PortfolioState,
        symbol: str,
        quantity: int,
        price: Decimal,
    ) -> _RuleResult:
        """단일 종목 비중이 MAX_POSITION_PCT 또는 하드캡을 초과하면 위반."""
        existing_value = sum(
            p.market_value for p in state.positions if p.symbol == symbol
        )
        new_total = existing_value + Decimal(quantity) * price

        pct_limit = state.total_value * Decimal(str(self._settings.MAX_POSITION_PCT)) / Decimal(100)
        hard_cap = Decimal(self._settings.MAX_POSITION_SIZE_KRW)
        effective_limit = min(pct_limit, hard_cap)

        remaining = effective_limit - existing_value
        max_qty = 0 if remaining <= Decimal(0) else int(remaining / price)

        if new_total > effective_limit:
            return _RuleResult(
                violation="MAX_POSITION",
                max_quantity=max_qty,
            )
        return _RuleResult(max_quantity=max_qty)

    # ── Rule 3: 최대 보유 종목 수 ────────────────────────────────────────

    def _check_max_holdings(
        self,
        state: PortfolioState,
        symbol: str,
        reservation: BatchReservation | None = None,
    ) -> _RuleResult:
        """보유 종목 수가 MAX_PORTFOLIO_POSITIONS 이상이면 신규 종목 진입 차단."""
        current_symbols = {p.symbol for p in state.positions}

        # 같은 배치에서 이미 접수한 신규 종목도 보유로 누적 (F-04)
        if reservation is not None:
            current_symbols = current_symbols | reservation.new_symbols

        # 기존 종목(또는 이번 배치 신규 종목) 추가매수는 허용
        if symbol in current_symbols:
            return _RuleResult()

        if len(current_symbols) >= self._settings.MAX_PORTFOLIO_POSITIONS:
            return _RuleResult(
                violation="MAX_HOLDINGS",
                max_quantity=0,
            )
        return _RuleResult()

    # ── Rule 4: 섹터 집중도 ──────────────────────────────────────────────

    def _check_sector_concentration(
        self,
        state: PortfolioState,
        symbol: str,
        quantity: int,
        price: Decimal,
        sector: str,
        reservation: BatchReservation | None = None,
    ) -> _RuleResult:
        """동일 섹터 비중 합산이 SECTOR_CONCENTRATION_PCT를 초과하면 위반."""
        current_pct = state.sector_allocations.get(sector, Decimal(0))

        # 같은 배치에서 이미 접수한 동일 섹터 거래대금을 비중에 누적 (F-04)
        if reservation is not None:
            reserved_value = reservation.sector_added_value.get(sector, Decimal(0))
            if reserved_value > Decimal(0):
                current_pct += reserved_value / state.total_value * Decimal(100)

        additional_pct = (Decimal(quantity) * price) / state.total_value * Decimal(100)
        new_pct = current_pct + additional_pct

        limit = Decimal(str(self._settings.SECTOR_CONCENTRATION_PCT))

        allowed_pct = limit - current_pct
        if allowed_pct <= Decimal(0):
            max_qty = 0
        else:
            max_qty = int(allowed_pct / Decimal(100) * state.total_value / price)

        if new_pct > limit:
            return _RuleResult(
                violation="SECTOR_CONCENTRATION",
                max_quantity=max_qty,
            )
        return _RuleResult(max_quantity=max_qty)

    # ── Rule 5: 최대 낙폭 (ABSOLUTE BLOCK) ──────────────────────────────

    def _check_max_drawdown(self, state: PortfolioState) -> _RuleResult:
        """포트폴리오 낙폭이 MAX_DRAWDOWN_PCT 이상이면 전체 매매 중단."""
        limit = Decimal(str(self._settings.MAX_DRAWDOWN_PCT))
        if state.drawdown_pct >= limit:
            return _RuleResult(
                violation="MAX_DRAWDOWN",
                max_quantity=0,
            )
        return _RuleResult()

    # ── Rule 6: 일일 손실 제한 (ABSOLUTE BLOCK) ──────────────────────────

    def _check_daily_loss_limit(self, state: PortfolioState) -> _RuleResult:
        """일일 손실이 비율 또는 금액 한도를 초과하면 매매 차단."""
        pct_limit = Decimal(str(self._settings.DAILY_LOSS_LIMIT_PCT))
        krw_limit = Decimal(self._settings.DAILY_LOSS_LIMIT_KRW)

        violations: list[str] = []

        # daily_pnl_pct가 음수일 때 손실 — -3.0 이하면 위반
        if state.daily_pnl_pct < -pct_limit:
            violations.append("DAILY_LOSS_PCT")

        # daily_pnl 금액이 음수일 때 손실 — -500,000 이하면 위반
        if state.daily_pnl < -krw_limit:
            violations.append("DAILY_LOSS_KRW")

        if violations:
            # 두 위반 모두 하나의 _RuleResult로 합침
            return _RuleResult(
                violation="DAILY_LOSS_LIMIT",
                max_quantity=0,
            )
        return _RuleResult()

    # ── Rule 7: 상관계수 (WARNING ONLY) ──────────────────────────────────

    async def _check_correlation(
        self,
        state: PortfolioState,
        symbol: str,
    ) -> _RuleResult:
        """기존 보유 종목과의 상관계수가 임계치 이상이면 경고."""
        if not state.positions:
            return _RuleResult()

        holding_symbols = {p.symbol for p in state.positions}

        # 자기 자신은 제외 (추가매수 시)
        holding_symbols.discard(symbol)
        if not holding_symbols:
            return _RuleResult()

        threshold = self._settings.CORRELATION_THRESHOLD

        # 후보 종목의 수익률 시리즈
        candidate_returns = await self._get_daily_returns(symbol)
        if candidate_returns is None or len(candidate_returns) < _MIN_CORRELATION_DAYS:
            n = len(candidate_returns) if candidate_returns is not None else 0
            return _RuleResult(
                warning=f"{symbol} OHLCV 데이터 부족 ({n}일) — 상관계수 미산출."
            )

        high_corr_pairs: list[str] = []
        for h_sym in holding_symbols:
            h_returns = await self._get_daily_returns(h_sym)
            if h_returns is None or len(h_returns) < _MIN_CORRELATION_DAYS:
                continue

            corr = self._pearson_correlation(candidate_returns, h_returns)
            if corr is not None and corr >= threshold:
                high_corr_pairs.append(f"{h_sym}({corr:.2f})")

        if high_corr_pairs:
            return _RuleResult(
                warning=f"높은 상관관계 종목: {', '.join(high_corr_pairs)}."
            )
        return _RuleResult()

    # ── Rule 8: 일일 거래 횟수 ───────────────────────────────────────────

    def _check_daily_trades(
        self,
        state: PortfolioState,
        reservation: BatchReservation | None = None,
    ) -> _RuleResult:
        """당일 거래 횟수가 MAX_DAILY_TRADES 이상이면 차단."""
        count = state.daily_trade_count
        # 같은 배치에서 이미 접수한 진입 건수를 누적 (F-04)
        if reservation is not None:
            count += reservation.trade_count

        if count >= self._settings.MAX_DAILY_TRADES:
            return _RuleResult(
                violation="MAX_DAILY_TRADES",
                max_quantity=0,
            )
        return _RuleResult()

    # ── 헬퍼 메서드 ─────────────────────────────────────────────────────

    async def _get_daily_returns(
        self, symbol: str, days: int = _CORRELATION_LOOKBACK
    ) -> pd.Series | None:
        """DailyOHLCV에서 close를 조회하여 일간 수익률 Series를 반환한다."""
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(DailyOHLCV.date, DailyOHLCV.close)
                    .where(DailyOHLCV.symbol == symbol)
                    .order_by(DailyOHLCV.date.desc())
                    .limit(days + 1)
                )
                rows = (await session.execute(stmt)).all()
        except Exception:
            logger.warning("algo_risk.ohlcv_query_failed", symbol=symbol)
            return None

        if len(rows) < 2:
            return None

        # oldest first
        rows = list(reversed(rows))
        dates = [r[0] for r in rows]
        closes = [float(r[1]) for r in rows]

        series = pd.Series(closes, index=dates)
        return series.pct_change().dropna()

    @staticmethod
    def _pearson_correlation(s1: pd.Series, s2: pd.Series) -> float | None:
        """두 수익률 시리즈의 Pearson 상관계수. 공통 인덱스 < 20이면 None."""
        # 공통 날짜만 사용
        common = s1.index.intersection(s2.index)
        if len(common) < _MIN_CORRELATION_DAYS:
            return None

        corr = s1.loc[common].corr(s2.loc[common])
        if math.isnan(corr):
            return None
        return float(corr)

    @staticmethod
    def _build_reasoning(
        passed: bool,
        violations: list[str],
        warnings: list[str],
        original_qty: int,
        adjusted_qty: int,
    ) -> str:
        """검사 결과를 사람이 읽을 수 있는 문장으로 요약한다."""
        parts: list[str] = []

        if passed:
            parts.append("모든 리스크 규칙을 통과했습니다.")
        else:
            parts.append(f"위반 규칙: {', '.join(violations)}.")

        if warnings:
            parts.append(f"경고: {'; '.join(warnings)}.")

        if adjusted_qty != original_qty:
            parts.append(
                f"수량 조정: {original_qty}주 → {adjusted_qty}주."
            )

        return " ".join(parts)
