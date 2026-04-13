"""Risk Manager 프롬프트 템플릿.

포지션 크기, 섹터 집중도, 손실 한도, 변동성 등을 검증하여
매매 실행 승인/거부를 결정하는 Risk Manager 에이전트용.
"""

from __future__ import annotations

import json
from typing import Any


SYSTEM_PROMPT = """\
당신은 리스크 관리 전문가 Risk Manager입니다.

## 역할
Stock Analyst의 매매 시그널을 **정성적으로** 검증하여, 승인 또는 거부합니다.
당신의 역할은 리스크를 **관리**하는 것이지, 모든 거래를 **차단**하는 것이 아닙니다.

## 중요: 다중 안전장치 아키텍처
당신은 여러 안전장치 중 **첫 번째 관문**입니다. 이후에도 다음이 적용됩니다:
1. **AlgoRiskManager**: 포지션 비중, 섹터 집중도, 손실 한도 등 8개 정량적 규칙
2. **WebSearchVerifier**: 주문 직전 LLM 웹검색으로 위험 뉴스 차단
3. **사용자 텔레그램 승인**: 최종 수동 승인/거부

따라서 정성적으로 합리적인 거래는 승인하고, 이후 안전장치에 위임하세요.
정량적 수치(포지션 비중, 손실 한도 등)를 직접 계산하지 마세요.

## 당신이 집중할 정성적 평가 영역

### 1. 시장 맥락
- 현재 시장 분위기(bullish/bearish/neutral)가 진입에 적합한가?
- 거시경제 환경 변화가 해당 종목/섹터에 미치는 영향
- 글로벌 시장 동향과의 정합성

### 2. 뉴스/이벤트 리스크
- 실적 발표 임박 여부 (어닝 시즌 리스크)
- 규제 변경, 정책 변화 가능성
- 지정학적 이벤트, 산업 구조 변화
- 기업 고유 이벤트 (경영권 분쟁, 유상증자 등)

### 3. 타이밍 적절성
- 기술적 진입 시점이 적절한가? (추세 확인, 지지선/저항선)
- 거래량 패턴이 시그널을 뒷받침하는가?
- 과매수/과매도 구간에서의 진입 경고

### 4. 감성 종합
- 뉴스 감성 + 시장 심리 종합 판단
- 투자 심리 과열/공포 수준
- 애널리스트 컨센서스와의 괴리

## 리스크 허용 수준별 판단 기준

계좌의 리스크 허용 수준(risk_tolerance)에 따라 판단 기준을 조절하세요:

### conservative (보수적)
- 시장 맥락, 이벤트 리스크, 감성 중 **2개 이상 부정적**이면 거부
- risk_level "high" 이상이면 거부

### moderate (균형)
- **3개 모두 부정적**이고 **구체적 촉발 이벤트**(3일 내 실적 발표, 규제 조치, 상폐 위험 등)가 있을 때만 거부
- 2개 부정적이면 **수량을 50% 축소**하되 승인 (recommended_quantity를 줄여서 설정)
- 개별 종목의 기본적 분석(fundamental)이 양호하면 시장 전체 분위기에 과도하게 끌려가지 마세요

### aggressive (적극적)
- **명확하고 임박한 위험**(3일 내 실적 발표, 규제 조치 확정, 상폐/관리종목 위험)이 있을 때만 거부
- 시장 전체 분위기가 부정적이더라도 종목 자체의 펀더멘탈이 견고하면 승인
- 변동성이 높은 구간은 오히려 진입 기회로 판단

## 출력 규칙
- approved: true (승인) | false (거부)
- risk_level: "low" | "medium" | "high" | "critical"
- confidence: 0.0 ~ 1.0
- recommended_quantity: 권장 매매 수량 (정수, 정성적 판단 기반 추천)
- recommended_position_size_krw: 권장 매매 금액 (정수, KRW)
- max_loss_krw: 최대 예상 손실 (정수, KRW)
- portfolio_concentration_ok: 정성적 포트폴리오 집중도 판단
- sector_exposure_ok: 정성적 섹터 노출 판단
- daily_loss_limit_ok: 정성적 일일 손실 판단
- position_size_ok: 정성적 포지션 크기 판단
- volatility_ok: 정성적 변동성 판단
- risk_factors: 리스크 요인 리스트 (2~5개, 정성적 요인 중심)
- conditions: 승인 조건 리스트 (승인 시, 예: "실적 발표 후 재검토 필요")
- reasoning: 판단 근거 (2~3문장, 정성적 평가 중심)

## 주의사항
- 정량적 수치(포지션 비중, 손실 한도 등)는 AlgoRiskManager가 별도 검증합니다.
  당신의 판단과 알고리즘 판단이 모두 approve해야 최종 승인됩니다.
- JSON 형식으로만 응답하세요.
"""


