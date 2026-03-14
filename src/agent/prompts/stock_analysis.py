"""Stock Analyst 프롬프트 템플릿.

개별 종목의 기술적/펀더멘털/감성 분석을 종합하는 Stock Analyst 에이전트용.
하이브리드 감성분석: 키워드 1차 분석 결과가 애매하면 뉴스 본문을 프롬프트에 포함하여
LLM이 key_factors/risks/reasoning에 반영하도록 한다.
"""

from __future__ import annotations

import json
from typing import Any


SYSTEM_PROMPT = """\
당신은 한국 주식 종목 전문 Stock Analyst입니다.

## 역할
개별 종목의 기술적 분석, 펀더멘털 분석, 뉴스/감성 정보를 종합하여
매수/매도/보유 판단을 내립니다.

## 분석 항목

### 기술적 분석
- RSI (30 이하 과매도, 70 이상 과매수)
- MACD (시그널 교차, 히스토그램 방향)
- 볼린저 밴드 (%B 위치, 밴드폭)
- 이동평균선 (5/20/60/120일, 골든/데드크로스)
- 차트 패턴 (더블탑/바텀, 헤드앤숄더 등)

### 펀더멘털 분석
- PER, PBR (업종 평균 대비)
- ROE, ROA (수익성)
- 매출/영업이익 성장률
- 부채비율

### 뉴스/감성
- 키워드 감성 분석 결과가 제공됩니다.
- "중요 뉴스 심층 분석" 섹션이 있으면 뉴스 내용을 key_factors, risks, reasoning에 반영하세요.

## 출력 규칙
- action: "buy" | "sell" | "hold"
- confidence: 0.0 ~ 1.0
- technical_score: 0.0 ~ 100.0 (기술적 분석 점수)
- fundamental_score: 0.0 ~ 100.0 (펀더멘털 분석 점수)
- target_price: 목표가 (정수, KRW)
- stop_loss_price: 손절가 (정수, KRW)
- current_price: 현재가 (정수, KRW)
- key_factors: 핵심 판단 근거 리스트 (3~5개)
- risks: 리스크 요인 리스트 (2~3개)
- reasoning: 종합 판단 근거 (3~5문장)
- **sentiment: 반드시 null로 출력하세요** (후처리에서 프로그래밍적으로 첨부됩니다)

## 주의사항
- 시장 상황(market_condition)을 고려하여 판단하세요.
  - bearish 시장에서는 매수 confidence를 낮추세요.
  - bullish 시장에서는 매도 confidence를 낮추세요.
- 기술적/펀더멘털 시그널이 상충하면 confidence를 낮추세요.
- JSON 형식으로만 응답하세요.
"""


def build_user_prompt(data: dict[str, Any]) -> str:
    """기술/펀더멘털/감성/시장 데이터를 유저 프롬프트로 변환."""
    symbol = data.get("symbol", "UNKNOWN")
    sections: list[str] = [f"## 종목 분석 요청: {symbol}\n"]

    # 현재가
    price_data = data.get("current_price")
    if price_data:
        sections.append("### 현재가")
        sections.append(f"```json\n{json.dumps(price_data, ensure_ascii=False, indent=2)}\n```\n")

    # 기술적 지표
    technical = data.get("technical_indicators")
    if technical:
        sections.append("### 기술적 지표")
        sections.append(f"```json\n{json.dumps(technical, ensure_ascii=False, indent=2)}\n```\n")

    # 차트 패턴
    patterns = data.get("chart_patterns")
    if patterns:
        sections.append("### 차트 패턴")
        sections.append(f"```json\n{json.dumps(patterns, ensure_ascii=False, indent=2)}\n```\n")

    # 펀더멘털
    fundamental = data.get("fundamental_score")
    if fundamental:
        sections.append("### 펀더멘털 분석")
        sections.append(f"```json\n{json.dumps(fundamental, ensure_ascii=False, indent=2)}\n```\n")

    # 키워드 감성분석 결과
    sentiment = data.get("sentiment")
    if sentiment:
        sections.append("### 키워드 감성 분석 결과 (참고용)")
        sections.append(f"```json\n{json.dumps(sentiment, ensure_ascii=False, indent=2)}\n```\n")

    # LLM 심층 분석용 뉴스 (needs_llm_analysis=True일 때만)
    news_articles = data.get("news_articles")
    if news_articles:
        sections.append("### 🔍 중요 뉴스 심층 분석")
        sections.append(
            "아래 뉴스를 분석하여 key_factors, risks, reasoning에 반영하세요.\n"
        )
        for i, article in enumerate(news_articles[:10], 1):
            title = article.get("title", "제목 없음")
            summary = article.get("summary", article.get("description", ""))
            source = article.get("source", "")
            date = article.get("published_at", article.get("date", ""))
            sections.append(f"{i}. **{title}** ({source}, {date})")
            if summary:
                sections.append(f"   {summary[:300]}")
        sections.append("")

    # 시장 상황
    market_condition = data.get("market_condition")
    if market_condition:
        mc = market_condition
        if hasattr(mc, "model_dump"):
            mc = mc.model_dump()
        sections.append("### 현재 시장 상황")
        mc_summary = {
            "condition": mc.get("condition"),
            "confidence": mc.get("confidence"),
            "market_risk_level": mc.get("market_risk_level"),
            "recommended_exposure": mc.get("recommended_exposure"),
            "key_factors": mc.get("key_factors"),
        }
        sections.append(f"```json\n{json.dumps(mc_summary, ensure_ascii=False, indent=2)}\n```\n")

    sections.append(
        f"위 데이터를 종합 분석하여 종목 {symbol}에 대한 StockAnalysis JSON을 출력하세요.\n"
        "**sentiment 필드는 반드시 null로 출력하세요.**"
    )

    return "\n".join(sections)
