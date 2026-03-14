# 매매 로직 & 차트 분석 통합 레퍼런스

> 이 문서는 프로젝트의 모든 매매 로직과 차트 분석 생성 로직을 한 눈에 볼 수 있도록 통합 관리하는 문서입니다.
> Phase 2~4 구현 시 각 섹션을 채워나갑니다.
>
> **관련 문서:** [DESIGN.md](../DESIGN.md)

---

## 목차

1. [기술적 분석 지표](#기술적-분석-지표) — Phase 2
2. [차트 패턴 감지](#차트-패턴-감지) — Phase 2
3. [펀더멘털 분석](#펀더멘털-분석) — Phase 2
4. [감성 분석](#감성-분석) — Phase 2
5. [포지션 트레이딩 전략](#포지션-트레이딩-전략) — Phase 4
6. [스윙 트레이딩 전략](#스윙-트레이딩-전략) — Phase 4
7. [리스크 관리 규칙](#리스크-관리-규칙) — Phase 4
8. [포지션 사이징](#포지션-사이징) — Phase 4
9. [손절/익절 로직](#손절익절-로직) — Phase 4

---

## 기술적 분석 지표
> `src/analysis/technical/indicators.py`

DB에 저장하지 않고 on-the-fly로 계산한다. `compute_all_indicators(df, symbol)` → `TechnicalIndicators` 반환.

### 추세 지표

| 지표 | 계산 | 임계값 | 활용 |
|------|------|--------|------|
| **SMA 5** | 5일 종가 산술평균 | — | 초단기 추세 (일주일) |
| **SMA 20** | 20일 종가 산술평균 | — | 단기 추세 (약 1개월) |
| **SMA 60** | 60일 종가 산술평균 | — | 중기 추세 (약 3개월, 수급선) |
| **SMA 120** | 120일 종가 산술평균 | — | 장기 추세 (약 6개월, 경기선) |
| **EMA 12/26** | 지수이동평균 (승수 = 2/(N+1)) | — | MACD 기반, 추세 변화 민감 반응 |
| **MACD** | EMA(12) - EMA(26), Signal = MACD의 EMA(9), Histogram = MACD - Signal | 히스토그램 양전환/음전환 | 추세 방향·강도·전환점 종합 판단 |

### 모멘텀 지표

| 지표 | 계산 | 임계값 | 활용 |
|------|------|--------|------|
| **RSI(14)** | 100 - 100/(1 + avg_gain/avg_loss), 14일 기준 | >70 과매수, <30 과매도 | 과매수/과매도 핵심 판단 |
| **Stochastic %K/%D** | %K = (종가-N일최저)/(N일최고-N일최저)×100, %D = %K의 3일 SMA | >80 과매수, <20 과매도 | RSI 교차확인, 횡보장 단기 매매 |

### 변동성 지표

| 지표 | 계산 | 활용 |
|------|------|------|
| **Bollinger Bands** | Middle = SMA(20), Upper/Lower = SMA(20) ± 2σ | 가격 변동성 기반 과매수/과매도 |
| **BB Position** | (종가 - Lower) / (Upper - Lower) | <0.2 하단 근접(매수), >0.8 상단 근접(매도) |

### 거래량 지표

| 지표 | 계산 | 활용 |
|------|------|------|
| **Volume SMA(20)** | 20일 거래량 산술평균 | 거래량 > 2×SMA → 유효 돌파 판단 |

### 최신값 스냅샷

`TechnicalIndicators`는 4개 최신값 스냅샷을 제공한다:
- `latest_rsi`: 최신 RSI (0~100)
- `latest_macd_histogram`: 최신 MACD 히스토그램 (양수=상승 모멘텀)
- `latest_stoch_k`: 최신 Stochastic %K
- `latest_bb_position`: 최신 BB Position (0~1)

---

## 차트 패턴 감지
> `src/analysis/technical/patterns.py`

`scan_patterns(df, indicators)` → `list[PatternSignal]` 반환. 감지된 패턴이 없으면 빈 리스트.

### 골든크로스 (Golden Cross)

- **정의:** 단기 SMA가 장기 SMA를 아래에서 위로 돌파
- **감지:** SMA5↔SMA20, SMA20↔SMA60 각각 최근 5일 내 부호 전환(음→양) 탐색
- **confidence:** 기본 0.6 + recency 보너스(최대 0.15) + gap 보너스(최대 0.15) → 최대 0.9
- **signal_type:** `bullish`

### 데드크로스 (Death Cross)

- **정의:** 단기 SMA가 장기 SMA를 위에서 아래로 돌파
- **감지:** 골든크로스의 역방향 (양→음 전환)
- **confidence:** 골든크로스와 동일한 계산 방식
- **signal_type:** `bearish`

### 더블탑 (Double Top) — M자 패턴

- **정의:** 비슷한 수준의 고점 2회 형성 후 하락 반전
- **감지:** 60일 윈도우 내 swing highs 2개, 가격 차이 2% 이내, 사이에 네크라인(저점) 존재
- **confidence:** 네크라인 돌파 시 0.7~0.9, 미돌파 시 0.5~0.6
- **signal_type:** `bearish`

### 더블바텀 (Double Bottom) — W자 패턴

- **정의:** 비슷한 수준의 저점 2회 형성 후 상승 반전
- **감지:** 더블탑의 역방향 (swing lows)
- **confidence:** 더블탑과 동일한 계산 방식
- **signal_type:** `bullish`

### MACD 다이버전스

- **베어리시:** 가격 고점↑ but MACD 고점↓ → 상승 모멘텀 약화
- **불리시:** 가격 저점↓ but MACD 저점↑ → 하락 모멘텀 약화
- **감지:** 60일 윈도우 내 스윙 포인트 매칭, 방향 불일치 확인
- **confidence:** 0.5 + strength (최대 0.85)
- **signal_type:** `bearish` 또는 `bullish`

### 지지/저항 레벨

- **감지:** 로컬 극값 클러스터링 (tolerance 1.5%), 2개 이상 모인 클러스터만 인정
- **시그널:** 현재가가 지지선/저항선의 2% 이내 → `near_support`(bullish) / `near_resistance`(bearish)

---

## 펀더멘털 분석
> `src/analysis/fundamental/analyzer.py`

`FundamentalAnalyzer(session_factory).analyze(symbol)` → `FundamentalScore` (0~100점) 반환.

### 점수 체계

| 축 | 비중 | 구성 |
|----|------|------|
| **밸류에이션 (valuation_score)** | 40% | PER 35% + PBR 30% + ROE 35% |
| **성장성 (growth_score)** | 30% | 매출 YoY 50% + 영업이익 YoY 50%, 3개년 시 CAGR 블렌딩(YoY 70% + CAGR 30%) |
| **수익성 (profitability_score)** | 30% | 영업이익률 60% + 순이익률 40% |

### PER 점수 구간

| 구간 | 점수 | 해석 |
|------|------|------|
| ≤ 0 (적자) | 20 | 주의 |
| 0 < PER ≤ 8 | 90 | 저평가 |
| 8 < PER ≤ 12 | 80 | 양호 |
| 12 < PER ≤ 15 | 65 | 적정 |
| 15 < PER ≤ 25 | 45 | 보통 |
| 25 < PER ≤ 40 | 25 | 고평가 |
| > 40 | 10 | 주의 |

### PBR 점수 구간

| 구간 | 점수 |
|------|------|
| ≤ 0 | 10 |
| 0 < PBR ≤ 0.7 | 90 |
| 0.7 < PBR ≤ 1.0 | 80 |
| 1.0 < PBR ≤ 1.5 | 65 |
| 1.5 < PBR ≤ 3.0 | 45 |
| > 3.0 | 20 |

### ROE 점수 구간

| 구간 | 점수 |
|------|------|
| ≤ 0 | 10 |
| 0 < ROE ≤ 5% | 35 |
| 5% < ROE ≤ 10% | 55 |
| 10% < ROE ≤ 15% | 75 |
| 15% < ROE ≤ 25% | 90 |
| > 25% | 95 |

### 성장률 점수 구간

| 구간 | 점수 |
|------|------|
| ≤ -15% | 10 |
| -15% < g ≤ -5% | 25 |
| -5% < g ≤ 0% | 40 |
| 0% < g ≤ 5% | 55 |
| 5% < g ≤ 10% | 65 |
| 10% < g ≤ 20% | 75 |
| 20% < g ≤ 30% | 85 |
| > 30% | 95 |

### 점수 해석

| 점수 범위 | 등급 | 의미 |
|-----------|------|------|
| 70 이상 | 우량 | 펀더멘털 양호, 매수 후보 |
| 50~70 | 보통 | 중립, 추가 분석 필요 |
| 50 미만 | 주의 | 펀더멘털 취약, 리스크 관리 강화 |

---

## 감성 분석
> `src/analysis/sentiment/analyzer.py`

Phase 3에서 LLM 기반으로 구현 예정. Phase 2에서는 뉴스 수집만 완료 (`NaverProvider` → `NewsArticle` 테이블).

감성분석 필드(`sentiment_score`, `sentiment_label`, `sentiment_method`)는 Phase 3에서 LLM이 채운다.

---

## 포지션 트레이딩 전략
> `src/strategy/position_trading.py`

*Phase 4 구현 시 작성 예정*

---

## 스윙 트레이딩 전략
> `src/strategy/swing_trading.py`

*Phase 4 구현 시 작성 예정*

---

## 리스크 관리 규칙
> `src/strategy/risk_manager.py`

*Phase 4 구현 시 작성 예정*

---

## 포지션 사이징
> `src/strategy/risk_manager.py`

*Phase 4 구현 시 작성 예정*

---

## 손절/익절 로직
> `src/strategy/risk_manager.py`

*Phase 4 구현 시 작성 예정*
