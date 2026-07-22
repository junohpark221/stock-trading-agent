"""투자자 수급(investor flow) 지표 계산 모듈 — PRJ-03 단계 6.

수급 레코드(InvestorFlowRecord/MarketInvestorFlowRecord) 리스트에서
N일 누적·연속 순매수 스트릭·거래대금 대비 비율을 on-the-fly로 계산하는
**순수 함수** 계층. DB 접근 없음 — 라이브(에이전트 도구)·백테스트가
동일 함수를 재사용한다(F-13 단일출처 계산기 원칙).

입력 계약 (전 함수 공통):
- ``rows``는 **날짜 오름차순** 정렬·거래일 단위(달력 갭 미보정). 정렬은
  호출자 책임 — DB 조회 패턴은 ``ORDER BY date DESC LIMIT n`` 후 reverse
  (src/agent/tools/technical.py 전례).
- 전 데이터 필드는 nullable(krx 백필 행은 sell/buy 분해·등록/비등록 축이
  NULL) — 데이터 결측은 절대 예외를 내지 않고 None/0/커버리지로 강등한다.
  예외는 입력 검증 실패(미지 axis·window<=0·value 오타)의 ValueError뿐.
- 금액(*_amt)은 원(KRW)·Decimal, 수량(*_qty)은 주(株)·int. 시장 단위
  net_qty는 원천 반올림으로 ±500주 정밀도 한계 — 시장 지표는 amt 권장.

⚠️ 소비 대상 = **주식만 — ETF·ETN 수급은 지표 소비에서 제외한다**(07-22 확정):
DB에는 KIS 수집분 ETF·ETN 행이 존재하지만 ① 교차 검증 원천(pykrx 종목 수급
API — 주식 전용) 부재로 영구 미검증 ② pykrx 5년 백필 미포함이라 과거 결손
(KIS 증분 시작 이후 최근분뿐) ③ LP 헤지 물량 혼입으로 주식과 동일한 해석이
왜곡된다. 단계 8 도구·프롬프트 주입 시 심볼 선별은 호출자 책임.
"""

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from src.core.models import (
    FlowAxisSummary,
    FlowWindowStat,
    InvestorFlowRecord,
    InvestorFlowSummary,
    MarketFlowSummary,
    MarketInvestorFlowRecord,
)

FlowRow = InvestorFlowRecord | MarketInvestorFlowRecord
ValueKind = Literal["amt", "qty"]

#: 투자자 주체 15축 (KIS 토큰 — src/db/models/investor_flow.py docstring 기준)
VALID_AXES: frozenset[str] = frozenset(
    {
        "frgn",
        "frgn_reg",
        "frgn_nreg",
        "prsn",
        "orgn",
        "scrt",
        "ivtr",
        "pe_fund",
        "bank",
        "insu",
        "mrbn",
        "fund",
        "etc",
        "etc_corp",
        "etc_orgt",
    }
)

#: 요약 기본 축 — 외국인·기관합계·개인 + 금융투자 분리 표기(2026-07-21 사용자 확정)
DEFAULT_AXES: tuple[str, ...] = ("frgn", "orgn", "prsn", "scrt")

#: 요약 기본 윈도 — 주체별 5/20/60일 (plan.md 확정 8)
DEFAULT_WINDOWS: tuple[int, ...] = (5, 20, 60)

AXIS_LABELS: dict[str, str] = {
    "frgn": "외국인",
    "frgn_reg": "등록외국인",
    "frgn_nreg": "비등록외국인",
    "prsn": "개인",
    "orgn": "기관합계",
    "scrt": "금융투자",
    "ivtr": "투신",
    "pe_fund": "사모펀드",
    "bank": "은행",
    "insu": "보험",
    "mrbn": "종금·기타금융",
    "fund": "연기금",
    "etc": "기타",
    "etc_corp": "기타법인",
    "etc_orgt": "기타단체",
}

#: 금융투자(scrt) 축 소비 시 분리 표기 문구 (plan.md 확정 8)
SCRT_CAVEAT: str = (
    "ETF LP 헤지·프로그램 매매로 오염된 축 — 방향성 신호로 단독 사용 금지. "
    "기관합계(orgn)에 포함되어 있으므로 합산 시 이중계상 주의(분리 표기 전용)."
)

_INTENSITY_QUANT = Decimal("0.0001")  # 거래대금 대비 비율 소수 4자리
_RETURN_QUANT = Decimal("0.01")  # 지수 수익률 % 소수 2자리
_HUNDRED = Decimal(100)


