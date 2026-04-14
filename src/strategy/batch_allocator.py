"""BatchBudgetAllocator — 다중 매수 후보의 예산 배분 레이어.

전략이 한 배치에서 여러 BUY `TradeDecision`을 생성할 때, 각 후보의 `quantity`는
`PositionSizer`가 독립적으로 `total_portfolio_value` 기준으로 사전 계산한다.
이 때문에 N개 후보의 합계가 실제 가용 현금을 크게 초과할 수 있다.

이 allocator는 승인 루프 직전에 삽입되어 다음을 수행한다:
    1. 후보별 점수 계산 (confidence + risk_reward 가중 합성)
    2. 점수 내림차순 정렬 + Top-N 선별
    3. 가용 현금 기반 예산을 점수 비례로 분배
    4. MAX_POSITION_PCT / MAX_POSITION_SIZE_KRW 상한 적용
    5. 각 후보 `quantity` 재산정 (floor(allocated / price))
    6. 최소 할당액 미만 드랍

결과: 조정된 `TradeDecision` 리스트 (합계 ≤ batch_budget).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from src.core.enums import DecisionAction, DecisionStage

if TYPE_CHECKING:
    from src.agent.decision_recorder import DecisionRecorder
    from src.config import Settings
    from src.core.models import TradeDecision
    from src.strategy.portfolio_state import PortfolioStateService

logger = structlog.get_logger(__name__)

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class _ScoredCandidate:
    """점수와 함께 정렬/필터 단계에서 사용되는 내부 튜플."""

    td: TradeDecision
    score: Decimal


class BatchBudgetAllocator:
    """배치 예산을 점수 비례로 여러 BUY 후보에 배분한다.

    Stateless — 각 호출은 독립적이며, 다중 계좌 동시 호출 안전.
    """

    def __init__(
        self,
        settings: Settings,
        portfolio_service: PortfolioStateService,
        recorder: DecisionRecorder | None = None,
    ) -> None:
        self._settings = settings
        self._portfolio_service = portfolio_service
        self._recorder = recorder

    # ── Public API ────────────────────────────────────────────────────

    async def allocate(
        self,
        buy_decisions: list[TradeDecision],
        *,
        account_id: str = "default",
        session_id: UUID | None = None,
    ) -> list[TradeDecision]:
        """BUY 후보 리스트에 배치 예산을 배분하여 조정된 리스트 반환.

        Parameters
        ----------
        buy_decisions: 전략이 생성한 BUY `TradeDecision` 리스트.
        account_id: 계좌 ID (로깅 / decision_log용).
        session_id: 파이프라인 세션 ID (decision_log 부모 연결용).

        Returns
        -------
        수량이 조정된 `TradeDecision` 리스트. 드랍된 후보는 제외된다.
        빈 입력 / 예산 부족 시 빈 리스트.
        """
        if not buy_decisions:
            return []

        state = await self._portfolio_service.get_current_state()
        total_pv = state.total_value

        # ── 1. 가용 현금 조회 (주문가능현금, 미수 제외) ─────────────
        # state.cash(dnca_tot_amt)는 D+2 정산 전 당일 매수분을 차감하지 않아
        # 미수 방지용으로 신뢰할 수 없다. 브로커의 주문가능현금 API를 사용.
        # KIS는 symbol+price 필수이므로 첫 후보로 질의 → 계좌 레벨 근사값.
        first_candidate = buy_decisions[0]
        probe_price = first_candidate.price or Decimal(0)
        buyable_cash = await self._portfolio_service.fetch_buyable_cash(
            first_candidate.symbol, probe_price
        )
        # 폴백: 브로커가 0을 반환하면 보수적으로 state.cash 사용하지 말고 0 처리.
        # (이 allocator 위에 있는 OrderExecutor cash gate가 최종 방어선.)
        cash = buyable_cash

        # ── 2. 배치 예산 계산 ────────────────────────────────────────
        batch_budget = self._compute_batch_budget(cash)
        min_alloc = Decimal(self._settings.BATCH_MIN_ALLOCATION_KRW)

        if batch_budget < min_alloc:
            logger.warning(
                "batch_allocator.budget_below_min",
                account_id=account_id,
                cash=str(cash),
                buyable_cash=str(buyable_cash),
                state_cash=str(state.cash),
                batch_budget=str(batch_budget),
                min_allocation=str(min_alloc),
                candidate_count=len(buy_decisions),
            )
            await self._record_batch_decision(
                session_id=session_id,
                account_id=account_id,
                decision=DecisionAction.REJECT,
                reasoning=(
                    f"배치 예산({batch_budget}) < 최소 할당액({min_alloc}). "
                    f"후보 {len(buy_decisions)}개 전부 드랍."
                ),
                data={
                    "cash": str(cash),
                    "batch_budget": str(batch_budget),
                    "min_allocation": str(min_alloc),
                    "dropped_count": len(buy_decisions),
                    "reason": "batch_budget_below_min",
                },
            )
            return []

        # ── 2. 점수 계산 + 정렬 ─────────────────────────────────────
        scored = [
            _ScoredCandidate(td=td, score=self._score(td))
            for td in buy_decisions
        ]
        # 내림차순 점수, 동점은 symbol 오름차순 (안정적 결정론)
        scored.sort(key=lambda c: (-c.score, c.td.symbol))

        # ── 3. Top-N 컷 ──────────────────────────────────────────────
        top_n = max(1, self._settings.BATCH_TOP_N_CANDIDATES)
        selected = scored[:top_n]
        dropped_below_n = scored[top_n:]

        for rank_idx, cand in enumerate(dropped_below_n, start=top_n + 1):
            logger.info(
                "batch_allocator.decision",
                account_id=account_id,
                symbol=cand.td.symbol,
                rank=rank_idx,
                score=str(cand.score),
                original_qty=cand.td.quantity,
                allocated_qty=0,
                reason="below_top_n",
            )

        # ── 4. 점수 합계로 가중치 산출 ──────────────────────────────
        score_sum = sum((c.score for c in selected), start=_ZERO)
        if score_sum <= _ZERO:
            # 모든 점수가 0인 극단 케이스: 균등 분배로 폴백
            score_sum = Decimal(len(selected))
            weights = [Decimal(1) / score_sum for _ in selected]
        else:
            weights = [c.score / score_sum for c in selected]

        # ── 5. 상한 계산 (포지션 비중 / 절대 상한) ──────────────────
        pos_pct_cap = (
            total_pv * Decimal(str(self._settings.MAX_POSITION_PCT)) / _HUNDRED
            if total_pv > _ZERO
            else Decimal("Infinity")
        )
        pos_krw_cap = (
            Decimal(self._settings.MAX_POSITION_SIZE_KRW)
            if self._settings.MAX_POSITION_SIZE_KRW > 0
            else Decimal("Infinity")
        )

        # ── 6. 각 후보 재사이징 ─────────────────────────────────────
        result: list[TradeDecision] = []
        for rank_idx, (cand, weight) in enumerate(
            zip(selected, weights, strict=True), start=1,
        ):
            td = cand.td
            raw_alloc = batch_budget * weight
            allocated = min(raw_alloc, pos_pct_cap, pos_krw_cap)

            price = td.price or _ZERO
            if price <= _ZERO:
                logger.warning(
                    "batch_allocator.decision",
                    account_id=account_id,
                    symbol=td.symbol,
                    rank=rank_idx,
                    score=str(cand.score),
                    original_qty=td.quantity,
                    allocated_qty=0,
                    reason="invalid_price",
                )
                continue

            qty = int(allocated / price)
            cost = Decimal(qty) * price

            if qty < 1 or cost < min_alloc:
                logger.info(
                    "batch_allocator.decision",
                    account_id=account_id,
                    symbol=td.symbol,
                    rank=rank_idx,
                    score=str(cand.score),
                    original_qty=td.quantity,
                    allocated_qty=qty,
                    allocated_krw=str(cost),
                    reason="below_min_allocation",
                )
                continue

            adjusted = td.model_copy(update={"quantity": qty})
            result.append(adjusted)

            logger.info(
                "batch_allocator.decision",
                account_id=account_id,
                symbol=td.symbol,
                rank=rank_idx,
                score=str(cand.score),
                original_qty=td.quantity,
                allocated_qty=qty,
                allocated_krw=str(cost),
                weight=str(weight),
                reason="allocated",
            )

        # ── 7. 배치 전체 결정 기록 ───────────────────────────────────
        total_allocated_krw = sum(
            (Decimal(td.quantity) * (td.price or _ZERO) for td in result),
            start=_ZERO,
        )
        await self._record_batch_decision(
            session_id=session_id,
            account_id=account_id,
            decision=DecisionAction.APPROVE if result else DecisionAction.REJECT,
            reasoning=(
                f"후보 {len(buy_decisions)}개 → Top-{top_n} → {len(result)}개 배분. "
                f"예산={batch_budget}, 사용={total_allocated_krw}."
            ),
            data={
                "cash": str(cash),
                "batch_budget": str(batch_budget),
                "candidate_count": len(buy_decisions),
                "top_n": top_n,
                "allocated_count": len(result),
                "total_allocated_krw": str(total_allocated_krw),
                "allocations": [
                    {
                        "symbol": td.symbol,
                        "quantity": td.quantity,
                        "price": str(td.price or _ZERO),
                    }
                    for td in result
                ],
            },
        )

        return result

    # ── Internal helpers ──────────────────────────────────────────────

    def _compute_batch_budget(self, cash: Decimal) -> Decimal:
        """가용 현금 × BATCH_BUDGET_PCT, 필요 시 BATCH_BUDGET_MAX_KRW로 캡."""
        if cash <= _ZERO:
            return _ZERO

        pct = Decimal(str(self._settings.BATCH_BUDGET_PCT))
        budget = cash * pct / _HUNDRED

        if self._settings.BATCH_BUDGET_MAX_KRW > 0:
            budget = min(budget, Decimal(self._settings.BATCH_BUDGET_MAX_KRW))

        return budget

    def _score(self, td: TradeDecision) -> Decimal:
        """confidence × W_CONF + normalize(rr) × W_RR 점수 공식."""
        w_conf = Decimal(str(self._settings.BATCH_SCORE_W_CONFIDENCE))
        w_rr = Decimal(str(self._settings.BATCH_SCORE_W_RR))
        rr_cap = Decimal(str(self._settings.BATCH_RR_CAP))

        confidence = td.confidence if td.confidence is not None else _ZERO
        rr = td.risk_reward_ratio if td.risk_reward_ratio is not None else _ZERO
        if rr < _ZERO:
            rr = _ZERO

        rr_normalized = min(rr, rr_cap) / rr_cap if rr_cap > _ZERO else _ZERO

        return w_conf * confidence + w_rr * rr_normalized

    async def _record_batch_decision(
        self,
        *,
        session_id: UUID | None,
        account_id: str,
        decision: DecisionAction,
        reasoning: str,
        data: dict,
    ) -> None:
        """decision_log에 배치 배분 결정 기록 (recorder/session_id 없으면 스킵)."""
        if self._recorder is None or session_id is None:
            return
        try:
            await self._recorder.record(
                session_id=session_id,
                stage=DecisionStage.BATCH_ALLOCATION.value,
                decision=decision.value,
                reasoning=reasoning,
                account_id=account_id,
                data_snapshot=data,
            )
        except Exception:
            logger.warning(
                "batch_allocator.record_failed",
                account_id=account_id,
                exc_info=True,
            )
