"""차트 패턴 감지 모듈.

TechnicalIndicators 기반으로 차트 패턴을 감지하여 PatternSignal 리스트를 반환한다.
감지된 패턴은 Phase 3 LLM 에이전트가 매매 판단에 활용할 핵심 입력 데이터.
"""

from decimal import Decimal

import pandas as pd

from src.core.models import PatternSignal, TechnicalIndicators

# ---------------------------------------------------------------------------
# 헬퍼 함수 (module-private)
# ---------------------------------------------------------------------------


def _list_to_series(data: list[float | None], index: pd.Index) -> pd.Series:
    """TechnicalIndicators의 list 필드를 pd.Series로 변환 (None → NaN)."""
    return pd.Series(data, index=index, dtype=float)


def _find_local_extrema(
    series: pd.Series, order: int = 5
) -> tuple[list[int], list[int]]:
    """로컬 최고/최저점 인덱스 반환.

    order개 이웃보다 큰/작은 점을 탐색한다.

    Args:
        series: 분석 대상 시계열
        order: 양쪽으로 비교할 이웃 수

    Returns:
        (고점 인덱스 리스트, 저점 인덱스 리스트)
    """
    highs: list[int] = []
    lows: list[int] = []
    values = series.values
    n = len(values)

    for i in range(order, n - order):
        if pd.isna(values[i]):
            continue

        is_high = True
        is_low = True
        for j in range(1, order + 1):
            left = values[i - j]
            right = values[i + j]
            if pd.isna(left) or pd.isna(right):
                is_high = False
                is_low = False
                break
            if values[i] <= left or values[i] <= right:
                is_high = False
            if values[i] >= left or values[i] >= right:
                is_low = False

        if is_high:
            highs.append(i)
        if is_low:
            lows.append(i)

    return highs, lows


