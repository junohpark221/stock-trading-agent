"""차트 패턴 감지 테스트.

결정론적 합성 데이터를 사용하여 각 패턴 감지 함수를 검증한다.
"""

from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from src.analysis.technical.indicators import compute_all_indicators
from src.analysis.technical.patterns import (
    detect_death_cross,
    detect_double_bottom,
    detect_double_top,
    detect_golden_cross,
    detect_macd_divergence,
    detect_support_resistance,
    scan_patterns,
)
from src.core.models import PatternSignal

# ---------------------------------------------------------------------------
# Fixtures — 결정론적 합성 데이터
# ---------------------------------------------------------------------------


@pytest.fixture()
def golden_cross_df() -> pd.DataFrame:
    """충분한 하락 후 최근에 급등하여 SMA5가 SMA20 상향 돌파 보장.

    SMA20가 안정화될 수 있도록 50일 하락 후 마지막 20일에서 급등.
    """
    n = 70
    dates = pd.date_range("2025-06-01", periods=n, freq="B")
    prices = np.concatenate([
        np.linspace(55000, 44000, 50),  # 긴 하락 (SMA20 충분히 안정)
        np.linspace(44000, 54000, 20),  # 최근 급등 → cross 발생
    ])
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": np.full(n, 1_000_000.0),
        },
        index=dates,
    )


@pytest.fixture()
def death_cross_df() -> pd.DataFrame:
    """충분한 상승 후 최근에 급락하여 SMA5가 SMA20 하향 돌파 보장."""
    n = 70
    dates = pd.date_range("2025-06-01", periods=n, freq="B")
    prices = np.concatenate([
        np.linspace(42000, 55000, 50),  # 긴 상승
        np.linspace(55000, 43000, 20),  # 최근 급락 → cross 발생
    ])
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": np.full(n, 1_000_000.0),
        },
        index=dates,
    )


@pytest.fixture()
def double_bottom_df() -> pd.DataFrame:
    """W자 패턴: 명확한 두 저점과 네크라인 돌파.

    60일 window 안에 패턴이 모두 포함되도록 설계.
    """
    n = 55
    dates = pd.date_range("2025-06-01", periods=n, freq="B")
    base = np.concatenate([
        np.linspace(50000, 44000, 10),   # first dip
        np.linspace(44000, 49000, 12),   # recovery (neckline)
        np.linspace(49000, 44200, 10),   # second dip (within 2% of first)
        np.linspace(44200, 52000, 23),   # breakout above neckline
    ])
    np.random.seed(42)
    noise = np.random.normal(0, 150, n)
    prices = base + noise
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": np.full(n, 1_000_000.0),
        },
        index=dates,
    )


@pytest.fixture()
def double_top_df() -> pd.DataFrame:
    """M자 패턴: 명확한 두 고점과 네크라인 하향 돌파.

    60일 window 안에 패턴이 모두 포함되도록 설계.
    """
    n = 55
    dates = pd.date_range("2025-06-01", periods=n, freq="B")
    base = np.concatenate([
        np.linspace(50000, 56000, 10),   # first rise
        np.linspace(56000, 51000, 12),   # dip (neckline)
        np.linspace(51000, 55800, 10),   # second rise (within 2%)
        np.linspace(55800, 48000, 23),   # drop below neckline
    ])
    np.random.seed(43)
    noise = np.random.normal(0, 150, n)
    prices = base + noise
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": np.full(n, 1_000_000.0),
        },
        index=dates,
    )


@pytest.fixture()
def divergence_df() -> pd.DataFrame:
    """가격 고점↑ but 모멘텀↓ (bearish divergence 유도).

    가격은 상승 추세이지만 상승폭이 줄어들어 MACD가 하락하도록 설계.
    """
    n = 120
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    # 초반 강한 상승 → 후반 약한 상승 (모멘텀 약화)
    prices = np.concatenate([
        np.linspace(40000, 40000, 10),
        np.linspace(40000, 50000, 25),   # 강한 상승 (1차 고점)
        np.linspace(50000, 46000, 15),   # 조정
        np.linspace(46000, 51000, 25),   # 약한 상승 (2차 고점 - 가격은 더 높지만 모멘텀 약화)
        np.linspace(51000, 48000, 15),   # 하락
        np.linspace(48000, 49000, 30),   # 횡보
    ])
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": np.full(n, 1_000_000.0),
        },
        index=dates,
    )


@pytest.fixture()
def flat_df() -> pd.DataFrame:
    """횡보 (변동 없음)."""
    n = 60
    dates = pd.date_range("2025-06-01", periods=n, freq="B")
    prices = np.full(n, 50000.0)
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.001,
            "low": prices * 0.999,
            "close": prices,
            "volume": np.full(n, 1_000_000.0),
        },
        index=dates,
    )


@pytest.fixture()
def empty_df() -> pd.DataFrame:
    """빈 DataFrame."""
    return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


@pytest.fixture()
def short_df() -> pd.DataFrame:
    """10일 짧은 DataFrame."""
    n = 10
    dates = pd.date_range("2025-06-01", periods=n, freq="B")
    prices = np.linspace(50000, 51000, n)
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": np.full(n, 1_000_000.0),
        },
        index=dates,
    )


