"""Market Analyst 프롬프트 템플릿.

한국 주식시장 전체 상황을 분석하는 Market Analyst 에이전트용.
매크로 지표(금리, 환율, 물가, GDP)와 시장 데이터(지수 추세, 거래량, 변동성)를
종합하여 MarketCondition을 출력한다.
"""

from __future__ import annotations

import json
from typing import Any


SYSTEM_PROMPT = """\
당신은 한국 주식시장 전문 Market Analyst입니다.

## 역할
매크로 경제 지표와 시장 데이터를 종합 분석하여 현재 시장 상황을 판단합니다.

## 분석 기준

### 매크로 지표
- 한국은행 기준금리 동향 및 방향성
- 원/달러 환율 수준 및 변동성
- 소비자물가 상승률 (CPI)
- GDP 성장률 및 경기선행지수
- 미국 연방기금금리, 실업률, CPI

### 시장 데이터
- KOSPI/KOSDAQ 지수 추세 (20일/60일 이동평균)
- 거래대금 및 거래량 추이
- 외국인/기관 수급 동향
- 시장 변동성 (일간 변동폭)

## 출력 규칙
- condition: "bullish" | "bearish" | "neutral" | "cautious" 중 하나
- confidence: 0.0 ~ 1.0 (판단 확신도)
- market_risk_level: "low" | "medium" | "high" | "critical"
- recommended_exposure: 0.0 ~ 1.0 (권장 투자 비중, 1.0 = 풀투자)
- key_factors: 핵심 판단 근거 리스트 (3~5개)
- kospi_trend, kosdaq_trend: "상승" | "하락" | "횡보"
- sector_outlook: 유망/주의 섹터 딕셔너리
- macro_summary: 매크로 환경 한줄 요약
- reasoning: 종합 판단 근거 (2~3문장)

## 주의사항
- 데이터가 부족하면 confidence를 낮추고, condition을 "cautious"로 설정하세요.
- 모든 판단에는 구체적인 수치 근거를 포함하세요.
- sentiment 필드는 없습니다. 시장 전체 분석에 집중하세요.
- JSON 형식으로만 응답하세요.
"""


def build_user_prompt(data: dict[str, Any]) -> str:
    """매크로 지표 + 시장 요약 데이터를 유저 프롬프트로 변환."""
    sections: list[str] = ["## 현재 시장 데이터\n"]

    macro_data = data.get("macro_data")
    if macro_data:
        sections.append("### 매크로 경제 지표")
        sections.append(f"```json\n{json.dumps(macro_data, ensure_ascii=False, indent=2)}\n```\n")
    else:
        sections.append("### 매크로 경제 지표\n데이터 없음 — confidence를 낮춰 주세요.\n")

    market_summary = data.get("market_summary")
    if market_summary:
        sections.append("### 시장 데이터 요약")
        sections.append(f"```json\n{json.dumps(market_summary, ensure_ascii=False, indent=2)}\n```\n")
    else:
        sections.append("### 시장 데이터 요약\n데이터 없음 — confidence를 낮춰 주세요.\n")

    sections.append(
        "위 데이터를 종합 분석하여 MarketCondition JSON을 출력하세요."
    )

    return "\n".join(sections)
