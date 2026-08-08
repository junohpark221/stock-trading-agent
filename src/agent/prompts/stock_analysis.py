"""Stock Analyst 프롬프트 템플릿.

개별 종목의 기술적/펀더멘털/감성 분석을 종합하는 Stock Analyst 에이전트용.
하이브리드 감성분석: 키워드 1차 분석 결과가 애매하면 뉴스 본문을 프롬프트에 포함하여
LLM이 key_factors/risks/reasoning에 반영하도록 한다.
"""

from __future__ import annotations

import json
from typing import Any

from src.agent.prompts.holding_context import close_price, render_holding_section


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

### 수급 (투자자별 순매수 — 보조 신호)
- **지속성만 유의미**: 연속 스트릭·다중 윈도(5/20/60일)가 일관될 때만 신호로 취급하고,
  1~2일 단발 순매수/순매도는 잡음으로 무시하세요.
- 수급은 **보조 신호**입니다 — 수급 단독으로는 매수 근거가 될 수 없습니다.
  외국인·기관 동반 순매도가 지속되면 `risks`에 반영하세요.
- `scrt`(금융투자)는 caveat대로 방향성 단독 판독 금지, `orgn`(기관합계)과 합산 금지
  (이중계상 — 분리 표기 전용).
- `days_covered`가 `days_in_window`보다 작으면 결측 구간이 있다는 뜻 — 확신도를 낮추세요.

### 뉴스/감성 (비대칭 원칙 — 반드시 준수)
감성은 **매수 가점이 아니라 리스크 신호**로 다룹니다. 긍정과 부정을 대칭으로 취급하지 마세요.
- **악재/부정 감성**: 발견되면 반드시 confidence를 낮추고 `risks`에 명시하세요. (누락 금지)
- **호재/긍정 감성**: **단독으로는 매수 근거가 될 수 없습니다.**
  실적 서프라이즈·수주·인수·규제 승인 등 구체적 촉매가 데이터(펀더멘털/뉴스 본문)로
  확인될 때만 confidence에 소폭 반영하세요. 막연한 테마·기대감·투자심리·"분위기"만으로는
  confidence를 올리지 말고 key_factors/매수 근거에서 제외하세요.
- "중요 뉴스 심층 분석" 섹션이 있으면 그 내용을 위 원칙에 따라
  key_factors, risks, reasoning에 반영하세요.

## 보유 종목 추가매수 판단 (PRJ-04)
"이 종목의 보유 현황" 섹션이 **보유 중**이면, `buy`는 신규 진입이 아니라
**기존 포지션에 병합되는 추가매수**입니다. 병합은 평균단가를 바꾸고, 보유 시계
(진입일·시간손절)를 리셋하며, 손절가·익절가를 새 평균단가 기준으로 재산정합니다.

- 물타기(평가손실 중)·불타기(평가이익 중) **둘 다 가능하지만, 신규 진입보다 높은 기준**을
  적용하세요. 아래 3가지를 **모두** 충족할 때만 `buy`, 하나라도 불충족이면 `hold`입니다.
  1. 진입 가설(보유 현황의 "진입 가설")이 현재 데이터로도 여전히 유효하다
  2. 진입 이후 새로 확인된 촉매가 데이터(펀더멘털·뉴스·수급)로 뒷받침된다
  3. 같은 조건의 신규 진입이라면 매수했을 수준보다 confidence가 높다
- **"평균단가를 낮춘다"는 것 자체는 매수 근거가 아닙니다.** 평가손실 중 추가매수는 가설이
  유효하다는 적극적 증거가 있을 때만 하고, 그 근거를 `key_factors`에 명시하세요.
- 보유 현황에 부분익절 러너 경고(⚠️)가 있으면, 추가매수로 트레일링 보호가 해제되는 것을
  리스크로 계산에 넣고 `risks`에 명시하세요.
- 청산은 별도 규칙 엔진이 담당하므로 보유 종목에 대한 `sell` 판단은 이 파이프라인에서
  사용되지 않습니다. 보유 종목의 실질 선택지는 **추가매수(`buy`) / 유지(`hold`)** 입니다.
- 보유 현황이 **미조회**이면 보유 여부를 단정하지 말고 confidence를 보수적으로 잡으세요.

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
- 감성은 비대칭으로 반영하세요: 악재 감성은 confidence를 낮추되, 긍정 감성은 구체적 촉매가
  확인되지 않으면 confidence를 올리지 마세요(위 "뉴스/감성" 원칙 참조).
- 수급 단독으로 confidence를 올리지 마세요(위 "수급" 원칙 참조).
- JSON 형식으로만 응답하세요.
"""


def build_user_prompt(data: dict[str, Any]) -> str:
    """기술/펀더멘털/감성/시장 데이터를 유저 프롬프트로 변환."""
    symbol = data.get("symbol", "UNKNOWN")
    name = data.get("name")
    # F-19: 종목명이 있으면 "종목명 (코드)"로 노출, 없으면 코드만.
    symbol_line = f"{name} ({symbol})" if name else symbol
    sections: list[str] = [f"## 종목 분석 요청: {symbol_line}\n"]

    # 현재가
    price_data = data.get("current_price")
    if price_data:
        sections.append("### 현재가")
        sections.append(f"```json\n{json.dumps(price_data, ensure_ascii=False, indent=2, default=str)}\n```\n")

    # 보유 현황 (PRJ-04 §8) — 신규 진입 / 추가매수를 구분해 판단하게 한다.
    sections.append(
        render_holding_section(
            data.get("holding_context"), current_price=close_price(data)
        )
    )

    # 기술적 지표
    technical = data.get("technical_indicators")
    if technical:
        sections.append("### 기술적 지표")
        sections.append(f"```json\n{json.dumps(technical, ensure_ascii=False, indent=2, default=str)}\n```\n")

    # 차트 패턴
    patterns = data.get("chart_patterns")
    if patterns:
        sections.append("### 차트 패턴")
        sections.append(f"```json\n{json.dumps(patterns, ensure_ascii=False, indent=2, default=str)}\n```\n")

    # 펀더멘털
    fundamental = data.get("fundamental_score")
    if fundamental:
        sections.append("### 펀더멘털 분석")
        sections.append(f"```json\n{json.dumps(fundamental, ensure_ascii=False, indent=2, default=str)}\n```\n")

    # 수급 요약 (excluded/error/데이터 없음이면 axes 부재 → 섹션 생략)
    flow = data.get("investor_flow")
    if flow and flow.get("axes"):
        sections.append("### 수급 요약 (투자자별 순매수, 5/20/60일 — 보조 신호)")
        sections.append(f"```json\n{json.dumps(flow, ensure_ascii=False, indent=2, default=str)}\n```\n")

    # 키워드 감성분석 결과
    sentiment = data.get("sentiment")
    if sentiment:
        sections.append("### 키워드 감성 분석 결과 (리스크 신호·촉매 확인용, 비대칭 반영)")
        sections.append(f"```json\n{json.dumps(sentiment, ensure_ascii=False, indent=2, default=str)}\n```\n")

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
        sections.append(f"```json\n{json.dumps(mc_summary, ensure_ascii=False, indent=2, default=str)}\n```\n")

    sections.append(
        f"위 데이터를 종합 분석하여 종목 {symbol_line}에 대한 StockAnalysis JSON을 출력하세요.\n"
        "**sentiment 필드는 반드시 null로 출력하세요.**"
    )

    return "\n".join(sections)