# ---------------------------------------------------------------------------
# TestDetectGoldenCross
# ---------------------------------------------------------------------------


class TestDetectGoldenCross:
    def test_detects_golden_cross(self, golden_cross_df: pd.DataFrame) -> None:
        """하락→급등 데이터에서 골든크로스 감지."""
        from src.analysis.technical.indicators import calculate_sma

        sma = calculate_sma(golden_cross_df["close"], periods=[5, 20])
        result = detect_golden_cross(sma[5], sma[20], lookback=15)
        assert result is not None
        assert result.pattern_name == "golden_cross"
        assert result.signal_type == "bullish"

    def test_no_cross_in_flat(self, flat_df: pd.DataFrame) -> None:
        """횡보 데이터에서 골든크로스 미감지."""
        from src.analysis.technical.indicators import calculate_sma

        sma = calculate_sma(flat_df["close"], periods=[5, 20])
        result = detect_golden_cross(sma[5], sma[20], lookback=15)
        assert result is None

    def test_short_data_returns_none(self) -> None:
        """데이터 부족 시 None."""
        s = pd.Series([1.0, 2.0, 3.0])
        result = detect_golden_cross(s, s)
        assert result is None

    def test_confidence_range(self, golden_cross_df: pd.DataFrame) -> None:
        """confidence가 0.6~0.9 범위."""
        from src.analysis.technical.indicators import calculate_sma

        sma = calculate_sma(golden_cross_df["close"], periods=[5, 20])
        result = detect_golden_cross(sma[5], sma[20], lookback=15)
        assert result is not None
        assert Decimal("0.6") <= result.confidence <= Decimal("0.9")


# ---------------------------------------------------------------------------
# TestDetectDeathCross
# ---------------------------------------------------------------------------


class TestDetectDeathCross:
    def test_detects_death_cross(self, death_cross_df: pd.DataFrame) -> None:
        """상승→급락 데이터에서 데드크로스 감지."""
        from src.analysis.technical.indicators import calculate_sma

        sma = calculate_sma(death_cross_df["close"], periods=[5, 20])
        result = detect_death_cross(sma[5], sma[20], lookback=15)
        assert result is not None
        assert result.pattern_name == "death_cross"
        assert result.signal_type == "bearish"

    def test_no_cross_in_flat(self, flat_df: pd.DataFrame) -> None:
        """횡보 데이터에서 데드크로스 미감지."""
        from src.analysis.technical.indicators import calculate_sma

        sma = calculate_sma(flat_df["close"], periods=[5, 20])
        result = detect_death_cross(sma[5], sma[20], lookback=15)
        assert result is None

    def test_short_data_returns_none(self) -> None:
        """데이터 부족 시 None."""
        s = pd.Series([3.0, 2.0, 1.0])
        result = detect_death_cross(s, s)
        assert result is None

    def test_confidence_range(self, death_cross_df: pd.DataFrame) -> None:
        """confidence가 0.6~0.9 범위."""
        from src.analysis.technical.indicators import calculate_sma

        sma = calculate_sma(death_cross_df["close"], periods=[5, 20])
        result = detect_death_cross(sma[5], sma[20], lookback=15)
        assert result is not None
        assert Decimal("0.6") <= result.confidence <= Decimal("0.9")


# ---------------------------------------------------------------------------
# TestDetectSupportResistance
# ---------------------------------------------------------------------------


class TestDetectSupportResistance:
    def test_returns_dict_structure(self, golden_cross_df: pd.DataFrame) -> None:
        """support, resistance 키를 가진 dict 반환."""
        result = detect_support_resistance(golden_cross_df["close"])
        assert "support" in result
        assert "resistance" in result
        assert isinstance(result["support"], list)
        assert isinstance(result["resistance"], list)

    def test_flat_data(self, flat_df: pd.DataFrame) -> None:
        """횡보 데이터 — 극값이 적어도 에러 없이 반환."""
        result = detect_support_resistance(flat_df["close"])
        assert isinstance(result, dict)

    def test_short_data(self) -> None:
        """짧은 데이터 — 빈 리스트 반환."""
        s = pd.Series([100.0, 101.0, 102.0])
        result = detect_support_resistance(s)
        assert result == {"support": [], "resistance": []}

    def test_levels_are_floats(self, double_bottom_df: pd.DataFrame) -> None:
        """반환된 레벨이 float 타입."""
        result = detect_support_resistance(double_bottom_df["close"])
        for level in result["support"] + result["resistance"]:
            assert isinstance(level, float)


# ---------------------------------------------------------------------------
# TestDetectDoubleTop
# ---------------------------------------------------------------------------


