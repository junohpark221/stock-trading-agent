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
# atr_clamped (F-13)
# ---------------------------------------------------------------------------


class TestAtrClamped:
    def test_midrange_no_clamp(self):
        """ATR% 3% × 1.5 = 4.5% (∈ [2.5,6]) → clamp 미적용, R:R 보존."""
        stop, tp = ExitPriceCalculator.atr_clamped(
            Decimal("50000"), Decimal("1500")
        )
        assert stop == Decimal("47750")  # 50000 × (1 - 0.045)
        # tp = 50000 + 1.67 × (50000 - 47750) = 50000 + 3757.5
        assert tp == Decimal("53757.5")
        # R:R = reward/risk = 3757.5 / 2250 = 1.67
        assert (tp - Decimal("50000")) / (Decimal("50000") - stop) == Decimal("1.67")

    def test_high_vol_clamped_at_cap(self):
        """ATR% 8% × 1.5 = 12% → cap 6%. 고변동이 상한에 걸려도 R:R 보존."""
        stop, tp = ExitPriceCalculator.atr_clamped(
            Decimal("50000"), Decimal("4000")
        )
        assert stop == Decimal("47000")  # cap 6% → 50000 × 0.94
        assert tp == Decimal("55010")    # 50000 + 1.67 × 3000
        assert (tp - Decimal("50000")) / (Decimal("50000") - stop) == Decimal("1.67")

    def test_low_vol_clamped_at_floor(self):
        """ATR% 1% × 1.5 = 1.5% → floor 2.5%. 저변동이 하한으로 보정."""
        stop, tp = ExitPriceCalculator.atr_clamped(
            Decimal("50000"), Decimal("500")
        )
        assert stop == Decimal("48750")  # floor 2.5% → 50000 × 0.975
        assert tp == Decimal("52087.5")  # 50000 + 1.67 × 1250

    def test_custom_params(self):
        stop, tp = ExitPriceCalculator.atr_clamped(
            Decimal("50000"), Decimal("1500"),
            stop_mult=Decimal("2.0"),
            floor_pct=Decimal("2.0"),
            cap_pct=Decimal("10.0"),
            rr_ratio=Decimal("2.0"),
        )
        # width = 2.0 × 3% = 6% (∈ [2,10]) → stop = 47000
        assert stop == Decimal("47000")
        # tp = 50000 + 2.0 × 3000 = 56000
        assert tp == Decimal("56000")

    def test_zero_atr_raises(self):
        with pytest.raises(ValueError, match="atr must be positive"):
            ExitPriceCalculator.atr_clamped(Decimal("50000"), Decimal("0"))

    def test_invalid_entry_raises(self):
        with pytest.raises(ValueError, match="entry_price must be positive"):
            ExitPriceCalculator.atr_clamped(Decimal("0"), Decimal("1500"))


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
# rebase (F-16 체결가 재적용 / PRJ-04 병합 재산정 공용)
# ---------------------------------------------------------------------------


class TestRebase:
    def test_preserves_widths(self):
        """손절 3% / 익절 5% 폭이 목표가에 그대로 재적용된다."""
        result = ExitPriceCalculator.rebase(
            reference_price=Decimal("80000"),
            stop0=Decimal("77600"),
            tp0=Decimal("84000"),
            target_price=Decimal("75000"),
        )
        assert result == (Decimal("72750.00"), Decimal("78750.00"))

    def test_no_take_profit_returns_none_tp(self):
        result = ExitPriceCalculator.rebase(
            reference_price=Decimal("80000"),
            stop0=Decimal("77600"),
            tp0=None,
            target_price=Decimal("75000"),
        )
        assert result is not None
        assert result[1] is None

    @pytest.mark.parametrize(
        ("reference", "stop0", "target"),
        [
            (Decimal("0"), Decimal("70000"), Decimal("75000")),  # 기준가 비양수
            (Decimal("80000"), None, Decimal("75000")),  # 손절가 결측
            (Decimal("80000"), Decimal("0"), Decimal("75000")),  # 손절가 0
            (Decimal("80000"), Decimal("90000"), Decimal("75000")),  # 손절가 역전
            (Decimal("80000"), Decimal("77600"), Decimal("0")),  # 목표가 비양수
        ],
    )
    def test_invalid_input_returns_none(self, reference, stop0, target):
        """비정상 입력이면 None → 호출자가 원본 유지."""
        assert (
            ExitPriceCalculator.rebase(
                reference_price=reference,
                stop0=stop0,
                tp0=Decimal("84000"),
                target_price=target,
            )
            is None
        )


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
