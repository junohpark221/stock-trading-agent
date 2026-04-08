"""기술적 분석 지표 계산 모듈.

DailyOHLCV DataFrame에서 ta 라이브러리를 사용하여 기술지표를 on-the-fly로 계산한다.
DB 저장 없이 TechnicalIndicators Pydantic 모델로 반환.
"""

from math import isnan

import pandas as pd
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import EMAIndicator, MACD, SMAIndicator
from ta.volatility import AverageTrueRange, BollingerBands

from src.core.models import TechnicalIndicators


def _series_to_list(s: pd.Series) -> list[float | None]:
    """pd.Series를 list로 변환하며 NaN을 None으로 치환."""
    return [None if pd.isna(v) else float(v) for v in s]


def _last_valid(s: pd.Series) -> float | None:
    """시리즈의 마지막 유효값(non-NaN)을 반환."""
    valid = s.dropna()
    if valid.empty:
        return None
    val = float(valid.iloc[-1])
    return None if isnan(val) else val


def calculate_sma(close: pd.Series, periods: list[int] | None = None) -> dict[int, pd.Series]:
    """
    SMA (Simple Moving Average) — 단순이동평균

    [매매 로직에서의 역할]
    - 추세 방향 판단의 기본 지표
    - 주가가 SMA 위: 상승 추세, 아래: 하락 추세
    - 단기 SMA가 장기 SMA를 상향 돌파: 골든크로스(매수 시그널)
    - 단기 SMA가 장기 SMA를 하향 돌파: 데드크로스(매도 시그널)

    [계산 방식]
    1. 최근 N일간 종가의 산술평균
    2. SMA(N) = (C₁ + C₂ + ... + Cₙ) / N
    3. 매일 가장 오래된 값을 빼고 새 값을 더해 슬라이딩

    [우리 전략에서의 활용]
    - SMA 5: 초단기 추세 (일주일 평균)
    - SMA 20: 단기 추세 (약 1개월)
    - SMA 60: 중기 추세 (약 3개월, 수급선)
    - SMA 120: 장기 추세 (약 6개월, 경기선)
    - Stock Analyst 에이전트가 골든/데드크로스와 지지/저항 분석에 활용

    Args:
        close: 종가 시계열 데이터
        periods: SMA 계산 기간 목록 (기본 [5, 20, 60, 120])

    Returns:
        {기간: SMA 시계열} 딕셔너리
    """
    if periods is None:
        periods = [5, 20, 60, 120]
    return {p: SMAIndicator(close=close, window=p).sma_indicator() for p in periods}


def calculate_ema(close: pd.Series, periods: list[int] | None = None) -> dict[int, pd.Series]:
    """
    EMA (Exponential Moving Average) — 지수이동평균

    [매매 로직에서의 역할]
    - SMA보다 최근 가격에 높은 가중치를 부여하여 추세 변화에 민감하게 반응
    - MACD 계산의 기반이 되는 핵심 지표 (EMA 12, EMA 26)
    - 빠른 EMA가 느린 EMA 위: 상승 모멘텀, 아래: 하락 모멘텀

    [계산 방식]
    1. 승수(multiplier) = 2 / (N + 1)
    2. EMA(today) = Close(today) × multiplier + EMA(yesterday) × (1 - multiplier)
    3. 첫 번째 EMA는 SMA로 초기화

    [우리 전략에서의 활용]
    - EMA 12/26: MACD 산출 기반
    - 단기 트레이딩에서 SMA 대비 빠른 시그널 포착
    - Stock Analyst 에이전트가 모멘텀 전환 감지에 활용

    Args:
        close: 종가 시계열 데이터
        periods: EMA 계산 기간 목록 (기본 [12, 26])

    Returns:
        {기간: EMA 시계열} 딕셔너리
    """
    if periods is None:
        periods = [12, 26]
    return {p: EMAIndicator(close=close, window=p).ema_indicator() for p in periods}


