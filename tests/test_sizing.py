"""Phase 4 Step 5: PositionSizer 단위 테스트.

기본 계산 / 캡 적용 / 기존 포지션 차감 / 경계값 / 예외 = 14 테스트.
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.core.models import PositionSizing
from src.strategy.sizing import PositionSizer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides: object) -> MagicMock:
    s = MagicMock()
    s.RISK_PER_TRADE_PCT = 2.0
    s.MAX_POSITION_PCT = 10.0
    s.MAX_POSITION_SIZE_KRW = 10_000_000  # 1천만원
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _sizer(**overrides: object) -> PositionSizer:
    return PositionSizer(_make_settings(**overrides))


# ---------------------------------------------------------------------------
# 기본 계산
# ---------------------------------------------------------------------------


class TestBasicCalculation:
    def test_fixed_fractional(self):
        """1억 포트, 2% 리스크, entry=50000, stop=48000.

        risk_amount = 100_000_000 × 0.02 = 2_000_000
        risk_per_share = 2_000
        raw_qty = 1_000 → cap_pct = 100M×10%/50000 = 200, cap_krw = 10M/50000 = 200
        quantity = min(1000, 200, 200) = 200
        """
        sizer = _sizer()
        result = sizer.calculate(
            symbol="005930",
            entry_price=Decimal("50000"),
            stop_loss_price=Decimal("48000"),
            take_profit_price=Decimal("56000"),
            total_portfolio_value=Decimal("100000000"),
        )
        assert isinstance(result, PositionSizing)
        assert result.symbol == "005930"
        assert result.quantity == 200  # MAX_POSITION_PCT 캡 적용
        assert result.entry_price == Decimal("50000")
        assert result.stop_loss_price == Decimal("48000")
        assert result.risk_per_share == Decimal("2000")

    def test_output_fields_complete(self):
        """모든 PositionSizing 출력 필드가 정확한지 검증."""
        sizer = _sizer(MAX_POSITION_SIZE_KRW=50_000_000)  # 캡 완화
        result = sizer.calculate(
            symbol="005930",
            entry_price=Decimal("10000"),
            stop_loss_price=Decimal("9000"),
            take_profit_price=Decimal("13000"),
            total_portfolio_value=Decimal("10000000"),  # 1천만
        )
        # risk_amount = 10M × 2% = 200,000 / risk_per_share=1000 → raw_qty=200
        # cap_pct = 10M × 10% / 10000 = 100
        # cap_krw = 50M / 10000 = 5000
        # qty = min(200, 100, 5000) = 100
        assert result.quantity == 100
        assert result.position_value_krw == Decimal("1000000")  # 100 × 10000
        assert result.risk_amount_krw == Decimal("100000")  # 100 × 1000
        assert result.risk_pct_of_portfolio == Decimal("1.00")  # 100000/10M×100
        assert result.position_pct_of_portfolio == Decimal("10.00")  # 1M/10M×100
        assert result.risk_reward_ratio == Decimal("3.00")  # (13000-10000)/(10000-9000)


# ---------------------------------------------------------------------------
# 캡 적용
# ---------------------------------------------------------------------------


class TestCapConstraints:
    def test_max_position_pct_cap(self):
        """리스크 기반 qty가 MAX_POSITION_PCT 한도를 초과하면 캡 적용."""
        sizer = _sizer(MAX_POSITION_SIZE_KRW=100_000_000)  # KRW 캡 완화
        result = sizer.calculate(
            symbol="005930",
            entry_price=Decimal("50000"),
            stop_loss_price=Decimal("49900"),  # risk_per_share=100 → raw_qty=20000
            take_profit_price=None,
            total_portfolio_value=Decimal("100000000"),
        )
        # cap_pct = 100M × 10% / 50000 = 200
        assert result.quantity == 200

    def test_max_position_krw_hard_cap(self):
        """KRW 하드캡이 더 제한적인 경우."""
        sizer = _sizer(MAX_POSITION_SIZE_KRW=500_000)  # 50만원 하드캡
        result = sizer.calculate(
            symbol="005930",
            entry_price=Decimal("50000"),
            stop_loss_price=Decimal("48000"),
            take_profit_price=None,
            total_portfolio_value=Decimal("100000000"),
        )
        # cap_krw = 500000 / 50000 = 10
        # cap_pct = 100M × 10% / 50000 = 200
        # qty = min(1000, 200, 10) = 10
        assert result.quantity == 10

    def test_all_caps_interact(self):
        """3개 캡 중 가장 제한적인 것이 적용."""
        sizer = _sizer(
            RISK_PER_TRADE_PCT=5.0,  # 높은 리스크 → 많은 수량
            MAX_POSITION_PCT=5.0,  # 비중 캡 타이트
            MAX_POSITION_SIZE_KRW=2_000_000,  # KRW 캡 더 타이트
        )
        result = sizer.calculate(
            symbol="005930",
            entry_price=Decimal("100000"),
            stop_loss_price=Decimal("99000"),  # risk_per_share=1000
            take_profit_price=None,
            total_portfolio_value=Decimal("100000000"),
        )
        # risk-based = 100M × 5% / 1000 = 5000
        # cap_pct = 100M × 5% / 100000 = 50
        # cap_krw = 2M / 100000 = 20  ← 가장 제한적
        assert result.quantity == 20


# ---------------------------------------------------------------------------
# 기존 포지션 차감
# ---------------------------------------------------------------------------


class TestExistingPositionDeduction:
    def test_deduction(self):
        """기존 50만 보유 → 잔여 한도만 사용."""
        sizer = _sizer(MAX_POSITION_SIZE_KRW=1_000_000)
        result = sizer.calculate(
            symbol="005930",
            entry_price=Decimal("50000"),
            stop_loss_price=Decimal("48000"),
            take_profit_price=None,
            total_portfolio_value=Decimal("100000000"),
            existing_position_value=Decimal("500000"),
        )
        # cap_krw = 1M / 50000 = 20, existing_equiv = 500000/50000 = 10
        # cap_from_krw = 20 - 10 = 10
        # cap_pct = 200, existing_equiv = 10, cap_from_pct = 190
        # qty = min(1000, 190, 10) = 10
        assert result.quantity == 10

    def test_existing_exceeds_cap(self):
        """기존 포지션이 캡 이상 → quantity=0."""
        sizer = _sizer(MAX_POSITION_SIZE_KRW=1_000_000)
        result = sizer.calculate(
            symbol="005930",
            entry_price=Decimal("50000"),
            stop_loss_price=Decimal("48000"),
            take_profit_price=None,
            total_portfolio_value=Decimal("100000000"),
            existing_position_value=Decimal("1_500_000"),
        )
        # cap_krw = 20, existing_equiv = 30, cap_from_krw = 0
        assert result.quantity == 0
        assert result.position_value_krw == Decimal("0")


# ---------------------------------------------------------------------------
# 경계값
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_minimum_one_share(self):
        """risk_per_share가 매우 커서 raw_qty=0 → floor 1주 (캡 이내일 때)."""
        sizer = _sizer(MAX_POSITION_SIZE_KRW=100_000_000)
        result = sizer.calculate(
            symbol="005930",
            entry_price=Decimal("50000"),
            stop_loss_price=Decimal("10000"),  # risk_per_share=40000
            take_profit_price=None,
            total_portfolio_value=Decimal("1000000"),  # 100만 포트
        )
        # risk_amount = 1M × 2% = 20000 / 40000 = 0 → max(1, 0) = 1
        # cap_pct = 1M × 10% / 50000 = 2
        # cap_krw = 100M / 50000 = 2000
        # qty = min(1, 2, 2000) = 1
        assert result.quantity == 1

    def test_take_profit_none(self):
        """take_profit_price=None → risk_reward_ratio=None."""
        sizer = _sizer()
        result = sizer.calculate(
            symbol="005930",
            entry_price=Decimal("50000"),
            stop_loss_price=Decimal("48000"),
            take_profit_price=None,
            total_portfolio_value=Decimal("100000000"),
        )
        assert result.risk_reward_ratio is None
        assert result.take_profit_price is None

    def test_risk_reward_populated(self):
        """take_profit 있을 때 ratio 정상 계산."""
        sizer = _sizer()
        result = sizer.calculate(
            symbol="005930",
            entry_price=Decimal("50000"),
            stop_loss_price=Decimal("48000"),
            take_profit_price=Decimal("56000"),
            total_portfolio_value=Decimal("100000000"),
        )
        # (56000-50000) / (50000-48000) = 6000/2000 = 3.00
        assert result.risk_reward_ratio == Decimal("3.00")

    def test_quantity_zero_fields(self):
        """quantity=0일 때 비율 필드들이 0."""
        sizer = _sizer(MAX_POSITION_SIZE_KRW=10_000)  # 매우 낮은 캡
        result = sizer.calculate(
            symbol="005930",
            entry_price=Decimal("50000"),
            stop_loss_price=Decimal("48000"),
            take_profit_price=None,
            total_portfolio_value=Decimal("100000000"),
        )
        # cap_krw = 10000 / 50000 = 0
        assert result.quantity == 0
        assert result.risk_pct_of_portfolio == Decimal("0")
        assert result.position_pct_of_portfolio == Decimal("0")


# ---------------------------------------------------------------------------
# 예외
# ---------------------------------------------------------------------------


class TestValidation:
    def test_stop_equals_entry_raises(self):
        with pytest.raises(ValueError, match="stop_loss_price must differ"):
            _sizer().calculate(
                symbol="005930",
                entry_price=Decimal("50000"),
                stop_loss_price=Decimal("50000"),
                take_profit_price=None,
                total_portfolio_value=Decimal("100000000"),
            )

    def test_zero_entry_raises(self):
        with pytest.raises(ValueError, match="entry_price must be positive"):
            _sizer().calculate(
                symbol="005930",
                entry_price=Decimal("0"),
                stop_loss_price=Decimal("48000"),
                take_profit_price=None,
                total_portfolio_value=Decimal("100000000"),
            )

    def test_zero_portfolio_raises(self):
        with pytest.raises(ValueError, match="total_portfolio_value must be positive"):
            _sizer().calculate(
                symbol="005930",
                entry_price=Decimal("50000"),
                stop_loss_price=Decimal("48000"),
                take_profit_price=None,
                total_portfolio_value=Decimal("0"),
            )

    def test_negative_stop_raises(self):
        with pytest.raises(ValueError, match="stop_loss_price must be positive"):
            _sizer().calculate(
                symbol="005930",
                entry_price=Decimal("50000"),
                stop_loss_price=Decimal("-1000"),
                take_profit_price=None,
                total_portfolio_value=Decimal("100000000"),
            )
