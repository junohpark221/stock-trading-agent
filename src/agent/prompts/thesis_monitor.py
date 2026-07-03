"""Thesis Monitor 프롬프트 템플릿 (F-11).

중장기 포지션의 **진입 가설(entry thesis)**이 현재 시점의 뉴스·공시·펀더멘털로
여전히 유효한지 판단한다. 자동 청산이 아니라 경보형이므로, 프롬프트는 **보수적**
으로 — 명백한 훼손 근거가 데이터로 확인될 때만 thesis_broken=true, confidence를
과대평가하지 말 것 — 지시한다.
"""

from __future__ import annotations

import json
from typing import Any


SYSTEM_PROMPT = """\
당신은 중장기 포지션의 투자 가설을 감시하는 Thesis Monitor입니다.

## 역할
종목을 **매수한 근거(진입 가설)**가 현재 시점의 뉴스·공시·펀더멘털로도
여전히 유효한지 판단합니다. 이것은 자동 매도가 아니라 **사람에게 보내는 경보**의
근거가 되므로, 오경보를 최소화하도록 **보수적으로** 판단합니다.

## 판단 원칙 (보수성 — 반드시 준수)
- 기본값은 **thesis_broken=false**입니다. 가설 훼손을 뒷받침하는 **구체적·확인
  가능한 사실**(악재 공시, 실적/가이던스 하향, 사업 근간을 흔드는 뉴스 등)이
  데이터에 있을 때만 true로 판단하세요.
- 막연한 주가 하락·단기 변동성·테마 소멸·"분위기"만으로는 훼손으로 보지 마세요.
  (가격 기반 청산은 별도 손절/익절 로직이 담당합니다 — 여기서 중복 판단 금지.)
- 근거가 약하거나 데이터가 부족하면 thesis_broken=false, confidence를 낮게.
- confidence는 **훼손 판단의 확신도**입니다. 훼손이 명백할수록 높게, 애매하면 낮게.
  확신이 없으면 0.5 미만으로 두세요(과대평가 금지).

## 입력
- 진입 가설: 매수 시점의 action/confidence/key_factors 요약.
- 현재 뉴스 / DART 공시 / 펀더멘털 점수 / 재무제표.

## 출력 (JSON)
- symbol: 종목코드
- thesis_broken: 진입 가설이 훼손됐는가 (true/false)
- confidence: 0.0~1.0 (훼손 판단의 확신도)
- severity: "low" | "medium" | "high" (훼손 심각도)
- key_changes: 진입 가설 대비 바뀐 핵심 사실 2~4개 (없으면 빈 리스트)
- reasoning: 판단 근거 2~4문장 (진입 근거의 어느 부분이 왜 훼손/유지됐는지)
- JSON 형식으로만 응답하세요.
"""


def build_user_prompt(data: dict[str, Any]) -> str:
    """진입 가설 + 현재 데이터를 유저 프롬프트로 변환."""
    symbol = data.get("symbol", "UNKNOWN")
    name = data.get("name")
    symbol_line = f"{name} ({symbol})" if name and name != symbol else symbol
    sections: list[str] = [f"## 가설 점검 요청: {symbol_line}\n"]

    entry_date = data.get("entry_date")
    if entry_date:
        sections.append(f"진입일: {entry_date}\n")

    # 진입 가설 (매수 근거 스냅샷)
    snapshot = data.get("entry_snapshot")
    if snapshot:
        sections.append("### 진입 시 투자 가설 (매수 근거)")
        sections.append(
            f"```json\n{json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)}\n```\n"
        )

    # 현재 펀더멘털
    fundamental = data.get("fundamental")
    if fundamental and not fundamental.get("error"):
        sections.append("### 현재 펀더멘털 점수")
        sections.append(
            f"```json\n{json.dumps(fundamental, ensure_ascii=False, indent=2, default=str)}\n```\n"
        )

    financials = data.get("financials")
    if financials and financials.get("statements"):
        sections.append("### 최근 재무제표")
        sections.append(
            f"```json\n{json.dumps(financials, ensure_ascii=False, indent=2, default=str)}\n```\n"
        )

    # 현재 공시 (악재 공시 탐지에 중요)
    disclosures = data.get("disclosures")
    if disclosures and disclosures.get("disclosures"):
        sections.append("### 최근 DART 공시")
        for d in disclosures["disclosures"][:10]:
            sections.append(
                f"- [{d.get('receipt_date', '')}] {d.get('report_name', '')} ({d.get('filer_name', '')})"
            )
        sections.append("")

    # 현재 뉴스
    news = data.get("recent_news")
    if news and news.get("articles"):
        sections.append("### 최근 뉴스")
        for a in news["articles"][:15]:
            label = a.get("sentiment_label") or ""
            sections.append(
                f"- [{a.get('published_at', '')}] {a.get('title', '')} {('(' + label + ')') if label else ''}"
            )
        sections.append("")

    sections.append(
        f"위 진입 가설이 현재 데이터로도 유효한지 판단하여 종목 {symbol_line}에 대한"
        " ThesisMonitorResult JSON을 출력하세요.\n"
        "**보수적으로** 판단하세요 — 구체적 훼손 근거가 없으면 thesis_broken=false."
    )

    return "\n".join(sections)