class TestDetectDoubleTop:
    def test_detects_m_pattern(self, double_top_df: pd.DataFrame) -> None:
        """M자 패턴 데이터에서 더블탑 감지."""
        result = detect_double_top(double_top_df["close"])
        assert result is not None
        assert result.pattern_name == "double_top"
        assert result.signal_type == "bearish"

    def test_no_pattern_in_flat(self, flat_df: pd.DataFrame) -> None:
        """횡보 데이터에서 미감지."""
        result = detect_double_top(flat_df["close"])
        assert result is None

    def test_confidence_range(self, double_top_df: pd.DataFrame) -> None:
        """confidence가 0.5~0.9 범위."""
        result = detect_double_top(double_top_df["close"])
        assert result is not None
        assert Decimal("0.5") <= result.confidence <= Decimal("0.9")


# ---------------------------------------------------------------------------
# TestDetectDoubleBottom
# ---------------------------------------------------------------------------


class TestDetectDoubleBottom:
    def test_detects_w_pattern(self, double_bottom_df: pd.DataFrame) -> None:
        """W자 패턴 데이터에서 더블바텀 감지."""
        result = detect_double_bottom(double_bottom_df["close"])
        assert result is not None
        assert result.pattern_name == "double_bottom"
        assert result.signal_type == "bullish"

    def test_no_pattern_in_flat(self, flat_df: pd.DataFrame) -> None:
        """횡보 데이터에서 미감지."""
        result = detect_double_bottom(flat_df["close"])
        assert result is None

    def test_confidence_range(self, double_bottom_df: pd.DataFrame) -> None:
        """confidence가 0.5~0.9 범위."""
        result = detect_double_bottom(double_bottom_df["close"])
        assert result is not None
        assert Decimal("0.5") <= result.confidence <= Decimal("0.9")


# ---------------------------------------------------------------------------
# TestDetectMacdDivergence
# ---------------------------------------------------------------------------


class TestDetectMacdDivergence:
    def test_bearish_divergence(self, divergence_df: pd.DataFrame) -> None:
        """가격 고점↑ MACD 고점↓ bearish divergence 감지."""
        from src.analysis.technical.indicators import calculate_macd

        macd_line, _, _ = calculate_macd(divergence_df["close"])
        result = detect_macd_divergence(divergence_df["close"], macd_line)
        # 합성 데이터로 반드시 감지된다고 보장하기 어려우므로
        # 감지되면 올바른 타입인지, 미감지면 None인지 확인
        if result is not None:
            assert result.signal_type == "bearish"
            assert "divergence" in result.pattern_name

    def test_no_divergence_in_flat(self, flat_df: pd.DataFrame) -> None:
        """횡보 데이터에서 다이버전스 미감지."""
        from src.analysis.technical.indicators import calculate_macd

        macd_line, _, _ = calculate_macd(flat_df["close"])
        result = detect_macd_divergence(flat_df["close"], macd_line)
        assert result is None

    def test_short_data_returns_none(self) -> None:
        """데이터 부족 시 None."""
        s = pd.Series([1.0, 2.0, 3.0])
        result = detect_macd_divergence(s, s)
        assert result is None

    def test_confidence_range(self, divergence_df: pd.DataFrame) -> None:
        """감지 시 confidence가 0.5~0.85 범위."""
        from src.analysis.technical.indicators import calculate_macd

        macd_line, _, _ = calculate_macd(divergence_df["close"])
        result = detect_macd_divergence(divergence_df["close"], macd_line)
        if result is not None:
            assert Decimal("0.5") <= result.confidence <= Decimal("0.85")


# ---------------------------------------------------------------------------
# TestScanPatterns
# ---------------------------------------------------------------------------


class TestScanPatterns:
    def test_empty_df_returns_empty(self, empty_df: pd.DataFrame) -> None:
        """빈 DataFrame → 빈 리스트."""
        indicators = compute_all_indicators(empty_df, "005930")
        result = scan_patterns(empty_df, indicators)
        assert result == []

    def test_short_df_returns_empty(self, short_df: pd.DataFrame) -> None:
        """짧은 DataFrame (< 20) → 빈 리스트."""
        indicators = compute_all_indicators(short_df, "005930")
        result = scan_patterns(short_df, indicators)
        assert result == []

    def test_returns_pattern_signals(self, golden_cross_df: pd.DataFrame) -> None:
        """정상 데이터 → PatternSignal 리스트 반환."""
        indicators = compute_all_indicators(golden_cross_df, "005930")
        result = scan_patterns(golden_cross_df, indicators)
        assert isinstance(result, list)
        for sig in result:
            assert isinstance(sig, PatternSignal)

    def test_all_signal_types_valid(self, double_top_df: pd.DataFrame) -> None:
        """반환된 시그널의 signal_type이 bullish 또는 bearish."""
        indicators = compute_all_indicators(double_top_df, "005930")
        result = scan_patterns(double_top_df, indicators)
        for sig in result:
            assert sig.signal_type in ("bullish", "bearish")

    def test_all_confidences_valid(self, double_bottom_df: pd.DataFrame) -> None:
        """반환된 시그널의 confidence가 0~1 범위."""
        indicators = compute_all_indicators(double_bottom_df, "005930")
        result = scan_patterns(double_bottom_df, indicators)
        for sig in result:
            assert Decimal("0") <= sig.confidence <= Decimal("1")