def calculate_macd(
    close: pd.Series,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    MACD (Moving Average Convergence Divergence) — 이동평균 수렴·확산

    [매매 로직에서의 역할]
    - 추세의 방향, 강도, 전환점을 동시에 파악하는 종합 지표
    - MACD 라인이 시그널 라인 상향 돌파: 매수 시그널
    - MACD 라인이 시그널 라인 하향 돌파: 매도 시그널
    - 히스토그램 양전환/음전환: 모멘텀 변화 조기 감지

    [계산 방식]
    1. MACD Line = EMA(12) - EMA(26)
    2. Signal Line = MACD Line의 EMA(9)
    3. Histogram = MACD Line - Signal Line

    [우리 전략에서의 활용]
    - 포지션 트레이딩: 히스토그램 방향 전환으로 진입/청산 타이밍 포착
    - 스윙 트레이딩: MACD-시그널 크로스오버 + RSI 확인으로 신뢰도 향상
    - latest_macd_histogram으로 현재 모멘텀 방향 스냅샷 제공

    Args:
        close: 종가 시계열 데이터

    Returns:
        (MACD line, Signal line, Histogram) 튜플
    """
    macd = MACD(close=close)
    return macd.macd(), macd.macd_signal(), macd.macd_diff()


def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """
    RSI (Relative Strength Index) — 상대강도지수

    [매매 로직에서의 역할]
    - 과매수/과매도 판단의 핵심 지표
    - RSI > 70: 과매수 → 매도 시그널 고려
    - RSI < 30: 과매도 → 매수 시그널 고려
    - 다이버전스: 가격은 신고가인데 RSI는 하락 → 추세 반전 경고

    [계산 방식]
    1. 가격 변화량(delta) = 현재가 - 전일가
    2. 상승분(gain) = max(delta, 0), 하락분(loss) = abs(min(delta, 0))
    3. 평균 상승(avg_gain) = gain의 period일 지수이동평균
    4. 평균 하락(avg_loss) = loss의 period일 지수이동평균
    5. RS = avg_gain / avg_loss
    6. RSI = 100 - (100 / (1 + RS))

    [우리 전략에서의 활용]
    - 포지션 트레이딩: RSI < 35 + 이동평균선 지지 → 매수 후보
    - 스윙 트레이딩: RSI < 25 → 단기 반등 매수, RSI > 75 → 단기 매도
    - Stock Analyst 에이전트가 이 값을 참조하여 LLM에 보고

    Args:
        close: 종가 시계열 데이터
        period: RSI 계산 기간 (기본 14일)

    Returns:
        RSI 시계열 (0~100)
    """
    return RSIIndicator(close=close, window=period).rsi()


def calculate_stochastic(
    high: pd.Series, low: pd.Series, close: pd.Series
) -> tuple[pd.Series, pd.Series]:
    """
    Stochastic Oscillator — 스토캐스틱 오실레이터

    [매매 로직에서의 역할]
    - 현재가가 최근 가격 범위에서 어느 위치인지 측정 (0~100)
    - %K > 80: 과매수 영역, %K < 20: 과매도 영역
    - %K가 %D를 상향 돌파(과매도 구간): 매수 시그널
    - %K가 %D를 하향 돌파(과매수 구간): 매도 시그널

    [계산 방식]
    1. %K = (현재 종가 - N일 최저가) / (N일 최고가 - N일 최저가) × 100
    2. %D = %K의 M일 이동평균 (시그널 라인)
    3. 기본: N=14, M=3

    [우리 전략에서의 활용]
    - RSI와 교차 확인하여 과매수/과매도 판단 신뢰도 향상
    - 횡보장에서 특히 유효한 단기 매매 시그널
    - latest_stoch_k로 현재 위치 스냅샷 제공

    Args:
        high: 고가 시계열 데이터
        low: 저가 시계열 데이터
        close: 종가 시계열 데이터

    Returns:
        (%K, %D) 튜플
    """
    stoch = StochasticOscillator(high=high, low=low, close=close)
    return stoch.stoch(), stoch.stoch_signal()


def calculate_bollinger_bands(
    close: pd.Series, period: int = 20
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Bollinger Bands — 볼린저 밴드

    [매매 로직에서의 역할]
    - 가격 변동성 기반 상/하한 밴드로 과매수/과매도 판단
    - 가격이 상단 밴드 돌파: 과매수 또는 강한 상승 모멘텀
    - 가격이 하단 밴드 돌파: 과매도 또는 강한 하락 모멘텀
    - 밴드 폭 축소(스퀴즈): 큰 변동성 폭발 임박 시그널

    [계산 방식]
    1. Middle Band = SMA(N) — N일 단순이동평균
    2. Upper Band = SMA(N) + 2 × σ(N) — 상단 밴드 (2 표준편차)
    3. Lower Band = SMA(N) - 2 × σ(N) — 하단 밴드 (2 표준편차)
    4. σ(N) = N일 종가의 표준편차

    [우리 전략에서의 활용]
    - BB Position = (종가 - 하단) / (상단 - 하단): 0~1 범위에서 현재 위치
    - BB Position < 0.2: 하단 근접 → 반등 매수 후보
    - BB Position > 0.8: 상단 근접 → 차익 실현 고려
    - latest_bb_position으로 현재 밴드 내 위치 스냅샷 제공

    Args:
        close: 종가 시계열 데이터
        period: 볼린저 밴드 계산 기간 (기본 20일)

    Returns:
        (Upper Band, Middle Band, Lower Band) 튜플
    """
    bb = BollingerBands(close=close, window=period)
    return bb.bollinger_hband(), bb.bollinger_mavg(), bb.bollinger_lband()


def calculate_volume_sma(volume: pd.Series, period: int = 20) -> pd.Series:
    """
    Volume SMA — 거래량 이동평균

    [매매 로직에서의 역할]
    - 평균 거래량 대비 현재 거래량으로 매매 관심도 측정
    - 거래량 > SMA: 평소보다 활발한 거래 → 추세 신뢰도 상승
    - 거래량 < SMA: 한산한 거래 → 추세 신뢰도 하락 또는 횡보

    [계산 방식]
    1. 최근 N일간 거래량의 산술평균
    2. Volume SMA(N) = (V₁ + V₂ + ... + Vₙ) / N

    [우리 전략에서의 활용]
    - 돌파 매매: 가격 돌파 + 거래량 > 2×SMA → 유효 돌파 판단
    - 추세 확인: 상승 추세에서 거래량 증가 동반 → 추세 지속 신뢰
    - Stock Analyst 에이전트가 거래량 이상 감지에 활용

    Args:
        volume: 거래량 시계열 데이터
        period: 거래량 SMA 기간 (기본 20일)

    Returns:
        거래량 SMA 시계열
    """
    return SMAIndicator(close=volume, window=period).sma_indicator()


def calculate_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """
    ATR (Average True Range) — 평균 진폭

    [매매 로직에서의 역할]
    - 갭(상한가/하한가)을 포함한 진정한 변동성 측정
    - 단순 종가 표준편차와 달리 일중 변동폭 + 갭을 모두 반영
    - 변동성 기반 손절폭 산정, 포지션 사이징, 변동성 게이트 등에 활용

    [계산 방식]
    1. True Range = max(high - low, |high - prev_close|, |low - prev_close|)
    2. ATR(N) = TR의 N일 지수이동평균(또는 단순이동평균)

    [우리 전략에서의 활용]
    - SwingTrading scan_universe에서 ATR/종가 비율로 정규화 변동성 계산
    - 단발 상한가가 표준편차를 부풀리는 문제를 완화

    Args:
        high: 고가 시계열
        low: 저가 시계열
        close: 종가 시계열
        period: ATR 계산 기간 (기본 14일)

    Returns:
        ATR 시계열
    """
    return AverageTrueRange(
        high=high, low=low, close=close, window=period
    ).average_true_range()


def compute_all_indicators(df: pd.DataFrame, symbol: str) -> TechnicalIndicators:
    """OHLCV DataFrame에서 모든 기술지표를 계산하여 TechnicalIndicators 반환.

    Args:
        df: columns=['open','high','low','close','volume'], DatetimeIndex 또는 date 컬럼
        symbol: 종목 코드

    Returns:
        TechnicalIndicators (각 지표 시계열 + latest 스냅샷)
    """
    if df.empty:
        return TechnicalIndicators(symbol=symbol)

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    # 이동평균 계산
    sma = calculate_sma(close)
    ema = calculate_ema(close)

    # MACD 계산
    macd_line, macd_signal, macd_histogram = calculate_macd(close)

    # 오실레이터 계산
    rsi_14 = calculate_rsi(close)
    stoch_k, stoch_d = calculate_stochastic(high, low, close)

    # 볼린저 밴드 계산
    bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(close)

    # 거래량 이동평균 계산
    vol_sma_20 = calculate_volume_sma(volume)

    # BB Position 계산: (종가 - 하단) / (상단 - 하단)
    bb_width = bb_upper - bb_lower
    bb_position = (close - bb_lower) / bb_width.replace(0, float("nan"))

    return TechnicalIndicators(
        symbol=symbol,
        # 이동평균
        sma_5=_series_to_list(sma[5]),
        sma_20=_series_to_list(sma[20]),
        sma_60=_series_to_list(sma[60]),
        sma_120=_series_to_list(sma[120]),
        ema_12=_series_to_list(ema[12]),
        ema_26=_series_to_list(ema[26]),
        # MACD
        macd_line=_series_to_list(macd_line),
        macd_signal=_series_to_list(macd_signal),
        macd_histogram=_series_to_list(macd_histogram),
        # 오실레이터
        rsi_14=_series_to_list(rsi_14),
        stoch_k=_series_to_list(stoch_k),
        stoch_d=_series_to_list(stoch_d),
        # 볼린저 밴드
        bb_upper=_series_to_list(bb_upper),
        bb_middle=_series_to_list(bb_middle),
        bb_lower=_series_to_list(bb_lower),
        # 거래량
        volume_sma_20=_series_to_list(vol_sma_20),
        # 최신값 스냅샷
        latest_rsi=_last_valid(rsi_14),
        latest_macd_histogram=_last_valid(macd_histogram),
        latest_stoch_k=_last_valid(stoch_k),
        latest_bb_position=_last_valid(bb_position),
    )
