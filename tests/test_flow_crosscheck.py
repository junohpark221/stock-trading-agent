"""PRJ-03 단계 5 — 수급 크로스체크 순수 비교 모듈 단위 테스트.

DB·네트워크 불요: existing/incoming 전부 합성 dict.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.core.models import InvestorFlowRecord, MarketInvestorFlowRecord
from src.data.flow_crosscheck import (
    CROSS_AXES,
    INDEX_OHLC_FIELDS,
    KIS_ONLY_AXES,
    MARKET_CROSS_FIELDS,
    REVISION_FIELDS_MARKET,
    REVISION_FIELDS_SYMBOL,
    SYMBOL_CROSS_FIELDS,
    CrosscheckStats,
    FlowSyncResult,
    crosscheck_rows,
)

D1 = date(2026, 7, 17)
D2 = date(2026, 7, 18)
TODAY = date(2026, 7, 21)


def _flow_row(day: date, **overrides) -> dict:
    """종목 수급 행 — 기본 전 필드 None, 필요한 셀만 채운다."""
    row: dict = {"symbol": "005930", "date": day}
    for f in REVISION_FIELDS_SYMBOL:
        row.setdefault(f, None)
    row.update(overrides)
    return row


def _existing(day: date, source: str, **overrides) -> dict:
    return {**_flow_row(day, **overrides), "source": source}


def _market_row(day: date, **overrides) -> dict:
    row: dict = {"market": "kospi", "date": day}
    for f in REVISION_FIELDS_MARKET:
        row.setdefault(f, None)
    row.update(overrides)
    return row


def _run(existing_by_date, incoming, *, table="flow", incoming_source="kis", before=TODAY, **kw):
    return crosscheck_rows(
        existing_by_date,
        incoming,
        key_value="005930" if table == "flow" else "kospi",
        table=table,
        incoming_source=incoming_source,
        before=before,
        **kw,
    )


# ═══════════════════════════════════════════════════════════════════════
# 상수 계약
# ═══════════════════════════════════════════════════════════════════════


class TestFieldContracts:
    def test_cross_fields_subset_of_record_fields(self):
        assert set(SYMBOL_CROSS_FIELDS) <= set(InvestorFlowRecord.model_fields)
        assert set(MARKET_CROSS_FIELDS) <= set(MarketInvestorFlowRecord.model_fields)
        assert set(INDEX_OHLC_FIELDS) <= set(MarketInvestorFlowRecord.model_fields)

    def test_cross_field_counts(self):
        assert len(SYMBOL_CROSS_FIELDS) == 11 * 3 * 2  # 66
        assert len(MARKET_CROSS_FIELDS) == 11 * 2  # 22

    def test_kis_only_axes_not_in_cross_fields(self):
        # etc prefix 함정: etc는 제외되되 etc_corp는 비교 대상이어야 한다.
        for axis in KIS_ONLY_AXES:
            assert not any(
                f.startswith(f"{axis}_net") or f.startswith(f"{axis}_sell")
                or f.startswith(f"{axis}_buy")
                for f in SYMBOL_CROSS_FIELDS
            ), axis
        assert "etc_corp_net_qty" in SYMBOL_CROSS_FIELDS
        assert "etc_net_qty" not in SYMBOL_CROSS_FIELDS
        assert "etc_orgt_net_qty" not in SYMBOL_CROSS_FIELDS
        assert "etc_corp" in CROSS_AXES


# ═══════════════════════════════════════════════════════════════════════
# 기본 동작·모드 분류
# ═══════════════════════════════════════════════════════════════════════


class TestBasics:
    def test_identical_rows_no_diff(self):
        existing = {D1: _existing(D1, "kis", frgn_net_qty=100, frgn_net_amt=Decimal(5_000_000))}
        incoming = [_flow_row(D1, frgn_net_qty=100, frgn_net_amt=Decimal(5_000_000))]
        stats = _run(existing, incoming)
        assert stats.compared_rows == 1
        assert stats.mismatched_cells == 0
        assert stats.flagged_rows == 0
        assert stats.diffs == []
        # 채워진 2셀만 비교됨(None-None 쌍은 셀 카운트 제외)
        assert stats.compared_cells == 2

    def test_kind_classification_matrix(self):
        # existing kis + incoming kis → revision
        s1 = _run(
            {D1: _existing(D1, "kis", frgn_net_qty=100)},
            [_flow_row(D1, frgn_net_qty=999)],
            incoming_source="kis",
        )
        assert s1.revision_rows == 1 and s1.cross_source_rows == 0
        assert s1.diffs[0].kind == "revision"
        # existing krx + incoming kis → cross_source
        s2 = _run(
            {D1: _existing(D1, "krx", frgn_net_qty=100)},
            [_flow_row(D1, frgn_net_qty=999)],
            incoming_source="kis",
        )
        assert s2.cross_source_rows == 1 and s2.revision_rows == 0
        assert s2.diffs[0].kind == "cross_source"
        assert s2.diffs[0].source_before == "krx"
        # existing kis + incoming krx(스크립트 방향) → cross_source
        s3 = _run(
            {D1: _existing(D1, "kis", frgn_net_qty=100)},
            [_flow_row(D1, frgn_net_qty=999)],
            incoming_source="krx",
        )
        assert s3.cross_source_rows == 1

    def test_before_filter_and_unknown_dates(self):
        existing = {D1: _existing(D1, "kis", frgn_net_qty=100)}
        incoming = [
            _flow_row(D1, frgn_net_qty=999),  # 비교됨
            _flow_row(TODAY, frgn_net_qty=1),  # before 필터(당일 잠정치)
            _flow_row(D2, frgn_net_qty=1),  # DB에 없는 날짜 = 신규 적재
        ]
        stats = _run(existing, incoming, before=TODAY)
        assert stats.compared_rows == 1
        # before=None이면 당일 행도 비교 대상(단, D2·TODAY는 DB에 없어 스킵)
        stats2 = _run(existing, incoming, before=None)
        assert stats2.compared_rows == 1


# ═══════════════════════════════════════════════════════════════════════
# 허용 오차 경계
# ═══════════════════════════════════════════════════════════════════════


class TestTolerances:
    def test_cross_amt_million_boundary(self):
        base = Decimal(123_000_000)
        for delta, expect_mismatch in ((1_000_000, False), (1_000_001, True)):
            stats = _run(
                {D1: _existing(D1, "krx", frgn_net_amt=base)},
                [_flow_row(D1, frgn_net_amt=base + delta)],
            )
            assert (stats.mismatched_cells == 1) is expect_mismatch, delta

    def test_cross_symbol_qty_exact(self):
        stats = _run(
            {D1: _existing(D1, "krx", prsn_net_qty=1000)},
            [_flow_row(D1, prsn_net_qty=1001)],
        )
        assert stats.mismatched_cells == 1
        assert stats.cross_source_rows == 1

    def test_cross_market_qty_500_boundary(self):
        for delta, expect_mismatch in ((500, False), (501, True)):
            stats = _run(
                {D1: {**_market_row(D1, frgn_net_qty=10_000), "source": "krx"}},
                [_market_row(D1, frgn_net_qty=10_000 + delta)],
                table="market_flow",
            )
            assert (stats.mismatched_cells == 1) is expect_mismatch, delta

    def test_cross_index_ohlc_boundary(self):
        for close, expect_mismatch in (
            (Decimal("3175.77"), False),  # 정확 일치
            (Decimal("3175.78"), False),  # ±0.01 이내
            (Decimal("3175.79"), True),
        ):
            stats = _run(
                {D1: {**_market_row(D1, index_close=Decimal("3175.77")), "source": "krx"}},
                [_market_row(D1, index_close=close)],
                table="market_flow",
            )
            assert (stats.mismatched_cells == 1) is expect_mismatch, close

    def test_revision_is_exact_even_for_amt(self):
        # 동일 소스 재수신은 허용오차 없음 — 1원 차이도 diff.
        stats = _run(
            {D1: _existing(D1, "kis", frgn_net_amt=Decimal(5_000_000))},
            [_flow_row(D1, frgn_net_amt=Decimal(5_000_001))],
        )
        assert stats.mismatched_cells == 1
        assert stats.revision_rows == 1


# ═══════════════════════════════════════════════════════════════════════
# NULL 규칙·축 제외
# ═══════════════════════════════════════════════════════════════════════


class TestNullAndAxisRules:
    def test_cross_null_side_skipped(self):
        # krx 행은 kis 전용 아닌 축도 결측일 수 있다 — 한쪽 NULL은 스킵.
        stats = _run(
            {D1: _existing(D1, "krx", frgn_net_qty=None, prsn_net_qty=10)},
            [_flow_row(D1, frgn_net_qty=100, prsn_net_qty=10)],
        )
        assert stats.compared_cells == 1  # prsn만
        assert stats.mismatched_cells == 0

    def test_revision_null_to_value_is_diff(self):
        stats = _run(
            {D1: _existing(D1, "kis", frgn_net_qty=None)},
            [_flow_row(D1, frgn_net_qty=100)],
        )
        assert stats.mismatched_cells == 1
        assert stats.diffs[0].old is None and stats.diffs[0].new == 100

    def test_kis_only_axis_excluded_in_cross_but_compared_in_revision(self):
        existing_cross = _existing(D1, "krx", frgn_reg_net_qty=None, etc_net_qty=None)
        incoming = _flow_row(D1, frgn_reg_net_qty=55, etc_net_qty=7)
        s_cross = _run({D1: existing_cross}, [incoming])
        assert s_cross.compared_cells == 0
        assert s_cross.mismatched_cells == 0
        # revision(동일 소스)에서는 전 레코드 필드 비교 — NULL→값도 diff.
        s_rev = _run({D1: {**existing_cross, "source": "kis"}}, [incoming])
        assert s_rev.mismatched_cells == 2

    def test_market_pykrx_only_columns_ignored(self):
        # index_volume 등은 레코드 필드가 아니라 비교 자체가 일어나지 않는다.
        existing = {
            D1: {
                **_market_row(D1, frgn_net_qty=10),
                "source": "krx",
                "index_volume": 123456789,
            }
        }
        stats = _run(existing, [_market_row(D1, frgn_net_qty=10)], table="market_flow")
        assert stats.compared_cells == 1
        assert stats.mismatched_cells == 0


# ═══════════════════════════════════════════════════════════════════════
# 캡·merge·FlowSyncResult
# ═══════════════════════════════════════════════════════════════════════


class TestAggregation:
    def test_max_diffs_cap_keeps_counts_exact(self):
        overrides_old = {f: 1 for f in SYMBOL_CROSS_FIELDS if f.endswith("_qty")}
        overrides_new = {f: 999_999 for f in SYMBOL_CROSS_FIELDS if f.endswith("_qty")}
        existing = {D1: _existing(D1, "krx", **overrides_old)}
        stats = _run(existing, [_flow_row(D1, **overrides_new)], max_diffs=5)
        assert len(stats.diffs) == 5
        assert stats.mismatched_cells == 33  # 11축 × 3슬롯 qty 전부
        assert stats.cross_source_rows == 1

    def test_merge_accumulates(self):
        a = _run(
            {D1: _existing(D1, "krx", frgn_net_qty=1)},
            [_flow_row(D1, frgn_net_qty=2)],
        )
        b = _run(
            {D1: _existing(D1, "kis", frgn_net_qty=1)},
            [_flow_row(D1, frgn_net_qty=2)],
        )
        total = CrosscheckStats()
        total.merge(a)
        total.merge(b)
        assert total.compared_rows == 2
        assert total.cross_source_rows == 1 and total.revision_rows == 1
        assert total.mismatched_cells == 2
        assert total.per_field["frgn_net_qty"] == [2, 2]
        assert len(total.diffs) == 2

    def test_merge_respects_diff_cap(self):
        a = CrosscheckStats(diffs=[object()] * 28)  # type: ignore[list-item]
        b = CrosscheckStats(diffs=[object()] * 10)  # type: ignore[list-item]
        a.merge(b, max_diffs=30)
        assert len(a.diffs) == 30

    def test_flow_sync_result_none_stats(self):
        res = FlowSyncResult(30)
        assert res.upserted == 30
        assert res.revision_rows == 0
        assert res.cross_source_rows == 0
        assert res.mismatched_cells == 0

    def test_flow_sync_result_proxies_stats(self):
        stats = _run(
            {D1: _existing(D1, "kis", frgn_net_qty=1)},
            [_flow_row(D1, frgn_net_qty=2)],
        )
        res = FlowSyncResult(30, stats)
        assert res.revision_rows == 1
        assert res.mismatched_cells == 1
