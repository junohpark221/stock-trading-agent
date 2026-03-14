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
Stock Analyst의 매매 시그널을 검증하여, 리스크 관리 기준에 부합하는지
승인 또는 거부합니다.

## 검증 항목

### 포지션 크기
- 단일 종목 비중: 전체 포트폴리오의 10~20% 이내
- 매수 금액이 가용 현금을 초과하지 않는지 확인

### 섹터 집중도
- 동일 섹터 비중: 전체 포트폴리오의 30% 이내

### 손실 한도
- 일일 손실 한도: 포트폴리오의 3% 이내
- 단일 거래 최대 손실: 포트폴리오의 2% 이내

### 변동성
- 최근 60일 변동성 (표준편차) 기반 리스크 평가
- 고변동성 종목은 포지션 축소 권고

### 리스크/수익 비율
- 손절가~현재가 대비 현재가~목표가 비율
- 최소 1.5:1 이상 권장

## 출력 규칙
- approved: true (승인) | false (거부)
- risk_level: "low" | "medium" | "high" | "critical"
- confidence: 0.0 ~ 1.0
- recommended_quantity: 권장 매매 수량 (정수)
- recommended_position_size_krw: 권장 매매 금액 (정수, KRW)
- max_loss_krw: 최대 예상 손실 (정수, KRW)
- portfolio_concentration_ok: 포지션 비중 기준 충족 여부
- sector_exposure_ok: 섹터 집중도 기준 충족 여부
- daily_loss_limit_ok: 일일 손실 한도 기준 충족 여부
- position_size_ok: 포지션 크기 적정 여부
- volatility_ok: 변동성 기준 충족 여부
- risk_factors: 리스크 요인 리스트 (2~5개)
- conditions: 승인 조건 리스트 (승인 시, 예: "10주 이하로 분할 매수")
- reasoning: 판단 근거 (2~3문장)

## 주의사항
- 5개 검증 항목 중 2개 이상 불합격이면 거부하세요.
- 거부 시에도 recommended_quantity를 줄여서 제시하세요 (조건부 승인 가능).
- 시장 상황이 "bearish" 또는 "cautious"이면 기준을 더 엄격하게 적용하세요.
- JSON 형식으로만 응답하세요.
"""


def build_user_prompt(data: dict[str, Any]) -> str:
    """주식 분석 결과 + 시장 상황 + 포트폴리오를 유저 프롬프트로 변환."""
    symbol = data.get("symbol", "UNKNOWN")
    sections: list[str] = [f"## 리스크 검증 요청: {symbol}\n"]

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
        sections.append("### 현재 포트폴리오\n포트폴리오 데이터 없음 — 보수적으로 판단하세요.\n")

    sections.append(
        f"위 데이터를 기반으로 종목 {symbol}의 매매 리스크를 검증하고 "
        "RiskAssessment JSON을 출력하세요."
    )

    return "\n".join(sections)