def _validate_axis(axis: str) -> None:
    if axis not in VALID_AXES:
        raise ValueError(f"알 수 없는 투자자 축: {axis!r} (유효: {sorted(VALID_AXES)})")


def _validate_window(window: int) -> None:
    if window <= 0:
        raise ValueError(f"window는 1 이상이어야 함: {window}")


def _net_field(axis: str, value: ValueKind) -> str:
    if value not in ("amt", "qty"):
        raise ValueError(f"value는 'amt' 또는 'qty': {value!r}")
    return f"{axis}_net_{value}"


def _window_rows(rows: Sequence[FlowRow], window: int) -> Sequence[FlowRow]:
    """마지막 min(window, len(rows))행 슬라이스 (오름차순 유지)."""
    return rows[-window:] if len(rows) > window else rows


def cumulative_net(
    rows: Sequence[FlowRow],
    axis: str,
    *,
    window: int = 20,
    value: ValueKind = "amt",
) -> Decimal | int | None:
    """
    N일 누적 순매수 — 주체별 수급 방향·강도의 기본 지표

    [매매 로직에서의 역할]
    - 특정 주체(외국인/기관 등)가 최근 N일간 순매수/순매도한 총량
    - 양수: 해당 주체의 매집, 음수: 이탈 — 외국인·기관 동반 순매수는
      전통적으로 가장 강한 수급 신호
    - 원화 절대값은 대형주가 지배하므로 비교에는 flow_intensity(정규화) 병용

    [계산 방식]
    1. rows(날짜 오름차순)의 마지막 min(window, len(rows))행을 취함
    2. ``{axis}_net_{value}`` 필드의 non-None 값만 합산 (None 일은 제외 —
       결측 정도는 compute_flow_summary의 days_covered로 판독)
    3. 윈도 내 non-None 값이 하나도 없으면 None (행 부족 시 부분 합산 —
       백테스트 초기 구간에서 크래시 대신 커버리지 강등)

    [우리 전략에서의 활용]
    - 5일(초단기)·20일(단기)·60일(중기) 윈도로 주체별 매집 추세 판독
    - thesis_monitor(F-11)·stock_analyst 프롬프트 요약 통계의 근간
    - 시장 레코드에도 동일 적용(단, 시장 qty는 ±500주 오차 — amt 권장)

    Args:
        rows: 수급 레코드 리스트 (날짜 오름차순)
        axis: 투자자 축 토큰 (frgn/orgn/prsn/scrt/...)
        window: 누적 윈도 일수 (기본 20)
        value: "amt"(원, Decimal 반환) 또는 "qty"(주, int 반환)

    Returns:
        누적 순매수 합 — 윈도 내 유효값이 없으면 None
    """
    _validate_axis(axis)
    _validate_window(window)
    field = _net_field(axis, value)

    values = [
        v for r in _window_rows(rows, window) if (v := getattr(r, field)) is not None
    ]
    if not values:
        return None
    total = sum(values)
    return total if value == "qty" else Decimal(total)


def net_streak(
    rows: Sequence[FlowRow],
    axis: str,
    *,
    value: ValueKind = "amt",
) -> int:
    """
    연속 순매수/순매도 스트릭 — 수급 지속성 지표

    [매매 로직에서의 역할]
    - "외국인 N일 연속 순매수" 같은 지속성 신호를 부호 있는 정수로 표현
    - +N: 최신일 포함 N일 연속 순매수 / -N: N일 연속 순매도
    - 스트릭이 길수록 해당 주체의 방향성 확신이 강함을 시사

    [계산 방식]
    1. 최신 행부터 역방향으로 ``{axis}_net_{value}`` 부호를 스캔
    2. **None 또는 0을 만나면 즉시 중단** — None을 건너뛰어 연속성을
       조작하지 않는다("N일 연속"이라는 문장이 문자 그대로 참이어야 함)
    3. 최신 유효일이 순매수면 +N, 순매도면 -N. rows가 비었거나 최신 값이
       None/0이면 0

    [우리 전략에서의 활용]
    - LLM 프롬프트에 "외국인 +7일 연속 순매수" 형태로 주입 — 판단 재료
    - 누적(cumulative_net)과 교차 검증: 누적 양수 + 스트릭 양수 = 일관 매집

    Args:
        rows: 수급 레코드 리스트 (날짜 오름차순)
        axis: 투자자 축 토큰
        value: 부호 판정에 쓸 슬롯 ("amt" 기본)

    Returns:
        부호 있는 연속 일수 (중단 조건 즉시 0 또는 누적된 ±N)
    """
    _validate_axis(axis)
    field = _net_field(axis, value)

    streak = 0
    sign = 0
    for r in reversed(rows):
        v = getattr(r, field)
        if v is None or v == 0:
            break
        current = 1 if v > 0 else -1
        if sign == 0:
            sign = current
        elif current != sign:
            break
        streak += 1
    return streak * sign


