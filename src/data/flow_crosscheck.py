"""PRJ-03 단계 5: 수급 백필(krx)↔증분(kis) 크로스체크 순수 비교 모듈.

provider(일일 sync 리비전 감지)와 scripts/crosscheck_investor_flow.py(pykrx 대조)가
같은 비교 함수를 공유한다(F-13 단일 구현 원칙). 의존은 도메인 레코드 + stdlib뿐 —
DB/네트워크 없이 단위 테스트 가능.

비교 모드 (existing 행의 ``source`` vs ``incoming_source``):

- **revision** (동일 소스): 같은 소스가 과거 행을 다시 보냈는데 값이 다르면 전부
  이상 신호 — 전 레코드 필드 exact 비교(NULL↔값 변화도 diff). kis 자기 재수정
  감지가 게이트 ④ 캐비앗(새벽 보정 미검증 창)의 상시 감시 수단이다.
- **cross_source** (이종 소스): 원천 정밀도 차이를 허용 오차로 흡수 —
  대금 ±1,000,000원(KIS ``*_pbmn`` 백만원 절사), 시장 수량 ±500주(KIS 천주 반올림),
  지수 OHLC ±0.01. KIS 전용 축(krx 측 항상 NULL)은 비교 제외, 그 외에도 한쪽
  NULL이면 스킵.

short_interest는 범위 외 — krx 백필 자체가 없고(단계 4 확정 범위) 공매도·대차는
공표 지연으로 과거 행 갱신이 일상이라 리비전 감지가 노이즈만 만든다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from src.core.models import InvestorFlowRecord, MarketInvestorFlowRecord

__all__ = [
    "CROSS_AXES",
    "INDEX_OHLC_FIELDS",
    "KIS_ONLY_AXES",
    "MARKET_CROSS_FIELDS",
    "REVISION_FIELDS_MARKET",
    "REVISION_FIELDS_SYMBOL",
    "CrosscheckStats",
    "FieldDiff",
    "FlowSyncResult",
    "crosscheck_rows",
]

# ── 축·필드 정의 ────────────────────────────────────────────────────────

#: 크로스소스 비교 가능한 11축 — krx(pykrx) 측이 직접 제공(9) + 파생 합산(frgn/orgn).
CROSS_AXES: tuple[str, ...] = (
    "frgn", "prsn", "orgn", "scrt", "ivtr", "pe_fund",
    "bank", "insu", "mrbn", "fund", "etc_corp",
)

#: KIS 전용 축 — krx 행에서 항상 NULL이라 크로스소스 비교 제외.
#: ``etc``는 krx에 기타단체 축이 없어 파생 불가(백필이 의도적으로 NULL 유지).
#: ⚠️ prefix 매칭 금지 — ``etc``가 ``etc_corp``/``etc_orgt``를 삼킨다.
#: 반드시 아래처럼 정확한 필드명 튜플로만 사용한다.
KIS_ONLY_AXES: tuple[str, ...] = ("frgn_reg", "frgn_nreg", "etc", "etc_orgt")

_SLOTS = ("net", "sell", "buy")
_SUFFIXES = ("qty", "amt")

#: 종목 테이블 크로스소스 비교 필드 — 11축 × (net/sell/buy) × (qty/amt) = 66.
SYMBOL_CROSS_FIELDS: tuple[str, ...] = tuple(
    f"{axis}_{slot}_{suffix}"
    for axis in CROSS_AXES
    for slot in _SLOTS
    for suffix in _SUFFIXES
)

#: 시장 테이블 크로스소스 비교 필드 — 11축 × net × (qty/amt) = 22.
MARKET_CROSS_FIELDS: tuple[str, ...] = tuple(
    f"{axis}_net_{suffix}" for axis in CROSS_AXES for suffix in _SUFFIXES
)

#: 시장 테이블 지수 OHLC — kis·krx 모두 채우는 4필드(±0.01 비교).
#: index_prev_close/change/change_rate는 파생 방식이 소스별로 달라 제외,
#: index_volume/trading_value/market_cap은 pykrx 전용(레코드에 없음)이라 자동 제외.
INDEX_OHLC_FIELDS: tuple[str, ...] = (
    "index_open", "index_high", "index_low", "index_close",
)

#: revision(동일 소스) 비교 필드 — 도메인 레코드 필드에서 키 제외 전체.
REVISION_FIELDS_SYMBOL: tuple[str, ...] = tuple(
    k for k in InvestorFlowRecord.model_fields if k not in ("symbol", "date")
)
REVISION_FIELDS_MARKET: tuple[str, ...] = tuple(
    k for k in MarketInvestorFlowRecord.model_fields if k not in ("market", "date")
)

# ── 허용 오차 ──────────────────────────────────────────────────────────

#: KIS 대금 원천 정밀도 = 백만원 절사 → 원 단위 비교 시 ±1e6 정상.
_AMT_TOLERANCE = Decimal(1_000_000)
#: KIS 시장 수량 원천 = 천주 반올림(×1000 변환 저장) → ±500주 정상.
_MARKET_QTY_TOLERANCE = Decimal(500)
_INDEX_TOLERANCE = Decimal("0.01")
_EXACT = Decimal(0)


def _cross_tolerance(table: str, fld: str) -> Decimal:
    if fld in INDEX_OHLC_FIELDS:
        return _INDEX_TOLERANCE
    if fld.endswith("_amt"):
        return _AMT_TOLERANCE
    if table == "market_flow" and fld.endswith("_qty"):
        return _MARKET_QTY_TOLERANCE
    return _EXACT


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# ── 결과 타입 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FieldDiff:
    """단일 셀 불일치 상세."""

    key: str  # symbol 또는 market
    day: date
    kind: str  # "revision" | "cross_source"
    source_before: str  # 기존 DB 행의 source
    field: str
    old: Any
    new: Any


@dataclass
class CrosscheckStats:
    """크로스체크 집계 — ``diffs``는 ``max_diffs`` 캡, 카운트는 항상 정확."""

    compared_rows: int = 0
    revision_rows: int = 0  # 동일 소스인데 값이 바뀐 행 수(④ 새벽 보정 감시)
    cross_source_rows: int = 0  # 이종 소스에서 허용오차 초과 행 수(경계 크로스체크)
    compared_cells: int = 0
    mismatched_cells: int = 0
    diffs: list[FieldDiff] = field(default_factory=list)
    #: field → [compared, mismatched] (스크립트 축별 일치율 표용)
    per_field: dict[str, list[int]] = field(default_factory=dict)

    @property
    def flagged_rows(self) -> int:
        return self.revision_rows + self.cross_source_rows

    def merge(self, other: "CrosscheckStats", *, max_diffs: int = 30) -> None:
        """다른 집계를 이 집계에 합산(스크립트 전역 누적용)."""
        self.compared_rows += other.compared_rows
        self.revision_rows += other.revision_rows
        self.cross_source_rows += other.cross_source_rows
        self.compared_cells += other.compared_cells
        self.mismatched_cells += other.mismatched_cells
        room = max_diffs - len(self.diffs)
        if room > 0:
            self.diffs.extend(other.diffs[:room])
        for fld, (compared, mismatched) in other.per_field.items():
            acc = self.per_field.setdefault(fld, [0, 0])
            acc[0] += compared
            acc[1] += mismatched


@dataclass
class FlowSyncResult:
    """provider ``sync_investor_flow``/``sync_market_investor_flow`` 반환 타입.

    ``stats``는 크로스체크 실패(예외 흡수)·빈 응답 경로에서 None.
    """

    upserted: int
    stats: CrosscheckStats | None = None

    @property
    def revision_rows(self) -> int:
        return self.stats.revision_rows if self.stats is not None else 0

    @property
    def cross_source_rows(self) -> int:
        return self.stats.cross_source_rows if self.stats is not None else 0

    @property
    def mismatched_cells(self) -> int:
        return self.stats.mismatched_cells if self.stats is not None else 0


# ── 비교 본체 ──────────────────────────────────────────────────────────


def _fields_for(
    table: Literal["flow", "market_flow"], kind: str
) -> tuple[str, ...]:
    if kind == "revision":
        return (
            REVISION_FIELDS_SYMBOL if table == "flow" else REVISION_FIELDS_MARKET
        )
    if table == "flow":
        return SYMBOL_CROSS_FIELDS
    return MARKET_CROSS_FIELDS + INDEX_OHLC_FIELDS


def crosscheck_rows(
    existing_by_date: Mapping[date, Mapping[str, Any]],
    incoming_rows: Iterable[Mapping[str, Any]],
    *,
    key_value: str,
    table: Literal["flow", "market_flow"],
    incoming_source: Literal["kis", "krx"],
    before: date | None,
    max_diffs: int = 30,
) -> CrosscheckStats:
    """수신 행을 기존 DB 행과 비교해 불일치를 집계한다(부수효과 없음).

    Args:
        existing_by_date: date → 기존 DB 행(``source`` 키 필수, 비교 필드 포함).
        incoming_rows: ``record.model_dump()`` 또는 pykrx 빌더 산출 dict들
            (``date`` 키 필수).
        key_value: 로깅용 식별자(symbol 또는 market).
        table: ``"flow"``(종목) | ``"market_flow"``(시장).
        incoming_source: 수신 데이터의 소스 — 기존 행 source와 같으면 revision,
            다르면 cross_source 모드로 비교.
        before: 이 날짜 미만 행만 비교(None=전체). 당일 행 최초 적재는
            리비전이 아니므로 provider는 오늘 날짜를 넘긴다.
        max_diffs: ``diffs`` 상세 보관 상한(카운트는 캡과 무관하게 정확).
    """
    stats = CrosscheckStats()

    for row in incoming_rows:
        day: date = row["date"]
        if before is not None and day >= before:
            continue
        existing = existing_by_date.get(day)
        if existing is None:
            continue  # DB에 없는 날짜 = 신규 적재, 비교 대상 아님

        source_before = existing["source"]
        kind = "revision" if source_before == incoming_source else "cross_source"
        stats.compared_rows += 1
        row_mismatched = False

        for fld in _fields_for(table, kind):
            if fld not in row or fld not in existing:
                continue
            old = existing[fld]
            new = row[fld]

            if old is None and new is None:
                continue
            if old is None or new is None:
                if kind == "cross_source":
                    continue  # 이종 소스는 축 커버리지가 달라 한쪽 NULL 정상
                mismatch = True  # revision: NULL↔값 전환도 값 변경
            else:
                tol = (
                    _cross_tolerance(table, fld)
                    if kind == "cross_source"
                    else _EXACT
                )
                mismatch = abs(_as_decimal(old) - _as_decimal(new)) > tol

            stats.compared_cells += 1
            acc = stats.per_field.setdefault(fld, [0, 0])
            acc[0] += 1
            if mismatch:
                stats.mismatched_cells += 1
                acc[1] += 1
                row_mismatched = True
                if len(stats.diffs) < max_diffs:
                    stats.diffs.append(
                        FieldDiff(
                            key=key_value,
                            day=day,
                            kind=kind,
                            source_before=source_before,
                            field=fld,
                            old=old,
                            new=new,
                        )
                    )

        if row_mismatched:
            if kind == "revision":
                stats.revision_rows += 1
            else:
                stats.cross_source_rows += 1

    return stats
