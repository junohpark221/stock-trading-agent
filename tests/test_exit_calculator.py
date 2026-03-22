"""Phase 4 Step 5: ExitPriceCalculator 단위 테스트.

fixed_percentage / atr_based / trailing_stop / risk_reward_ratio
× (기본 / 경계값 / 예외) = 14 테스트.
"""

from decimal import Decimal

import pytest

from src.strategy.exit_calculator import ExitPriceCalculator


# ---------------------------------------------------------------------------
# fixed_percentage
# ---------------------------------------------------------------------------


class TestFixedPercentage:
    def test_basic(self):
        stop, tp = ExitPriceCalculator.fixed_percentage(
            Decimal("50000"), Decimal("3"), Decimal("5")
        )
        assert stop == Decimal("48500")
        assert tp == Decimal("52500")

    def test_zero_pct(self):
        stop, tp = ExitPriceCalculator.fixed_percentage(
            Decimal("50000"), Decimal("0"), Decimal("0")
        )
        assert stop == Decimal("50000")
        assert tp == Decimal("50000")

    def test_large_pct(self):
        stop, tp = ExitPriceCalculator.fixed_percentage(
            Decimal("50000"), Decimal("50"), Decimal("50")
        )
        assert stop == Decimal("25000")
        assert tp == Decimal("75000")

    def test_invalid_entry_raises(self):
        with pytest.raises(ValueError, match="entry_price must be positive"):
            ExitPriceCalculator.fixed_percentage(
                Decimal("0"), Decimal("3"), Decimal("5")
            )

    def test_negative_stop_pct_raises(self):
        with pytest.raises(ValueError, match="stop_pct must be non-negative"):
            ExitPriceCalculator.fixed_percentage(
                Decimal("50000"), Decimal("-1"), Decimal("5")
            )

    def test_negative_tp_pct_raises(self):
        with pytest.raises(ValueError, match="tp_pct must be non-negative"):
            ExitPriceCalculator.fixed_percentage(
                Decimal("50000"), Decimal("3"), Decimal("-1")
            )


# ---------------------------------------------------------------------------
# atr_based
# ---------------------------------------------------------------------------


class TestAtrBased:
    def test_basic(self):
        stop, tp = ExitPriceCalculator.atr_based(
            Decimal("50000"), Decimal("1000")
        )
        assert stop == Decimal("48000")
        assert tp == Decimal("53000")

    def test_custom_multipliers(self):
        stop, tp = ExitPriceCalculator.atr_based(
            Decimal("50000"), Decimal("1000"),
            stop_mult=Decimal("1.5"), tp_mult=Decimal("4.0"),
        )
        assert stop == Decimal("48500")
        assert tp == Decimal("54000")

    def test_stop_floor_at_one(self):
        """ATR이 매우 커서 stop이 음수 → Decimal("1")로 floor."""
        stop, tp = ExitPriceCalculator.atr_based(
            Decimal("1000"), Decimal("10000"), stop_mult=Decimal("2.0"),
        )
        assert stop == Decimal("1")
        assert tp > Decimal("1000")

    def test_zero_atr_raises(self):
        with pytest.raises(ValueError, match="atr must be positive"):
            ExitPriceCalculator.atr_based(Decimal("50000"), Decimal("0"))

    def test_invalid_entry_raises(self):
        with pytest.raises(ValueError, match="entry_price must be positive"):
            ExitPriceCalculator.atr_based(Decimal("-100"), Decimal("1000"))


# ---------------------------------------------------------------------------
# trailing_stop_price
# ---------------------------------------------------------------------------


class TestTrailingStop:
    def test_basic(self):
        result = ExitPriceCalculator.trailing_stop_price(
            Decimal("55000"), Decimal("5")
        )
        assert result == Decimal("52250")

    def test_at_entry(self):
        """highest == entry 시에도 정상 작동."""
        result = ExitPriceCalculator.trailing_stop_price(
            Decimal("50000"), Decimal("3")
        )
        assert result == Decimal("48500")

    def test_invalid_highest_raises(self):
        with pytest.raises(ValueError, match="highest_since_entry must be positive"):
            ExitPriceCalculator.trailing_stop_price(Decimal("0"), Decimal("5"))

    def test_invalid_trailing_pct_raises(self):
        with pytest.raises(ValueError, match="trailing_pct must be positive"):
            ExitPriceCalculator.trailing_stop_price(Decimal("55000"), Decimal("0"))


# ---------------------------------------------------------------------------
# risk_reward_ratio
# ---------------------------------------------------------------------------


class TestRiskRewardRatio:
    def test_basic(self):
        """50000 진입, 48000 손절, 56000 익절 → (6000/2000) = 3.00."""
        ratio = ExitPriceCalculator.risk_reward_ratio(
            Decimal("50000"), Decimal("48000"), Decimal("56000")
        )
        assert ratio == Decimal("3.00")

    def test_one_to_one(self):
        ratio = ExitPriceCalculator.risk_reward_ratio(
            Decimal("50000"), Decimal("48000"), Decimal("52000")
        )
        assert ratio == Decimal("1.00")

    def test_entry_equals_stop_raises(self):
        with pytest.raises(ValueError, match="entry_price and stop_loss must differ"):
            ExitPriceCalculator.risk_reward_ratio(
                Decimal("50000"), Decimal("50000"), Decimal("55000")
            )

    def test_returns_decimal(self):
        ratio = ExitPriceCalculator.risk_reward_ratio(
            Decimal("50000"), Decimal("49000"), Decimal("53000")
        )
        assert isinstance(ratio, Decimal)
        assert ratio == Decimal("3.00")

    def test_fractional_ratio(self):
        """비정수 비율도 소수점 2자리로 반올림."""
        ratio = ExitPriceCalculator.risk_reward_ratio(
            Decimal("50000"), Decimal("49000"), Decimal("51500")
        )
        # reward=1500, risk=1000 → 1.50
        assert ratio == Decimal("1.50")