def flow_intensity(
    rows: Sequence[InvestorFlowRecord],
    trading_values: Mapping[date, Decimal | None],
    axis: str,
    *,
    window: int = 20,
) -> Decimal | None:
    """
    거래대금 대비 순매수 강도 — 종목 간 비교 가능한 정규화 수급 지표

    [매매 로직에서의 역할]
    - 원화 누적 순매수는 대형주가 지배 — 거래대금으로 나눠 종목 규모와
      무관하게 비교 가능한 비율로 정규화 (plan.md 원칙 3: 실무 표준)
    - 예: 0.05 = 윈도 거래대금의 5%를 해당 주체가 순매수 (강한 매집)

    [계산 방식]
    1. 윈도 내에서 ``{axis}_net_amt``와 trading_values[날짜]가 **둘 다**
       non-None이고 거래대금 > 0인 날만 분자·분모 양쪽에 산입
       (pairwise-complete — 분자/분모 기간 불일치 방지)
    2. Σ net_amt ÷ Σ trading_value, ROUND_HALF_UP 소수 4자리
    3. 유효일 0개 또는 분모 합 0이면 None. 날짜 키 부재는 None과 동일 취급
       (OHLCV/수급 수집 갭에 견고)

    [우리 전략에서의 활용]
    - 분모 = DailyOHLCV.trading_value (원, plan.md 확정 6 즉시 가용 분모)
    - 종목 전용 — 시장 단위는 index_trading_value가 pykrx 전용(kis 행
      NULL)이라 정규화 미채택

    Args:
        rows: 종목 수급 레코드 리스트 (날짜 오름차순)
        trading_values: {날짜: 거래대금(원) 또는 None} 매핑
        axis: 투자자 축 토큰
        window: 윈도 일수 (기본 20)

    Returns:
        누적 순매수 대금 / 누적 거래대금 (소수 4자리) — 계산 불가 시 None
    """
    _validate_axis(axis)
    _validate_window(window)
    field = f"{axis}_net_amt"

    numerator = Decimal(0)
    denominator = Decimal(0)
    valid_days = 0
    for r in _window_rows(rows, window):
        net = getattr(r, field)
        tv = trading_values.get(r.date)
        if net is None or tv is None or tv <= 0:
            continue
        numerator += net
        denominator += Decimal(tv)
        valid_days += 1

    if valid_days == 0 or denominator == 0:
        return None
    return (numerator / denominator).quantize(_INTENSITY_QUANT, rounding=ROUND_HALF_UP)


def _axis_summary(
    rows: Sequence[FlowRow],
    axis: str,
    windows: Sequence[int],
    trading_values: Mapping[date, Decimal | None] | None,
) -> FlowAxisSummary:
    """1축 요약 조립 (종목/시장 공용 — trading_values=None이면 intensity 생략)."""
    window_stats: list[FlowWindowStat] = []
    for w in windows:
        _validate_window(w)
        w_rows = _window_rows(rows, w)
        amt_field = f"{axis}_net_amt"
        covered = sum(1 for r in w_rows if getattr(r, amt_field) is not None)
        window_stats.append(
            FlowWindowStat(
                window=w,
                days_in_window=len(w_rows),
                days_covered=covered,
                net_amt=cumulative_net(rows, axis, window=w, value="amt"),
                net_qty=cumulative_net(rows, axis, window=w, value="qty"),
                intensity=(
                    flow_intensity(rows, trading_values, axis, window=w)  # type: ignore[arg-type]
                    if trading_values is not None
                    else None
                ),
            )
        )
    return FlowAxisSummary(
        axis=axis,
        label=AXIS_LABELS[axis],
        caveat=SCRT_CAVEAT if axis == "scrt" else None,
        streak=net_streak(rows, axis),
        windows=window_stats,
    )


