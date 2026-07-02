"""Phase 4 Step 4: AlgoRiskManager 단위 테스트.

8개 리스크 규칙 × (통과/위반/경계값) + 복합 시나리오 + edge case = 27 테스트.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from src.core.enums import PositionStatus, SignalAction
from src.core.models import PortfolioState, Position
from src.strategy.risk_manager import AlgoRiskManager, BatchReservation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_position(symbol: str = "005930", **overrides: object) -> Position:
    defaults = {
        "symbol": symbol,
        "quantity": 100,
        "average_cost": Decimal("70000"),
        "current_price": Decimal("72000"),
        "market_value": Decimal("7200000"),
        "unrealized_pnl": Decimal("200000"),
        "unrealized_pnl_pct": Decimal("2.86"),
        "status": PositionStatus.OPEN,
        "entry_date": datetime(2026, 3, 20, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Position(**defaults)  # type: ignore[arg-type]


def _make_state(**overrides: object) -> PortfolioState:
    """제어된 PortfolioState 생성."""
    defaults: dict = {
        "total_value": Decimal("100000000"),  # 1억
        "cash": Decimal("50000000"),
        "invested": Decimal("50000000"),
        "unrealized_pnl": Decimal("1000000"),
        "daily_pnl": Decimal("200000"),
        "daily_pnl_pct": Decimal("0.20"),
        "drawdown_pct": Decimal("2.0"),
        "peak_value": Decimal("102000000"),
        "positions": [],
        "sector_allocations": {},
        "daily_trade_count": 0,
        "timestamp": datetime(2026, 3, 22, 9, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return PortfolioState(**defaults)  # type: ignore[arg-type]


def _make_settings(**overrides: object) -> MagicMock:
    """Settings mock 생성."""
    s = MagicMock()
    s.RISK_PER_TRADE_PCT = 2.0
    s.MAX_POSITION_PCT = 10.0
    s.MAX_POSITION_SIZE_KRW = 1_000_000
    s.MAX_PORTFOLIO_POSITIONS = 5
    s.SECTOR_CONCENTRATION_PCT = 30.0
    s.MAX_DRAWDOWN_PCT = 10.0
    s.DAILY_LOSS_LIMIT_PCT = 3.0
    s.DAILY_LOSS_LIMIT_KRW = 500_000
    s.CORRELATION_THRESHOLD = 0.7
    s.MAX_DAILY_TRADES = 5
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_manager(
    state: PortfolioState | None = None,
    settings_overrides: dict | None = None,
) -> AlgoRiskManager:
    """mock 기반 AlgoRiskManager 생성."""
    portfolio_service = AsyncMock()
    portfolio_service.get_current_state = AsyncMock(
        return_value=state or _make_state()
    )

    session_factory = AsyncMock()
    settings = _make_settings(**(settings_overrides or {}))

    return AlgoRiskManager(
        portfolio_service=portfolio_service,
        session_factory=session_factory,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# SELL / HOLD pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_always_passes():
    mgr = _make_manager()
    result = await mgr.check(
        "005930", SignalAction.SELL, 100, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert result.passed is True
    assert result.violations == []
    assert result.adjusted_quantity == 100


@pytest.mark.asyncio
async def test_hold_always_passes():
    mgr = _make_manager()
    result = await mgr.check(
        "005930", SignalAction.HOLD, 50, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert result.passed is True
    assert result.adjusted_quantity == 50


# ---------------------------------------------------------------------------
# Rule 1: Position Sizing (고정비율법)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_position_sizing_pass():
    """리스크가 한도 내일 때 통과."""
    # total=100M, 2% = 2M risk budget
    # price=70000, stop=66000 → risk_per_share=4000
    # quantity=100 → risk=400,000 < 2,000,000 → pass
    mgr = _make_manager()
    result = await mgr.check(
        "005930", SignalAction.BUY, 100, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "RISK_PER_TRADE" not in result.violations


@pytest.mark.asyncio
async def test_position_sizing_violation():
    """리스크 초과 시 위반 + 수량 조정."""
    # total=100M, 2% = 2M risk budget
    # price=70000, stop=60000 → risk_per_share=10000
    # quantity=300 → risk=3,000,000 > 2,000,000 → violation
    # max_qty = 2M / 10000 = 200
    mgr = _make_manager()
    result = await mgr.check(
        "005930", SignalAction.BUY, 300, Decimal("70000"), Decimal("60000"), "전기전자"
    )
    assert "RISK_PER_TRADE" in result.violations
    # 수량이 조정되어야 함 (MAX_POSITION 규칙과의 min 적용)
    assert result.adjusted_quantity <= 200


@pytest.mark.asyncio
async def test_position_sizing_no_stop_loss():
    """stop_loss=None이면 warning만 발생."""
    mgr = _make_manager()
    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), None, "전기전자"
    )
    assert "RISK_PER_TRADE" not in result.violations
    assert any("손절가 미설정" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Rule 2: Max Position (단일 종목 비중)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_position_pct_pass():
    """비중 한도 내일 때 통과."""
    # total=100M, 10% = 10M, but hard_cap=1M → effective=1M
    # quantity=10, price=70000 → 700,000 < 1M → pass
    mgr = _make_manager()
    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "MAX_POSITION" not in result.violations


@pytest.mark.asyncio
async def test_max_position_pct_violation():
    """비중 초과 시 위반 + 수량 조정."""
    # total=100M, 10% = 10M, hard_cap=1M → effective=1M
    # quantity=20, price=70000 → 1,400,000 > 1M → violation
    # max_qty = 1M / 70000 = 14
    mgr = _make_manager()
    result = await mgr.check(
        "005930", SignalAction.BUY, 20, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "MAX_POSITION" in result.violations
    assert result.adjusted_quantity <= 14


@pytest.mark.asyncio
async def test_max_position_hard_cap():
    """KRW 하드캡 적용 확인."""
    # hard_cap=500,000, price=70000 → max_qty = 7
    mgr = _make_manager(settings_overrides={"MAX_POSITION_SIZE_KRW": 500_000})
    result = await mgr.check(
        "005930", SignalAction.BUY, 20, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "MAX_POSITION" in result.violations
    assert result.adjusted_quantity <= 7


# ---------------------------------------------------------------------------
# Rule 3: Max Holdings (최대 보유 종목 수)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_holdings_existing_symbol():
    """기존 종목 추가매수는 통과."""
    state = _make_state(positions=[_make_position("005930")])
    mgr = _make_manager(state=state)
    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "MAX_HOLDINGS" not in result.violations


@pytest.mark.asyncio
async def test_max_holdings_new_symbol_violation():
    """보유 종목 수가 한도 이상일 때 신규 종목 차단."""
    # 5개 종목 보유 중 신규 종목 추가 시도
    positions = [_make_position(f"00{i}000") for i in range(5)]
    state = _make_state(positions=positions)
    mgr = _make_manager(state=state)
    result = await mgr.check(
        "999999", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "MAX_HOLDINGS" in result.violations
    assert result.adjusted_quantity == 0


# ---------------------------------------------------------------------------
# Rule 4: Sector Concentration (섹터 집중도)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sector_concentration_pass():
    """섹터 한도 내일 때 통과."""
    # 전기전자 현재 10%, 추가 5% → 15% < 30% → pass
    state = _make_state(
        sector_allocations={"전기전자": Decimal("10.0")},
    )
    mgr = _make_manager(state=state)
    # quantity=71, price=70000 → 4,970,000 → ~5% of 100M
    result = await mgr.check(
        "005930", SignalAction.BUY, 71, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "SECTOR_CONCENTRATION" not in result.violations


@pytest.mark.asyncio
async def test_sector_concentration_violation():
    """섹터 비중 초과 시 위반 + 수량 조정."""
    # 전기전자 현재 28%, 추가 5% → 33% > 30% → violation
    # allowed = 2%, max_qty = 2% / 100 * 100M / 70000 = 28
    state = _make_state(
        sector_allocations={"전기전자": Decimal("28.0")},
    )
    mgr = _make_manager(state=state)
    result = await mgr.check(
        "005930", SignalAction.BUY, 100, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "SECTOR_CONCENTRATION" in result.violations
    assert result.adjusted_quantity <= 28


@pytest.mark.asyncio
async def test_sector_concentration_per_sector_isolated():
    """다중 섹터 분포에서 거래 대상 섹터 버킷만 게이트되고 타 섹터는 무관 (F-15).

    실제 sector가 적재되면 게이트가 총량 캡이 아닌 업종별로 동작해야 함을 가드한다.
    화학이 이미 28%지만, 화학과 무관한 전기전자(0%) 매수는 차단되지 않는다.
    """
    state = _make_state(
        sector_allocations={"화학": Decimal("28.0")},
    )
    mgr = _make_manager(state=state)
    # 전기전자(현재 0%) 5% 매수 → 5% < 30% → 통과
    result = await mgr.check(
        "005930", SignalAction.BUY, 71, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "SECTOR_CONCENTRATION" not in result.violations


# ---------------------------------------------------------------------------
# Rule 5: Max Drawdown (최대 낙폭)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drawdown_pass():
    """낙폭이 한도 미만이면 통과."""
    state = _make_state(drawdown_pct=Decimal("5.0"))
    mgr = _make_manager(state=state)
    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "MAX_DRAWDOWN" not in result.violations


@pytest.mark.asyncio
async def test_drawdown_violation_blocks_all():
    """낙폭 초과 시 전체 차단 (수량=0)."""
    state = _make_state(drawdown_pct=Decimal("12.0"))
    mgr = _make_manager(state=state)
    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "MAX_DRAWDOWN" in result.violations
    assert result.adjusted_quantity == 0
    assert result.passed is False


# ---------------------------------------------------------------------------
# Rule 6: Daily Loss Limit (일일 손실 제한)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_loss_pct_pass():
    """일일 손실이 한도 내이면 통과."""
    state = _make_state(daily_pnl=Decimal("-200000"), daily_pnl_pct=Decimal("-1.5"))
    mgr = _make_manager(state=state)
    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "DAILY_LOSS_LIMIT" not in result.violations


@pytest.mark.asyncio
async def test_daily_loss_pct_violation():
    """일일 손실 비율 초과 시 차단."""
    state = _make_state(daily_pnl=Decimal("-400000"), daily_pnl_pct=Decimal("-3.5"))
    mgr = _make_manager(state=state)
    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "DAILY_LOSS_LIMIT" in result.violations
    assert result.adjusted_quantity == 0


@pytest.mark.asyncio
async def test_daily_loss_krw_violation():
    """일일 손실 금액 초과 시 차단."""
    # pct는 한도 내이지만 KRW 초과
    state = _make_state(daily_pnl=Decimal("-600000"), daily_pnl_pct=Decimal("-2.0"))
    mgr = _make_manager(state=state)
    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "DAILY_LOSS_LIMIT" in result.violations
    assert result.adjusted_quantity == 0


# ---------------------------------------------------------------------------
# Rule 7: Correlation (상관계수)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correlation_below_threshold():
    """상관계수가 임계치 미만이면 warning 없음."""
    state = _make_state(positions=[_make_position("000660")])
    mgr = _make_manager(state=state)

    # Mock _get_daily_returns to return uncorrelated series
    dates = pd.date_range("2026-01-01", periods=30)
    with patch.object(mgr, "_get_daily_returns") as mock_returns:
        # 상관계수가 낮은 두 시리즈
        mock_returns.side_effect = [
            pd.Series([0.01, -0.02, 0.03, -0.01, 0.02] * 6, index=dates),
            pd.Series([-0.01, 0.03, -0.02, 0.01, -0.03] * 6, index=dates),
        ]
        result = await mgr.check(
            "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
        )

    assert not any("상관관계" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_correlation_above_threshold():
    """상관계수가 임계치 이상이면 warning 발생."""
    state = _make_state(positions=[_make_position("000660")])
    mgr = _make_manager(state=state)

    # Mock _get_daily_returns to return highly correlated series
    dates = pd.date_range("2026-01-01", periods=30)
    returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.015] * 5
    with patch.object(mgr, "_get_daily_returns") as mock_returns:
        mock_returns.side_effect = [
            pd.Series(returns, index=dates),
            pd.Series(returns, index=dates),  # 동일 → corr=1.0
        ]
        result = await mgr.check(
            "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
        )

    # 상관관계 warning이 있어야 하지만 violation은 아님
    assert any("상관관계" in w for w in result.warnings)
    assert "CORRELATION" not in result.violations  # warning only, not violation


@pytest.mark.asyncio
async def test_correlation_insufficient_data():
    """데이터 부족 시 warning 발생."""
    state = _make_state(positions=[_make_position("000660")])
    mgr = _make_manager(state=state)

    with patch.object(mgr, "_get_daily_returns") as mock_returns:
        # 후보 종목 데이터가 10일뿐 (최소 20일 미달)
        dates = pd.date_range("2026-03-01", periods=10)
        mock_returns.return_value = pd.Series(
            [0.01] * 10, index=dates
        )
        result = await mgr.check(
            "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
        )

    assert any("데이터 부족" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Rule 8: Daily Trades (일일 거래 횟수)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_trades_pass():
    """거래 횟수가 한도 미만이면 통과."""
    state = _make_state(daily_trade_count=3)
    mgr = _make_manager(state=state)
    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "MAX_DAILY_TRADES" not in result.violations


@pytest.mark.asyncio
async def test_daily_trades_violation():
    """거래 횟수 초과 시 차단."""
    state = _make_state(daily_trade_count=5)
    mgr = _make_manager(state=state)
    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "MAX_DAILY_TRADES" in result.violations
    assert result.adjusted_quantity == 0


# ---------------------------------------------------------------------------
# 복합 시나리오
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_violations():
    """여러 규칙 동시 위반 시 모두 보고."""
    state = _make_state(
        drawdown_pct=Decimal("12.0"),
        daily_pnl=Decimal("-600000"),
        daily_pnl_pct=Decimal("-4.0"),
    )
    mgr = _make_manager(state=state)
    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert "MAX_DRAWDOWN" in result.violations
    assert "DAILY_LOSS_LIMIT" in result.violations
    assert result.adjusted_quantity == 0
    assert result.passed is False


@pytest.mark.asyncio
async def test_quantity_adjustment_minimum():
    """여러 규칙의 max_qty 중 최소값이 적용되어야 한다."""
    # Rule 1: risk_per_share=10000, max_risk=2M → max_qty=200
    # Rule 2: hard_cap=1M / 70000 → max_qty=14  ← 이게 최소
    # Rule 4: sector 허용 20% → max_qty = 20% / 100 * 100M / 70000 = 285
    state = _make_state(
        sector_allocations={"전기전자": Decimal("10.0")},
    )
    mgr = _make_manager(state=state)
    result = await mgr.check(
        "005930", SignalAction.BUY, 300, Decimal("70000"), Decimal("60000"), "전기전자"
    )
    # MAX_POSITION이 가장 제한적 (14주)
    assert result.adjusted_quantity <= 14


@pytest.mark.asyncio
async def test_all_rules_pass():
    """모든 규칙이 통과하는 정상 시나리오."""
    state = _make_state(
        drawdown_pct=Decimal("1.0"),
        daily_pnl=Decimal("100000"),
        daily_pnl_pct=Decimal("0.5"),
        daily_trade_count=1,
    )
    mgr = _make_manager(state=state)
    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert result.passed is True
    assert result.violations == []
    assert result.adjusted_quantity == 10


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_total_value():
    """총자산이 0이면 차단."""
    state = _make_state(total_value=Decimal("0"))
    mgr = _make_manager(state=state)
    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"), "전기전자"
    )
    assert result.passed is False
    assert "ZERO_PORTFOLIO" in result.violations
    assert result.adjusted_quantity == 0


@pytest.mark.asyncio
async def test_zero_price():
    """price=0이면 즉시 차단."""
    mgr = _make_manager()
    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("0"), Decimal("0"), "전기전자"
    )
    assert result.passed is False
    assert "INVALID_PRICE" in result.violations
    assert result.adjusted_quantity == 0


# ---------------------------------------------------------------------------
# F-04: 배치 in-flight 예약(BatchReservation) 누적 한도 검증
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reservation_none_keeps_existing_behavior():
    """reservation=None이면 기존 동작과 동일(보유 0 → 신규 통과)."""
    mgr = _make_manager()
    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"),
        "전기전자", reservation=None,
    )
    assert "MAX_HOLDINGS" not in result.violations
    assert "MAX_DAILY_TRADES" not in result.violations


@pytest.mark.asyncio
async def test_reservation_holdings_pushes_over_limit():
    """DB 보유 4 + 예약 신규 1 = 5(한도) → 다음 신규 종목 차단."""
    positions = [_make_position(f"10{i}000") for i in range(4)]  # 보유 4종목
    state = _make_state(positions=positions)
    mgr = _make_manager(state=state)  # MAX_PORTFOLIO_POSITIONS=5

    reservation = BatchReservation()
    reservation.reserve(
        "888888", "전기전자", Decimal("1000000"), is_new_holding=True
    )  # 배치 신규 1종목 → 합 5

    result = await mgr.check(
        "999999", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"),
        "전기전자", reservation=reservation,
    )
    assert "MAX_HOLDINGS" in result.violations
    assert result.adjusted_quantity == 0


@pytest.mark.asyncio
async def test_reservation_holdings_addon_to_reserved_symbol_allowed():
    """이번 배치에서 예약한 종목의 추가매수는 보유로 간주되어 통과."""
    positions = [_make_position(f"10{i}000") for i in range(4)]
    state = _make_state(positions=positions)
    mgr = _make_manager(state=state)

    reservation = BatchReservation()
    reservation.reserve(
        "888888", "전기전자", Decimal("1000000"), is_new_holding=True
    )

    # 예약 신규집합에 이미 있는 888888 추가 → 신규 보유 증가 아님 → 허용
    result = await mgr.check(
        "888888", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"),
        "전기전자", reservation=reservation,
    )
    assert "MAX_HOLDINGS" not in result.violations


@pytest.mark.asyncio
async def test_reservation_daily_trades_accumulates():
    """DB 당일거래 3 + 예약 2 = 5(한도) → 다음 진입 차단."""
    state = _make_state(daily_trade_count=3)
    mgr = _make_manager(state=state)  # MAX_DAILY_TRADES=5

    reservation = BatchReservation()
    reservation.reserve("100000", "전기전자", Decimal("500000"), is_new_holding=True)
    reservation.reserve("200000", "전기전자", Decimal("500000"), is_new_holding=True)

    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"),
        "전기전자", reservation=reservation,
    )
    assert "MAX_DAILY_TRADES" in result.violations
    assert result.adjusted_quantity == 0


@pytest.mark.asyncio
async def test_reservation_daily_trades_below_limit_passes():
    """DB 1 + 예약 1 = 2 < 5 → 통과."""
    state = _make_state(daily_trade_count=1)
    mgr = _make_manager(state=state)

    reservation = BatchReservation()
    reservation.reserve("100000", "전기전자", Decimal("500000"), is_new_holding=True)

    result = await mgr.check(
        "005930", SignalAction.BUY, 10, Decimal("70000"), Decimal("66000"),
        "전기전자", reservation=reservation,
    )
    assert "MAX_DAILY_TRADES" not in result.violations


@pytest.mark.asyncio
async def test_reservation_sector_concentration_accumulates():
    """섹터 현재 10% + 예약 거래대금 18% + 추가 5% = 33% > 30% → 위반."""
    # total_value=100M. 예약 18% = 18,000,000
    state = _make_state(sector_allocations={"전기전자": Decimal("10.0")})
    mgr = _make_manager(state=state)  # SECTOR_CONCENTRATION_PCT=30

    reservation = BatchReservation()
    reservation.reserve(
        "100000", "전기전자", Decimal("18000000"), is_new_holding=True
    )

    # 추가 ~5% (71주 * 70000 = 4,970,000)
    result = await mgr.check(
        "005930", SignalAction.BUY, 71, Decimal("70000"), Decimal("66000"),
        "전기전자", reservation=reservation,
    )
    assert "SECTOR_CONCENTRATION" in result.violations


@pytest.mark.asyncio
async def test_reservation_other_sector_not_affected():
    """예약이 다른 섹터면 대상 섹터 비중에 영향 없음 → 통과."""
    state = _make_state(sector_allocations={"전기전자": Decimal("10.0")})
    mgr = _make_manager(state=state)

    reservation = BatchReservation()
    reservation.reserve("100000", "바이오", Decimal("18000000"), is_new_holding=True)

    result = await mgr.check(
        "005930", SignalAction.BUY, 71, Decimal("70000"), Decimal("66000"),
        "전기전자", reservation=reservation,
    )
    assert "SECTOR_CONCENTRATION" not in result.violations
