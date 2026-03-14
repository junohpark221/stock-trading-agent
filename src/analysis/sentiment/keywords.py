"""한국어 금융 키워드 사전 — 감성분석 키워드 매칭용.

긍정/부정 키워드는 weight(0.0 < w <= 1.0)로 신호 강도를 표현하고,
중요 키워드는 score와 무관하게 LLM 심층 분석을 트리거한다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KeywordEntry:
    """키워드와 가중치 쌍."""

    keyword: str
    weight: float  # 0.0 < weight <= 1.0


# ---------------------------------------------------------------------------
# 긍정 키워드 (~45개)
# ---------------------------------------------------------------------------

POSITIVE_KEYWORDS: list[KeywordEntry] = [
    # 실적/성장 (0.8~1.0)
    KeywordEntry("호실적", 0.9),
    KeywordEntry("매출증가", 0.8),
    KeywordEntry("영업이익증가", 0.9),
    KeywordEntry("어닝서프라이즈", 1.0),
    KeywordEntry("흑자전환", 0.9),
    KeywordEntry("사상최대", 1.0),
    KeywordEntry("실적개선", 0.8),
    KeywordEntry("매출성장", 0.8),
    KeywordEntry("이익증가", 0.8),
    KeywordEntry("순이익증가", 0.9),
    # 주가/시세 (0.6~0.8)
    KeywordEntry("신고가", 0.8),
    KeywordEntry("상한가", 0.7),
    KeywordEntry("급등", 0.6),
    KeywordEntry("강세", 0.6),
    KeywordEntry("반등", 0.6),
    KeywordEntry("회복세", 0.6),
    KeywordEntry("상승세", 0.6),
    KeywordEntry("돌파", 0.7),
    KeywordEntry("랠리", 0.7),
    # 투자의견 (0.7~0.9)
    KeywordEntry("매수추천", 0.9),
    KeywordEntry("목표가상향", 0.8),
    KeywordEntry("비중확대", 0.8),
    KeywordEntry("수혜주", 0.7),
    KeywordEntry("저평가", 0.7),
    KeywordEntry("매수의견", 0.8),
    KeywordEntry("아웃퍼폼", 0.8),
    KeywordEntry("탑픽", 0.9),
    # 기업이벤트(+) (0.7~0.9)
    KeywordEntry("배당확대", 0.8),
    KeywordEntry("자사주매입", 0.8),
    KeywordEntry("대규모수주", 0.9),
    KeywordEntry("전략적제휴", 0.7),
    KeywordEntry("특허취득", 0.7),
    KeywordEntry("신규수주", 0.8),
    KeywordEntry("기술이전", 0.7),
    KeywordEntry("흑자기조", 0.8),
    KeywordEntry("배당금인상", 0.7),
    KeywordEntry("자사주소각", 0.8),
    # 매크로(+) (0.5~0.7)
    KeywordEntry("금리인하", 0.6),
    KeywordEntry("경기회복", 0.6),
    KeywordEntry("수출호조", 0.5),
    KeywordEntry("경기부양", 0.5),
    KeywordEntry("유동성확대", 0.5),
    KeywordEntry("수출증가", 0.5),
    KeywordEntry("무역흑자", 0.5),
    KeywordEntry("경기개선", 0.6),
]

# ---------------------------------------------------------------------------
# 부정 키워드 (~45개)
# ---------------------------------------------------------------------------

NEGATIVE_KEYWORDS: list[KeywordEntry] = [
    # 실적/하락 (0.8~1.0)
    KeywordEntry("실적악화", 0.9),
    KeywordEntry("적자전환", 0.9),
    KeywordEntry("어닝쇼크", 1.0),
    KeywordEntry("매출감소", 0.8),
    KeywordEntry("영업손실", 0.9),
    KeywordEntry("실적부진", 0.8),
    KeywordEntry("적자확대", 0.9),
    KeywordEntry("매출하락", 0.8),
    KeywordEntry("영업이익감소", 0.8),
    KeywordEntry("순손실", 0.9),
    # 주가/시세 (0.6~0.8)
    KeywordEntry("신저가", 0.8),
    KeywordEntry("하한가", 0.7),
    KeywordEntry("급락", 0.7),
    KeywordEntry("폭락", 0.8),
    KeywordEntry("약세", 0.6),
    KeywordEntry("하락세", 0.6),
    KeywordEntry("추락", 0.7),
    KeywordEntry("투매", 0.7),
    KeywordEntry("반토막", 0.8),
    # 투자의견 (0.7~0.9)
    KeywordEntry("매도추천", 0.9),
    KeywordEntry("목표가하향", 0.8),
    KeywordEntry("비중축소", 0.8),
    KeywordEntry("투자주의", 0.7),
    KeywordEntry("매도의견", 0.8),
    KeywordEntry("언더퍼폼", 0.8),
    KeywordEntry("투자위험", 0.7),
    # 기업리스크 (0.9~1.0)
    KeywordEntry("분식회계", 1.0),
    KeywordEntry("횡령", 1.0),
    KeywordEntry("상장폐지", 1.0),
    KeywordEntry("관리종목", 0.9),
    KeywordEntry("감사의견거절", 1.0),
    KeywordEntry("리콜", 0.9),
    KeywordEntry("부도", 1.0),
    KeywordEntry("배임", 1.0),
    KeywordEntry("소송패소", 0.9),
    KeywordEntry("제재", 0.9),
    # 매크로(-) (0.5~0.7)
    KeywordEntry("금리인상", 0.6),
    KeywordEntry("경기침체", 0.7),
    KeywordEntry("무역전쟁", 0.6),
    KeywordEntry("인플레이션", 0.5),
    KeywordEntry("스태그플레이션", 0.7),
    KeywordEntry("경기둔화", 0.6),
    KeywordEntry("수출감소", 0.5),
    KeywordEntry("무역적자", 0.5),
    KeywordEntry("경기위축", 0.6),
]

# ---------------------------------------------------------------------------
# 중요 키워드 (~25개) — score 무관하게 needs_llm_analysis=True 트리거
# ---------------------------------------------------------------------------

IMPORTANT_KEYWORDS: list[str] = [
    # M&A
    "인수합병",
    "M&A",
    "합병",
    "인수",
    "피인수",
    # 공시
    "공시",
    "정정공시",
    "조회공시",
    # 자본변동
    "유상증자",
    "무상증자",
    "전환사채",
    "신주인수권",
    # 투자
    "대규모투자",
    "설비투자",
    "지분매각",
    # 경영
    "경영권분쟁",
    "대주주변경",
    "지배구조",
    # 위기
    "상장폐지위험",
    "관리종목지정",
    "감사의견",
    # 구조
    "분할",
    "물적분할",
    "인적분할",
    # 파산
    "파산",
    "회생",
    "워크아웃",
]
