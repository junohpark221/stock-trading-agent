"""Trader 프롬프트 템플릿.

최종 매매 결정을 내리는 Trader 에이전트용.
Risk Manager 승인을 받은 후 구체적인 주문 파라미터를 결정한다.
"""

from __future__ import annotations

import json
from typing import Any


SYSTEM_PROMPT = """\
당신은 최종 매매 결정을 내리는 Trader입니다.

## 역할
Stock Analyst의 분석과 Risk Manager의 승인을 바탕으로
구체적인 주문 파라미터(수량, 가격, 손절/익절)를 결정합니다.

## 결정 기준

### 리스크/수익 비율
- 최소 1.5:1 이상 유지
- risk_reward_ratio = (목표가 - 현재가) / (현재가 - 손절가)

### 주문 유형
- "limit": 지정가 주문 (기본)
- "market": 시장가 주문 (급등/급락 시)

### 가격 결정
- 매수: 현재가 또는 소폭 하회하는 지정가
- 매도: 현재가 또는 소폭 상회하는 지정가

### 손절/익절
- stop_loss_price: Risk Manager 권고 반영, 변동성 고려
- take_profit_price: 목표가의 80~100% 수준

## 출력 규칙
- action: "buy" | "sell" | "hold"
- confidence: 0.0 ~ 1.0
- order_type: "limit" | "market"
- quantity: 매매 수량 (정수, Risk Manager 권장 수량 이하)
- price: 주문 가격 (정수, KRW)
- stop_loss_price: 손절가 (정수, KRW)
- take_profit_price: 익절가 (정수, KRW)
- risk_reward_ratio: 리스크/수익 비율 (소수점 2자리)
- expected_return_pct: 기대 수익률 % (소수점 2자리)
- max_loss_pct: 최대 손실률 % (소수점 2자리)
- reasoning: 주문 결정 근거 (2~3문장)
- requires_approval: false (Risk Manager 이미 승인, 수동 승인 불필요)

## 주의사항
- Risk Manager의 recommended_quantity를 초과하지 마세요.
- Risk Manager가 조건을 제시했다면 해당 조건을 준수하세요.
- confidence가 0.5 미만이면 action을 "hold"로 변경하세요.
- JSON 형식으로만 응답하세요.
"""


def build_user_prompt(data: dict[str, Any]) -> str:
    """분석 결과 + 리스크 평가 + 시장 상황을 유저 프롬프트로 변환."""
    symbol = data.get("symbol", "UNKNOWN")
    name = data.get("name")
    # F-19: 종목명이 있으면 "종목명 (코드)"로 노출, 없으면 코드만.
    symbol_line = f"{name} ({symbol})" if name else symbol
    sections: list[str] = [f"## 최종 매매 결정 요청: {symbol_line}\n"]

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
        }
        sections.append("### Stock Analyst 분석")
        sections.append(f"```json\n{json.dumps(sa_summary, ensure_ascii=False, indent=2, default=str)}\n```\n")

    # Risk Assessment
    risk_assessment = data.get("risk_assessment")
    if risk_assessment:
        ra = risk_assessment
        if hasattr(ra, "model_dump"):
            ra = ra.model_dump()
        ra_summary = {
            "approved": ra.get("approved"),
            "risk_level": ra.get("risk_level"),
            "recommended_quantity": ra.get("recommended_quantity"),
            "recommended_position_size_krw": ra.get("recommended_position_size_krw"),
            "max_loss_krw": ra.get("max_loss_krw"),
            "conditions": ra.get("conditions"),
        }
        sections.append("### Risk Manager 평가")
        sections.append(f"```json\n{json.dumps(ra_summary, ensure_ascii=False, indent=2, default=str)}\n```\n")

    # 시장 상황
    market_condition = data.get("market_condition")
    if market_condition:
        mc = market_condition
        if hasattr(mc, "model_dump"):
            mc = mc.model_dump()
        mc_summary = {
            "condition": mc.get("condition"),
            "market_risk_level": mc.get("market_risk_level"),
        }
        sections.append("### 시장 상황")
        sections.append(f"```json\n{json.dumps(mc_summary, ensure_ascii=False, indent=2, default=str)}\n```\n")

    # 현재가
    current_price = data.get("current_price")
    if current_price:
        sections.append("### 현재가")
        sections.append(f"```json\n{json.dumps(current_price, ensure_ascii=False, indent=2)}\n```\n")

    sections.append(
        f"위 데이터를 기반으로 종목 {symbol_line}의 최종 매매 결정을 내리고 "
        "TradeDecision JSON을 출력하세요."
    )

    return "\n".join(sections)
