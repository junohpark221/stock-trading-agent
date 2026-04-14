"""BatchBudgetAllocator 단위 테스트.

Scoring / Top-N cut / 점수 비례 분배 / 상한 / 최소 할당 / 엣지 케이스.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.core.enums import DecisionAction
from src.core.models import PortfolioState, TradeDecision
from src.strategy.batch_allocator import BatchBudgetAllocator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides: object) -> MagicMock:
    s = MagicMock()
    s.BATCH_BUDGET_PCT = 100.0
    s.BATCH_BUDGET_MAX_KRW = 0
    s.BATCH_TOP_N_CANDIDATES = 5
    s.BATCH_MIN_ALLOCATION_KRW = 500_000
    s.BATCH_SCORE_W_CONFIDENCE = 0.7
    s.BATCH_SCORE_W_RR = 0.3
    s.BATCH_RR_CAP = 3.0
    s.MAX_POSITION_PCT = 100.0
    s.MAX_POSITION_SIZE_KRW = 0
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_portfolio_state(cash: Decimal, total_value: Decimal | None = None) -> PortfolioState:
    tv = total_value if total_value is not None else cash
    return PortfolioState(
        account_id="default",
        total_value=tv,
        cash=cash,
        invested=tv - cash,
        unrealized_pnl=Decimal(0),
        daily_pnl=Decimal(0),
        daily_pnl_pct=Decimal(0),
        drawdown_pct=Decimal(0),
        peak_value=tv,
        positions=[],
        sector_allocations={},
        daily_trade_count=0,
        timestamp=datetime.now(UTC),
    )


def _make_portfolio_service(cash: Decimal, total_value: Decimal | None = None) -> MagicMock:
    svc = MagicMock()
    svc.get_current_state = AsyncMock(
        return_value=_make_portfolio_state(cash, total_value),
    )
    return svc


def _td(
    symbol: str,
    *,
    price: Decimal,
    confidence: Decimal = Decimal("0.5"),
    rr: Decimal | None = Decimal("2.0"),
    quantity: int = 100,
    sl: Decimal | None = None,
    tp: Decimal | None = None,
    reasoning: str = "test",
) -> TradeDecision:
    return TradeDecision(
        symbol=symbol,
        action=DecisionAction.BUY,
        confidence=confidence,
        quantity=quantity,
        price=price,
        stop_loss_price=sl or (price * Decimal("0.95")),
        take_profit_price=tp or (price * Decimal("1.10")),
        risk_reward_ratio=rr,
        reasoning=reasoning,
    )


def _make_allocator(
    cash: Decimal,
    total_value: Decimal | None = None,
    **s_overrides,
) -> BatchBudgetAllocator:
    return BatchBudgetAllocator(
        settings=_make_settings(**s_overrides),
        portfolio_service=_make_portfolio_service(cash, total_value),
    )


# ---------------------------------------------------------------------------
# 기본 케이스
# ---------------------------------------------------------------------------


class TestEmptyAndSingle:
    @pytest.mark.asyncio
    async def test_empty_list(self):
        alloc = _make_allocator(Decimal("10_000_000"))
        result = await alloc.allocate([])
        assert result == []

    @pytest.mark.asyncio
    async def test_single_candidate_within_budget(self):
        alloc = _make_allocator(Decimal("10_000_000"))
        td = _td("005930", price=Decimal("50000"), confidence=Decimal("0.9"))
        result = await alloc.allocate([td])
        assert len(result) == 1
        # batch_budget = 10M, Top-1 → 전부 할당 (10M / 50000 = 200주)
        assert result[0].quantity == 200
        assert result[0].symbol == "005930"

    @pytest.mark.asyncio
    async def test_single_candidate_price_exceeds_cash(self):
        alloc = _make_allocator(Decimal("30_000"))  # cash 3만
        td = _td("005930", price=Decimal("50000"), confidence=Decimal("0.9"))
        # batch_budget = 30000 < MIN_ALLOCATION_KRW=500_000 → 전체 드랍
        result = await alloc.allocate([td])
        assert result == []


# ---------------------------------------------------------------------------
# 예산 내 다중 후보
# ---------------------------------------------------------------------------


class TestMultipleWithinBudget:
    @pytest.mark.asyncio
    async def test_three_candidates_fit(self):
        """3개 후보, 합이 예산 내 → 전부 유지, 점수 비례 수량."""
        alloc = _make_allocator(Decimal("30_000_000"))
        tds = [
            _td("AAA", price=Decimal("10000"), confidence=Decimal("0.9")),
            _td("BBB", price=Decimal("10000"), confidence=Decimal("0.6")),
            _td("CCC", price=Decimal("10000"), confidence=Decimal("0.3")),
        ]
        result = await alloc.allocate(tds)
        # 3개 전부 유지, symbol 내림차순 점수
        symbols = {td.symbol for td in result}
        assert symbols == {"AAA", "BBB", "CCC"}
        total_cost = sum(Decimal(td.quantity) * td.price for td in result)
        assert total_cost <= Decimal("30_000_000")
        # AAA가 가장 큰 할당
        by_sym = {td.symbol: td.quantity for td in result}
        assert by_sym["AAA"] > by_sym["BBB"] > by_sym["CCC"]


# ---------------------------------------------------------------------------
# 예산 초과 / Top-N cut
# ---------------------------------------------------------------------------


class TestTopNCut:
    @pytest.mark.asyncio
    async def test_five_candidates_sum_le_budget(self):
        """5개 후보 Top-N=5, 합계 ≤ batch_budget."""
        alloc = _make_allocator(Decimal("10_000_000"))
        tds = [
            _td(f"SYM{i}", price=Decimal("50000"), confidence=Decimal(f"0.{5 + i}"))
            for i in range(5)
        ]
        result = await alloc.allocate(tds)
        total_cost = sum(Decimal(td.quantity) * td.price for td in result)
        assert total_cost <= Decimal("10_000_000")
        assert len(result) <= 5

    @pytest.mark.asyncio
    async def test_seven_candidates_top_three(self):
        """7개 후보, Top-N=3 → 하위 4개 드랍."""
        alloc = _make_allocator(
            Decimal("30_000_000"),
            BATCH_TOP_N_CANDIDATES=3,
        )
        tds = [
            _td(f"S{i:02d}", price=Decimal("10000"), confidence=Decimal("0.1") * i)
            for i in range(1, 8)
        ]
        result = await alloc.allocate(tds)
        # 상위 3개(S07=0.7, S06=0.6, S05=0.5)만 배분됨
        symbols = {td.symbol for td in result}
        assert symbols == {"S07", "S06", "S05"}


# ---------------------------------------------------------------------------
# 정렬 안정성
# ---------------------------------------------------------------------------


class TestSortStability:
    @pytest.mark.asyncio
    async def test_score_tie_breaks_by_symbol(self):
        """동일 점수 → symbol 오름차순 정렬."""
        alloc = _make_allocator(
            Decimal("20_000_000"),
            BATCH_TOP_N_CANDIDATES=2,
        )
        # 전부 같은 confidence + rr → 동점
        tds = [
            _td("CCC", price=Decimal("10000"), confidence=Decimal("0.5"), rr=Decimal("2.0")),
            _td("AAA", price=Decimal("10000"), confidence=Decimal("0.5"), rr=Decimal("2.0")),
            _td("BBB", price=Decimal("10000"), confidence=Decimal("0.5"), rr=Decimal("2.0")),
        ]
        result = await alloc.allocate(tds)
        # Top-2: AAA, BBB (symbol 오름차순)
        symbols = {td.symbol for td in result}
        assert symbols == {"AAA", "BBB"}


# ---------------------------------------------------------------------------
# 최소 할당 미만 드랍
# ---------------------------------------------------------------------------


class TestMinAllocation:
    @pytest.mark.asyncio
    async def test_tiny_slice_dropped(self):
        """낮은 점수 후보가 최소 할당액 미만 → 드랍."""
        alloc = _make_allocator(
            Decimal("2_000_000"),
            BATCH_MIN_ALLOCATION_KRW=500_000,
        )
        # 점수 격차가 크도록 설정: 높은 confidence가 대부분 가져감
        tds = [
            _td("BIG", price=Decimal("10000"), confidence=Decimal("0.99"), rr=Decimal("3.0")),
            _td("MED", price=Decimal("10000"), confidence=Decimal("0.5"), rr=Decimal("2.0")),
            # TINY는 점수 매우 낮음 → 할당액이 500k 미만
            _td("TINY", price=Decimal("10000"), confidence=Decimal("0.01"), rr=Decimal("0.1")),
        ]
        result = await alloc.allocate(tds)
        symbols = {td.symbol for td in result}
        assert "BIG" in symbols
        assert "TINY" not in symbols

    @pytest.mark.asyncio
    async def test_budget_below_min_drops_all(self):
        """cash < MIN_ALLOCATION → 전체 드랍."""
        alloc = _make_allocator(
            Decimal("100_000"),  # cash 10만 < 50만
            BATCH_MIN_ALLOCATION_KRW=500_000,
        )
        tds = [_td("AAA", price=Decimal("10000"))]
        result = await alloc.allocate(tds)
        assert result == []


# ---------------------------------------------------------------------------
# 예산 캡 조합
# ---------------------------------------------------------------------------


class TestBudgetCaps:
    @pytest.mark.asyncio
    async def test_batch_budget_pct_and_max_krw(self):
        """BATCH_BUDGET_PCT=50 + BATCH_BUDGET_MAX_KRW=2_000_000 → min() 적용."""
        alloc = _make_allocator(
            Decimal("10_000_000"),
            BATCH_BUDGET_PCT=50.0,   # 5M
            BATCH_BUDGET_MAX_KRW=2_000_000,  # 2M
        )
        td = _td("AAA", price=Decimal("10000"), confidence=Decimal("0.9"))
        result = await alloc.allocate([td])
        # min(5M, 2M) = 2M → 200주
        assert result[0].quantity == 200


# ---------------------------------------------------------------------------
# 포지션 상한
# ---------------------------------------------------------------------------


class TestPositionCaps:
    @pytest.mark.asyncio
    async def test_max_position_size_krw_cap(self):
        """MAX_POSITION_SIZE_KRW 캡이 점수 비례 분배보다 타이트."""
        alloc = _make_allocator(
            Decimal("10_000_000"),
            MAX_POSITION_SIZE_KRW=1_000_000,  # 1종목당 1M 상한
        )
        tds = [
            _td(f"S{i}", price=Decimal("10000"), confidence=Decimal("0.8"))
            for i in range(3)
        ]
        result = await alloc.allocate(tds)
        # 각 후보 최대 1M = 100주
        for td in result:
            assert td.quantity <= 100


# ---------------------------------------------------------------------------
# 필드 보존
# ---------------------------------------------------------------------------


class TestFieldPreservation:
    @pytest.mark.asyncio
    async def test_sl_tp_rr_reasoning_preserved(self):
        """재사이징 후에도 stop_loss / take_profit / rr / reasoning 보존."""
        alloc = _make_allocator(Decimal("10_000_000"))
        td = _td(
            "AAA",
            price=Decimal("50000"),
            sl=Decimal("48000"),
            tp=Decimal("55000"),
            rr=Decimal("2.5"),
            reasoning="custom reason",
        )
        result = await alloc.allocate([td])
        assert len(result) == 1
        assert result[0].stop_loss_price == Decimal("48000")
        assert result[0].take_profit_price == Decimal("55000")
        assert result[0].risk_reward_ratio == Decimal("2.5")
        assert result[0].reasoning == "custom reason"
        assert result[0].action == DecisionAction.BUY


# ---------------------------------------------------------------------------
# 점수 공식
# ---------------------------------------------------------------------------


class TestScoring:
    def test_score_formula(self):
        """0.7*confidence + 0.3*(rr/rr_cap) 공식 검증."""
        alloc = BatchBudgetAllocator(
            settings=_make_settings(),
            portfolio_service=MagicMock(),
        )
        td = _td("AAA", price=Decimal("10000"), confidence=Decimal("0.9"), rr=Decimal("2.0"))
        score = alloc._score(td)
        # 0.7 * 0.9 + 0.3 * (2.0 / 3.0) = 0.63 + 0.2 = 0.83
        expected = (
            Decimal("0.7") * Decimal("0.9")
            + Decimal("0.3") * (Decimal("2.0") / Decimal("3.0"))
        )
        assert abs(score - expected) < Decimal("0.0001")

    def test_score_rr_none(self):
        """rr=None이면 rr_normalized=0, confidence만 반영."""
        alloc = BatchBudgetAllocator(
            settings=_make_settings(),
            portfolio_service=MagicMock(),
        )
        td = _td("AAA", price=Decimal("10000"), confidence=Decimal("0.8"), rr=None)
        score = alloc._score(td)
        # 0.7 * 0.8 + 0.3 * 0 = 0.56
        assert abs(score - Decimal("0.56")) < Decimal("0.0001")

    def test_score_rr_capped(self):
        """RR이 cap(3.0)을 넘으면 1.0으로 포화."""
        alloc = BatchBudgetAllocator(
            settings=_make_settings(),
            portfolio_service=MagicMock(),
        )
        td = _td("AAA", price=Decimal("10000"), confidence=Decimal("0.5"), rr=Decimal("10.0"))
        score = alloc._score(td)
        # 0.7 * 0.5 + 0.3 * 1.0 = 0.65
        assert abs(score - Decimal("0.65")) < Decimal("0.0001")


# ---------------------------------------------------------------------------
# Recorder 통합
# ---------------------------------------------------------------------------


class TestRecorderIntegration:
    @pytest.mark.asyncio
    async def test_recorder_called_on_successful_allocation(self):
        recorder = MagicMock()
        recorder.record = AsyncMock(return_value=uuid4())
        alloc = BatchBudgetAllocator(
            settings=_make_settings(),
            portfolio_service=_make_portfolio_service(Decimal("10_000_000")),
            recorder=recorder,
        )
        td = _td("AAA", price=Decimal("10000"), confidence=Decimal("0.8"))
        sid = uuid4()
        await alloc.allocate([td], session_id=sid)
        recorder.record.assert_awaited_once()
        call = recorder.record.call_args
        assert call.kwargs["session_id"] == sid
        assert call.kwargs["stage"] == "batch_allocation"

    @pytest.mark.asyncio
    async def test_recorder_skipped_without_session_id(self):
        recorder = MagicMock()
        recorder.record = AsyncMock()
        alloc = BatchBudgetAllocator(
            settings=_make_settings(),
            portfolio_service=_make_portfolio_service(Decimal("10_000_000")),
            recorder=recorder,
        )
        td = _td("AAA", price=Decimal("10000"))
        await alloc.allocate([td], session_id=None)
        recorder.record.assert_not_awaited()