def _find_swing_points(
    series: pd.Series, order: int = 5
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """(highs, lows) — 각각 (위치, 값) 튜플 리스트.

    Args:
        series: 분석 대상 시계열
        order: 양쪽으로 비교할 이웃 수

    Returns:
        (고점 (위치, 값) 리스트, 저점 (위치, 값) 리스트)
    """
    high_idx, low_idx = _find_local_extrema(series, order)
    values = series.values
    highs = [(i, float(values[i])) for i in high_idx]
    lows = [(i, float(values[i])) for i in low_idx]
    return highs, lows


# ---------------------------------------------------------------------------
# 공개 감지 함수
# ---------------------------------------------------------------------------


def detect_golden_cross(
    sma_short: pd.Series, sma_long: pd.Series, lookback: int = 5
) -> PatternSignal | None:
    """
    골든크로스 (Golden Cross) 감지

    [패턴 정의]
    - 단기 이동평균선이 장기 이동평균선을 아래에서 위로 돌파하는 현상
    - 상승 추세 전환의 대표적 기술적 신호

    [매매 로직에서의 역할]
    - 중장기 매수 시그널로, 추세 추종 전략의 핵심 진입 조건
    - RSI, 거래량 등 보조 지표와 함께 사용하면 신뢰도 향상
    - 데드크로스 대비 후행성이 있으나 추세 확인에 유리

    [우리 전략에서의 활용]
    - SMA5 vs SMA20: 단기 골든크로스 → 스윙 매수 진입
    - SMA20 vs SMA60: 중기 골든크로스 → 포지션 매수 진입
    - Stock Analyst 에이전트가 크로스 발생 시 LLM에 보고

    Args:
        sma_short: 단기 이동평균 시계열
        sma_long: 장기 이동평균 시계열
        lookback: 크로스 탐색 윈도우 (기본 5일)

    Returns:
        감지 시 PatternSignal, 미감지 시 None
    """
    diff = sma_short - sma_long
    valid = diff.dropna()
    if len(valid) < lookback + 1:
        return None

    recent = valid.iloc[-(lookback + 1) :]

    # lookback 범위 내에서 부호 전환(음→양) 탐색
    for i in range(1, len(recent)):
        if recent.iloc[i - 1] <= 0 < recent.iloc[i]:
            # recency 보너스: 최근일수록 높은 confidence
            recency = (i / len(recent)) * 0.15
            # gap widening 보너스: 크로스 이후 벌어지는 정도
            gap = abs(float(recent.iloc[-1])) / max(
                abs(float(sma_long.dropna().iloc[-1])), 1e-10
            )
            gap_bonus = min(gap * 5, 0.15)
            confidence = min(0.6 + recency + gap_bonus, 0.9)
            return PatternSignal(
                pattern_name="golden_cross",
                signal_type="bullish",
                confidence=Decimal(str(round(confidence, 2))),
                description="단기 이동평균이 장기 이동평균을 상향 돌파 (골든크로스)",
            )

    return None


def detect_death_cross(
    sma_short: pd.Series, sma_long: pd.Series, lookback: int = 5
) -> PatternSignal | None:
    """
    데드크로스 (Death Cross) 감지

    [패턴 정의]
    - 단기 이동평균선이 장기 이동평균선을 위에서 아래로 돌파하는 현상
    - 하락 추세 전환의 대표적 기술적 신호

    [매매 로직에서의 역할]
    - 중장기 매도 시그널로, 보유 포지션 청산 또는 신규 매수 보류 조건
    - 골든크로스와 함께 추세 방향 판단의 기본 프레임워크 구성

    [우리 전략에서의 활용]
    - SMA5 vs SMA20: 단기 데드크로스 → 스윙 매도 또는 진입 보류
    - SMA20 vs SMA60: 중기 데드크로스 → 포지션 청산 고려
    - Stock Analyst 에이전트가 크로스 발생 시 LLM에 보고

    Args:
        sma_short: 단기 이동평균 시계열
        sma_long: 장기 이동평균 시계열
        lookback: 크로스 탐색 윈도우 (기본 5일)

    Returns:
        감지 시 PatternSignal, 미감지 시 None
    """
    diff = sma_short - sma_long
    valid = diff.dropna()
    if len(valid) < lookback + 1:
        return None

    recent = valid.iloc[-(lookback + 1) :]

    for i in range(1, len(recent)):
        if recent.iloc[i - 1] >= 0 > recent.iloc[i]:
            recency = (i / len(recent)) * 0.15
            gap = abs(float(recent.iloc[-1])) / max(
                abs(float(sma_long.dropna().iloc[-1])), 1e-10
            )
            gap_bonus = min(gap * 5, 0.15)
            confidence = min(0.6 + recency + gap_bonus, 0.9)
            return PatternSignal(
                pattern_name="death_cross",
                signal_type="bearish",
                confidence=Decimal(str(round(confidence, 2))),
                description="단기 이동평균이 장기 이동평균을 하향 돌파 (데드크로스)",
            )

    return None


def detect_support_resistance(
    prices: pd.Series, window: int = 5
) -> dict[str, list[float]]:
    """
    지지/저항 레벨 감지

    [패턴 정의]
    - 지지선: 가격이 반복적으로 하락을 멈추고 반등하는 가격대
    - 저항선: 가격이 반복적으로 상승을 멈추고 하락하는 가격대
    - 로컬 극값 클러스터링으로 의미 있는 가격대를 추출

    [매매 로직에서의 역할]
    - 매수 타이밍: 가격이 지지선 근처에서 반등 신호 시 진입
    - 매도 타이밍: 가격이 저항선에 근접하면 부분/전량 매도 고려
    - 돌파 매매: 저항선 돌파 시 추세 가속 → 추가 매수

    [우리 전략에서의 활용]
    - scan_patterns에서 현재가와 레벨 간 거리를 계산해 near_support/near_resistance 시그널 생성
    - Stock Analyst 에이전트가 목표가/손절가 설정에 활용

    Args:
        prices: 종가 시계열
        window: 로컬 극값 탐색 윈도우 (기본 5)

    Returns:
        {"support": [가격대 리스트], "resistance": [가격대 리스트]}
    """
    valid = prices.dropna()
    if len(valid) < window * 2 + 1:
        return {"support": [], "resistance": []}

    swing_highs, swing_lows = _find_swing_points(valid, order=window)

    def _cluster(points: list[tuple[int, float]], tolerance: float = 0.015) -> list[float]:
        """가격대를 tolerance 이내로 클러스터링하여 대표 레벨 반환."""
        if not points:
            return []
        values = sorted(v for _, v in points)
        clusters: list[list[float]] = [[values[0]]]
        for v in values[1:]:
            if abs(v - clusters[-1][-1]) / max(abs(clusters[-1][-1]), 1e-10) <= tolerance:
                clusters[-1].append(v)
            else:
                clusters.append([v])
        # 2개 이상 모인 클러스터만 의미있는 레벨로 인정
        levels = [round(sum(c) / len(c), 2) for c in clusters if len(c) >= 2]
        # 단일 점이어도 최소 하나의 레벨은 반환
        if not levels and clusters:
            levels = [round(sum(clusters[0]) / len(clusters[0]), 2)]
        return levels

    return {
        "support": _cluster(swing_lows),
        "resistance": _cluster(swing_highs),
    }


def detect_double_top(
    prices: pd.Series, window: int = 60, tolerance: float = 0.02
) -> PatternSignal | None:
    """
    더블탑 (Double Top) — M자 패턴 감지

    [패턴 정의]
    - 가격이 비슷한 수준의 고점을 2번 형성한 후 하락하는 반전 패턴
    - 두 고점 사이에 저점(네크라인)이 존재하며, 네크라인 하향 돌파 시 패턴 완성
    - 상승 추세의 종료를 알리는 대표적 베어리시 반전 패턴

    [매매 로직에서의 역할]
    - 보유 포지션 청산 시그널
    - 네크라인 돌파 확인 후 매도 진입
    - 목표가: 네크라인 - (고점 - 네크라인)

    [우리 전략에서의 활용]
    - window(60일) 내 M자 형태 탐색
    - 두 고점의 가격 차이가 tolerance(2%) 이내일 때 인정
    - Stock Analyst 에이전트가 포지션 리스크 관리에 활용

    Args:
        prices: 종가 시계열
        window: 패턴 탐색 윈도우 (기본 60일)
        tolerance: 두 고점 가격 허용 오차 비율 (기본 2%)

    Returns:
        감지 시 PatternSignal, 미감지 시 None
    """
    valid = prices.dropna()
    if len(valid) < 20:
        return None

    recent = valid.iloc[-window:]
    swing_highs, swing_lows = _find_swing_points(recent, order=3)

    if len(swing_highs) < 2 or not swing_lows:
        return None

    # 최근 2개 고점 비교
    for i in range(len(swing_highs) - 1, 0, -1):
        h2_pos, h2_val = swing_highs[i]
        h1_pos, h1_val = swing_highs[i - 1]

        if h2_pos <= h1_pos:
            continue

        # 두 고점의 가격 차이가 tolerance 이내인지
        price_diff = abs(h2_val - h1_val) / max(h1_val, 1e-10)
        if price_diff > tolerance:
            continue

        # 두 고점 사이에 저점(네크라인)이 있는지
        neckline_candidates = [
            (p, v) for p, v in swing_lows if h1_pos < p < h2_pos
        ]
        if not neckline_candidates:
            continue

        neckline_val = min(v for _, v in neckline_candidates)
        current_price = float(recent.iloc[-1])

        # 네크라인 돌파 확인 (현재가가 네크라인 아래)
        if current_price < neckline_val:
            confidence = 0.7 + min((neckline_val - current_price) / neckline_val * 5, 0.2)
        else:
            # 네크라인 미돌파 — 패턴 형성 중
            confidence = 0.5
            # 네크라인에 가까울수록 confidence 상승
            dist = (current_price - neckline_val) / max(neckline_val, 1e-10)
            if dist < 0.03:
                confidence += 0.1

        confidence = min(confidence, 0.9)
        return PatternSignal(
            pattern_name="double_top",
            signal_type="bearish",
            confidence=Decimal(str(round(confidence, 2))),
            description="M자 패턴 감지 — 두 고점 형성 후 하락 가능성",
        )

    return None


def detect_double_bottom(
    prices: pd.Series, window: int = 60, tolerance: float = 0.02
) -> PatternSignal | None:
    """
    더블바텀 (Double Bottom) — W자 패턴 감지

    [패턴 정의]
    - 가격이 비슷한 수준의 저점을 2번 형성한 후 상승하는 반전 패턴
    - 두 저점 사이에 고점(네크라인)이 존재하며, 네크라인 상향 돌파 시 패턴 완성
    - 하락 추세의 종료를 알리는 대표적 불리시 반전 패턴

    [매매 로직에서의 역할]
    - 매수 진입 시그널
    - 네크라인 돌파 확인 후 매수 진입
    - 목표가: 네크라인 + (네크라인 - 저점)

    [우리 전략에서의 활용]
    - window(60일) 내 W자 형태 탐색
    - 두 저점의 가격 차이가 tolerance(2%) 이내일 때 인정
    - Stock Analyst 에이전트가 매수 타이밍 판단에 활용

    Args:
        prices: 종가 시계열
        window: 패턴 탐색 윈도우 (기본 60일)
        tolerance: 두 저점 가격 허용 오차 비율 (기본 2%)

    Returns:
        감지 시 PatternSignal, 미감지 시 None
    """
    valid = prices.dropna()
    if len(valid) < 20:
        return None

    recent = valid.iloc[-window:]
    swing_highs, swing_lows = _find_swing_points(recent, order=3)

    if len(swing_lows) < 2 or not swing_highs:
        return None

    for i in range(len(swing_lows) - 1, 0, -1):
        l2_pos, l2_val = swing_lows[i]
        l1_pos, l1_val = swing_lows[i - 1]

        if l2_pos <= l1_pos:
            continue

        price_diff = abs(l2_val - l1_val) / max(l1_val, 1e-10)
        if price_diff > tolerance:
            continue

        neckline_candidates = [
            (p, v) for p, v in swing_highs if l1_pos < p < l2_pos
        ]
        if not neckline_candidates:
            continue

        neckline_val = max(v for _, v in neckline_candidates)
        current_price = float(recent.iloc[-1])

        if current_price > neckline_val:
            confidence = 0.7 + min((current_price - neckline_val) / neckline_val * 5, 0.2)
        else:
            confidence = 0.5
            dist = (neckline_val - current_price) / max(neckline_val, 1e-10)
            if dist < 0.03:
                confidence += 0.1

        confidence = min(confidence, 0.9)
        return PatternSignal(
            pattern_name="double_bottom",
            signal_type="bullish",
            confidence=Decimal(str(round(confidence, 2))),
            description="W자 패턴 감지 — 두 저점 형성 후 상승 가능성",
        )

    return None


def detect_macd_divergence(
    prices: pd.Series, macd: pd.Series, window: int = 60
) -> PatternSignal | None:
    """
    MACD 다이버전스 감지

    [패턴 정의]
    - 베어리시 다이버전스: 가격 고점↑ but MACD 고점↓ → 상승 모멘텀 약화
    - 불리시 다이버전스: 가격 저점↓ but MACD 저점↑ → 하락 모멘텀 약화
    - 추세 반전을 예고하는 선행 시그널

    [매매 로직에서의 역할]
    - 베어리시 다이버전스: 보유 포지션 부분 청산, 신규 매수 보류
    - 불리시 다이버전스: 바닥 형성 확인, 분할 매수 진입 고려
    - 다른 기술지표(RSI, BB)와 함께 확인 시 신뢰도 향상

    [우리 전략에서의 활용]
    - 가격과 MACD의 스윙 포인트를 비교하여 방향성 불일치 감지
    - Stock Analyst 에이전트가 추세 전환 경고에 활용

    Args:
        prices: 종가 시계열
        macd: MACD line 시계열
        window: 다이버전스 탐색 윈도우 (기본 60일)

    Returns:
        감지 시 PatternSignal, 미감지 시 None
    """
    valid_prices = prices.dropna()
    valid_macd = macd.dropna()

    # 공통 인덱스로 정렬
    common_idx = valid_prices.index.intersection(valid_macd.index)
    if len(common_idx) < 20:
        return None

    p = valid_prices.loc[common_idx].iloc[-window:]
    m = valid_macd.loc[common_idx].iloc[-window:]

    price_highs, price_lows = _find_swing_points(p, order=3)
    macd_highs, macd_lows = _find_swing_points(m, order=3)

    # 베어리시 다이버전스: 가격 고점 상승 + MACD 고점 하락
    if len(price_highs) >= 2 and len(macd_highs) >= 2:
        ph1, ph1_v = price_highs[-2]
        ph2, ph2_v = price_highs[-1]
        # MACD 고점 중 가격 고점에 가장 가까운 것 매칭
        mh_near_ph1 = _nearest_swing(macd_highs, ph1)
        mh_near_ph2 = _nearest_swing(macd_highs, ph2)

        if (
            mh_near_ph1 is not None
            and mh_near_ph2 is not None
            and ph2_v > ph1_v
            and mh_near_ph2[1] < mh_near_ph1[1]
        ):
            price_rise = (ph2_v - ph1_v) / max(ph1_v, 1e-10)
            macd_drop = (mh_near_ph1[1] - mh_near_ph2[1]) / max(
                abs(mh_near_ph1[1]), 1e-10
            )
            strength = min(price_rise + macd_drop, 0.35)
            confidence = min(0.5 + strength, 0.85)
            return PatternSignal(
                pattern_name="macd_bearish_divergence",
                signal_type="bearish",
                confidence=Decimal(str(round(confidence, 2))),
                description="가격 고점 상승 중 MACD 고점 하락 — 상승 모멘텀 약화",
            )

    # 불리시 다이버전스: 가격 저점 하락 + MACD 저점 상승
    if len(price_lows) >= 2 and len(macd_lows) >= 2:
        pl1, pl1_v = price_lows[-2]
        pl2, pl2_v = price_lows[-1]
        ml_near_pl1 = _nearest_swing(macd_lows, pl1)
        ml_near_pl2 = _nearest_swing(macd_lows, pl2)

        if (
            ml_near_pl1 is not None
            and ml_near_pl2 is not None
            and pl2_v < pl1_v
            and ml_near_pl2[1] > ml_near_pl1[1]
        ):
            price_drop = (pl1_v - pl2_v) / max(pl1_v, 1e-10)
            macd_rise = (ml_near_pl2[1] - ml_near_pl1[1]) / max(
                abs(ml_near_pl1[1]), 1e-10
            )
            strength = min(price_drop + macd_rise, 0.35)
            confidence = min(0.5 + strength, 0.85)
            return PatternSignal(
                pattern_name="macd_bullish_divergence",
                signal_type="bullish",
                confidence=Decimal(str(round(confidence, 2))),
                description="가격 저점 하락 중 MACD 저점 상승 — 하락 모멘텀 약화",
            )

    return None


def _nearest_swing(
    swings: list[tuple[int, float]], target_pos: int
) -> tuple[int, float] | None:
    """target_pos에 가장 가까운 스윙 포인트 반환."""
    if not swings:
        return None
    return min(swings, key=lambda s: abs(s[0] - target_pos))


# ---------------------------------------------------------------------------
# 오케스트레이터
# ---------------------------------------------------------------------------


def scan_patterns(
    df: pd.DataFrame, indicators: TechnicalIndicators
) -> list[PatternSignal]:
    """모든 차트 패턴 감지기를 실행하여 감지된 시그널 리스트 반환.

    Args:
        df: OHLCV DataFrame (columns: open, high, low, close, volume)
        indicators: compute_all_indicators()로 계산된 기술지표

    Returns:
        감지된 PatternSignal 리스트 (없으면 빈 리스트)
    """
    if df.empty or len(df) < 20:
        return []

    signals: list[PatternSignal] = []
    close = df["close"].astype(float)
    index = df.index

    # SMA 시리즈 변환
    sma_5 = _list_to_series(indicators.sma_5, index) if indicators.sma_5 else None
    sma_20 = _list_to_series(indicators.sma_20, index) if indicators.sma_20 else None
    sma_60 = _list_to_series(indicators.sma_60, index) if indicators.sma_60 else None
    macd_line = (
        _list_to_series(indicators.macd_line, index) if indicators.macd_line else None
    )

    # 1. 골든크로스 / 데드크로스 감지 (SMA5 vs SMA20, SMA20 vs SMA60)
    cross_pairs = []
    if sma_5 is not None and sma_20 is not None:
        cross_pairs.append((sma_5, sma_20))
    if sma_20 is not None and sma_60 is not None:
        cross_pairs.append((sma_20, sma_60))

    for short, long in cross_pairs:
        sig = detect_golden_cross(short, long)
        if sig is not None:
            signals.append(sig)
        sig = detect_death_cross(short, long)
        if sig is not None:
            signals.append(sig)

    # 2. 더블탑 / 더블바텀 감지
    sig = detect_double_top(close)
    if sig is not None:
        signals.append(sig)
    sig = detect_double_bottom(close)
    if sig is not None:
        signals.append(sig)

    # 3. MACD 다이버전스 감지
    if macd_line is not None:
        sig = detect_macd_divergence(close, macd_line)
        if sig is not None:
            signals.append(sig)

    # 4. 지지/저항 레벨 → 근접 시그널 생성
    levels = detect_support_resistance(close)
    current_price = float(close.iloc[-1])

    for sup in levels["support"]:
        dist = abs(current_price - sup) / max(sup, 1e-10)
        if dist < 0.02:
            signals.append(
                PatternSignal(
                    pattern_name="near_support",
                    signal_type="bullish",
                    confidence=Decimal(str(round(0.5 + (0.02 - dist) * 15, 2))),
                    description=f"현재가가 지지선({sup:,.0f})에 근접 — 반등 가능성",
                )
            )

    for res in levels["resistance"]:
        dist = abs(current_price - res) / max(res, 1e-10)
        if dist < 0.02:
            signals.append(
                PatternSignal(
                    pattern_name="near_resistance",
                    signal_type="bearish",
                    confidence=Decimal(str(round(0.5 + (0.02 - dist) * 15, 2))),
                    description=f"현재가가 저항선({res:,.0f})에 근접 — 돌파 또는 반락 주시",
                )
            )

    return signals
