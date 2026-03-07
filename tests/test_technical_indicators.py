"""기술적 분석 지표 계산 테스트.

실제 ta 라이브러리를 사용한 통합 테스트 (순수 계산이므로 mock 불필요).
"""

import numpy as np
import pandas as pd
import pytest

from src.analysis.technical.indicators import (
    calculate_bollinger_bands,
    calculate_ema,
    calculate_macd,
    calculate_rsi,
    calculate_sma,
    calculate_stochastic,
    calculate_volume_sma,
    compute_all_indicators,
)
from src.core.models import TechnicalIndicators


@pytest.fixture()
def ohlcv_df() -> pd.DataFrame:
    """150일 분량의 합성 OHLCV DataFrame."""
    np.random.seed(42)
    n = 150
    # 랜덤워크 기반 합성 가격 데이터
    base_price = 50000.0
    returns = np.random.normal(0.001, 0.02, n)
    close = base_price * np.cumprod(1 + returns)
    high = close * (1 + np.abs(np.random.normal(0, 0.01, n)))
    low = close * (1 - np.abs(np.random.normal(0, 0.01, n)))
    open_ = close * (1 + np.random.normal(0, 0.005, n))
    volume = np.random.randint(100_000, 10_000_000, n).astype(float)

    dates = pd.date_range("2025-06-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )


@pytest.fixture()
def short_df() -> pd.DataFrame:
    """5일 분량의 짧은 OHLCV DataFrame."""
    n = 5
    dates = pd.date_range("2025-06-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": [100.0, 102.0, 101.0, 103.0, 104.0],
            "high": [103.0, 105.0, 104.0, 106.0, 107.0],
            "low": [99.0, 100.0, 99.0, 101.0, 102.0],
            "close": [101.0, 103.0, 100.0, 105.0, 106.0],
            "volume": [1000.0, 1200.0, 800.0, 1500.0, 1100.0],
        },
        index=dates,
    )


class TestCalculateSMA:
    def test_periods(self, ohlcv_df: pd.DataFrame) -> None:
        """4개 기간(5,20,60,120) SMA 반환, 길이 일치."""
        result = calculate_sma(ohlcv_df["close"])
        assert set(result.keys()) == {5, 20, 60, 120}
        for period, series in result.items():
            assert len(series) == len(ohlcv_df), f"SMA {period} 길이 불일치"


class TestCalculateEMA:
    def test_periods(self, ohlcv_df: pd.DataFrame) -> None:
        """2개 기간(12,26) EMA 반환."""
        result = calculate_ema(ohlcv_df["close"])
        assert set(result.keys()) == {12, 26}
        for period, series in result.items():
            assert len(series) == len(ohlcv_df), f"EMA {period} 길이 불일치"


class TestCalculateMACD:
    def test_returns_three_series(self, ohlcv_df: pd.DataFrame) -> None:
        """line, signal, histogram 3개 시리즈 반환."""
        line, signal, histogram = calculate_macd(ohlcv_df["close"])
        assert len(line) == len(ohlcv_df)
        assert len(signal) == len(ohlcv_df)
        assert len(histogram) == len(ohlcv_df)


class TestCalculateRSI:
    def test_range(self, ohlcv_df: pd.DataFrame) -> None:
        """RSI 값이 0~100 범위."""
        rsi = calculate_rsi(ohlcv_df["close"])
        valid = rsi.dropna()
        assert not valid.empty
        assert valid.min() >= 0
        assert valid.max() <= 100


class TestCalculateStochastic:
    def test_returns_k_d(self, ohlcv_df: pd.DataFrame) -> None:
        """K, D 반환, 0~100 범위."""
        k, d = calculate_stochastic(
            ohlcv_df["high"], ohlcv_df["low"], ohlcv_df["close"]
        )
        assert len(k) == len(ohlcv_df)
        assert len(d) == len(ohlcv_df)
        valid_k = k.dropna()
        assert valid_k.min() >= 0
        assert valid_k.max() <= 100


class TestCalculateBollingerBands:
    def test_band_ordering(self, ohlcv_df: pd.DataFrame) -> None:
        """upper > middle > lower 관계."""
        upper, middle, lower = calculate_bollinger_bands(ohlcv_df["close"])
        # NaN이 아닌 구간에서 비교
        mask = upper.notna() & middle.notna() & lower.notna()
        assert (upper[mask] >= middle[mask]).all()
        assert (middle[mask] >= lower[mask]).all()


class TestCalculateVolumeSMA:
    def test_length(self, ohlcv_df: pd.DataFrame) -> None:
        """거래량 SMA 길이 일치."""
        result = calculate_volume_sma(ohlcv_df["volume"])
        assert len(result) == len(ohlcv_df)


class TestComputeAllIndicators:
    def test_returns_model(self, ohlcv_df: pd.DataFrame) -> None:
        """TechnicalIndicators 모델 반환, 모든 필드 채워짐."""
        result = compute_all_indicators(ohlcv_df, "005930")
        assert isinstance(result, TechnicalIndicators)
        assert result.symbol == "005930"
        # 시계열 필드가 비어있지 않은지 확인
        assert len(result.sma_5) == len(ohlcv_df)
        assert len(result.sma_20) == len(ohlcv_df)
        assert len(result.sma_60) == len(ohlcv_df)
        assert len(result.sma_120) == len(ohlcv_df)
        assert len(result.ema_12) == len(ohlcv_df)
        assert len(result.ema_26) == len(ohlcv_df)
        assert len(result.macd_line) == len(ohlcv_df)
        assert len(result.rsi_14) == len(ohlcv_df)
        assert len(result.stoch_k) == len(ohlcv_df)
        assert len(result.bb_upper) == len(ohlcv_df)
        assert len(result.volume_sma_20) == len(ohlcv_df)

    def test_latest_snapshots(self, ohlcv_df: pd.DataFrame) -> None:
        """latest_* 4개 값이 None이 아닌지."""
        result = compute_all_indicators(ohlcv_df, "005930")
        assert result.latest_rsi is not None
        assert result.latest_macd_histogram is not None
        assert result.latest_stoch_k is not None
        assert result.latest_bb_position is not None

    def test_nan_to_none_conversion(self, ohlcv_df: pd.DataFrame) -> None:
        """NaN이 None으로 변환되는지 (SMA 120의 초반부는 None이어야 함)."""
        result = compute_all_indicators(ohlcv_df, "005930")
        # SMA 120은 처음 119개가 None
        assert result.sma_120[0] is None
        # 마지막 값은 유효해야 함
        assert result.sma_120[-1] is not None

    def test_empty_dataframe(self) -> None:
        """빈 DataFrame 입력 시 빈 리스트 반환."""
        empty_df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        result = compute_all_indicators(empty_df, "005930")
        assert isinstance(result, TechnicalIndicators)
        assert result.sma_5 == []
        assert result.rsi_14 == []
        assert result.latest_rsi is None

    def test_short_dataframe(self, short_df: pd.DataFrame) -> None:
        """데이터 부족 시 (5일) 일부 지표가 None."""
        result = compute_all_indicators(short_df, "005930")
        assert isinstance(result, TechnicalIndicators)
        assert len(result.sma_5) == 5
        # RSI 14는 5일 데이터로는 대부분 NaN
        none_count = sum(1 for v in result.rsi_14 if v is None)
        assert none_count > 0
        # SMA 120은 5일로 전부 None
        assert all(v is None for v in result.sma_120)