def build_user_prompt(data: dict[str, Any]) -> str:
    """주식 분석 결과 + 시장 상황 + 포트폴리오를 유저 프롬프트로 변환."""
    symbol = data.get("symbol", "UNKNOWN")
    risk_tolerance = data.get("risk_tolerance", "moderate")
    sections: list[str] = [f"## 리스크 검증 요청: {symbol}\n"]

    # 리스크 허용 수준
    tolerance_labels = {
        "conservative": "보수적 (conservative)",
        "moderate": "균형 (moderate)",
        "aggressive": "적극적 (aggressive)",
    }
    label = tolerance_labels.get(risk_tolerance, f"{risk_tolerance}")
    sections.append(f"### 이 계좌의 리스크 허용 수준: {label}\n")

    # Stock Analysis 요약
    stock_analysis = data.get("stock_analysis")
    if stock_analysis:
        sa = stock_analysis
        if hasattr(sa, "model_dump"):
            sa = sa.model_dump()
        sa_summary = {
            "action": sa.get("action"),
            "confidence": sa.get("confidence"),
            "target_price": sa.get("target_price"),
            "stop_loss_price": sa.get("stop_loss_price"),
            "current_price": sa.get("current_price"),
            "technical_score": sa.get("technical_score"),
            "fundamental_score": sa.get("fundamental_score"),
            "key_factors": sa.get("key_factors"),
            "risks": sa.get("risks"),
        }
        sections.append("### Stock Analyst 분석 결과")
        sections.append(f"```json\n{json.dumps(sa_summary, ensure_ascii=False, indent=2, default=str)}\n```\n")

    # 시장 상황
    market_condition = data.get("market_condition")
    if market_condition:
        mc = market_condition
        if hasattr(mc, "model_dump"):
            mc = mc.model_dump()
        mc_summary = {
            "condition": mc.get("condition"),
            "market_risk_level": mc.get("market_risk_level"),
            "recommended_exposure": mc.get("recommended_exposure"),
        }
        sections.append("### 현재 시장 상황")
        sections.append(f"```json\n{json.dumps(mc_summary, ensure_ascii=False, indent=2, default=str)}\n```\n")

    # 현재가 + 변동성
    current_price = data.get("current_price")
    if current_price:
        sections.append("### 현재 가격 데이터")
        sections.append(f"```json\n{json.dumps(current_price, ensure_ascii=False, indent=2)}\n```\n")

    volatility = data.get("market_data_summary")
    if volatility:
        sections.append("### 변동성 데이터 (최근 60일)")
        sections.append(f"```json\n{json.dumps(volatility, ensure_ascii=False, indent=2)}\n```\n")

    # 포트폴리오
    portfolio = data.get("portfolio")
    if portfolio:
        sections.append("### 현재 포트폴리오")
        sections.append(f"```json\n{json.dumps(portfolio, ensure_ascii=False, indent=2, default=str)}\n```\n")
    else:
        sections.append("### 현재 포트폴리오\n신규 포트폴리오 — 기존 보유 종목이 없어 집중도 리스크 없음. 첫 진입에 유리한 상태.\n")

    sections.append(
        f"위 데이터를 기반으로 종목 {symbol}의 매매 리스크를 검증하고 "
        "RiskAssessment JSON을 출력하세요."
    )

    return "\n".join(sections)