def compute_flow_summary(
    rows: Sequence[InvestorFlowRecord],
    trading_values: Mapping[date, Decimal | None] | None = None,
    *,
    axes: Sequence[str] = DEFAULT_AXES,
    windows: Sequence[int] = DEFAULT_WINDOWS,
) -> InvestorFlowSummary:
    """
    종목 수급 요약 오케스트레이터 — 축×윈도 요약 통계 일괄 계산

    [매매 로직에서의 역할]
    - 주체별 누적·스트릭·거래대금 대비 비율을 한 번에 조립해
      LLM 프롬프트 주입용 요약(InvestorFlowSummary)으로 반환
    - 원시 시계열이 아닌 요약 통계 주입 원칙(plan.md 확정 8)의 구현체

    [계산 방식]
    1. 각 축에 대해 windows별 FlowWindowStat(누적 amt/qty·커버리지·
       intensity) + 스트릭을 계산
    2. trading_values 미제공 시 intensity는 전부 None (거래대금 없이도
       누적·스트릭은 유효)
    3. 금융투자(scrt) 축에만 caveat 문구 부착 — ETF LP 오염 분리 표기

    [우리 전략에서의 활용]
    - 단계 8: 에이전트 도구가 DB 조회 후 이 함수를 호출, ``model_dump(
      mode="json")``으로 thesis_monitor·stock_analyst 프롬프트에 주입
    - 단계 7: 백테스트 로더가 같은 함수로 시점별 수급 요약 재현

    Args:
        rows: 종목 수급 레코드 리스트 (날짜 오름차순)
        trading_values: {날짜: 거래대금(원)} 매핑 (생략 시 intensity 없음)
        axes: 요약할 축 목록 (기본 frgn/orgn/prsn/scrt)
        windows: 윈도 목록 (기본 5/20/60)

    Returns:
        InvestorFlowSummary — rows가 비면 symbol=""·as_of=None·빈 통계
    """
    for axis in axes:
        _validate_axis(axis)
    return InvestorFlowSummary(
        symbol=rows[-1].symbol if rows else "",
        as_of=rows[-1].date if rows else None,
        days_available=len(rows),
        axes=[_axis_summary(rows, axis, windows, trading_values) for axis in axes],
    )


def _index_window_return(
    rows: Sequence[MarketInvestorFlowRecord], window: int
) -> Decimal | None:
    """윈도 내 첫·마지막 유효 index_close 기준 수익률(%) — 유효 종가 2개 미만이면 None."""
    closes = [
        c for r in _window_rows(rows, window) if (c := r.index_close) is not None
    ]
    if len(closes) < 2 or closes[0] == 0:
        return None
    pct = (closes[-1] / closes[0] - 1) * _HUNDRED
    return pct.quantize(_RETURN_QUANT, rounding=ROUND_HALF_UP)


def compute_market_flow_summary(
    rows: Sequence[MarketInvestorFlowRecord],
    *,
    axes: Sequence[str] = DEFAULT_AXES,
    windows: Sequence[int] = DEFAULT_WINDOWS,
) -> MarketFlowSummary:
    """
    시장 단위 수급 요약 오케스트레이터 — 주체별 누적 + 지수 컨텍스트

    [매매 로직에서의 역할]
    - kospi/kosdaq 시장 전체의 주체별 순매수 흐름과 같은 창의 지수
      방향을 함께 제공 — market_analyst의 "외국인/기관 수급 동향" 판단
      재료(현재 dangling 프롬프트 항목의 데이터 소스)

    [계산 방식]
    1. 축 요약은 종목과 동일(_axis_summary 공용) — 단 intensity는 항상
       None (index_trading_value가 pykrx 전용·kis 행 NULL이라 정규화 미채택)
    2. 지수 컨텍스트: 최신 행의 index_close/index_change_rate +
       윈도별 수익률 = (윈도 내 마지막 유효 종가 ÷ 첫 유효 종가 − 1)×100,
       소수 2자리 — 유효 종가 2개 미만이면 None
    3. 시장 net_qty는 원천 ±500주 반올림 한계 — 판독은 amt 우선

    [우리 전략에서의 활용]
    - 단계 8: market_analyst의 _prepare_data에 주입해 시장 수급 섹션 해소
    - "외국인 20일 누적 -2.1조 & 지수 -4%" 같은 수급·가격 대조 판독 제공

    Args:
        rows: 시장 수급 레코드 리스트 (날짜 오름차순, 단일 market)
        axes: 요약할 축 목록 (기본 frgn/orgn/prsn/scrt)
        windows: 윈도 목록 (기본 5/20/60)

    Returns:
        MarketFlowSummary — rows가 비면 market=""·as_of=None·빈 통계
    """
    for axis in axes:
        _validate_axis(axis)
    latest = rows[-1] if rows else None
    return MarketFlowSummary(
        market=latest.market if latest else "",
        as_of=latest.date if latest else None,
        days_available=len(rows),
        index_close=latest.index_close if latest else None,
        index_change_rate=latest.index_change_rate if latest else None,
        index_window_returns={w: _index_window_return(rows, w) for w in windows},
        axes=[_axis_summary(rows, axis, windows, None) for axis in axes],
    )
