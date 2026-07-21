"""PRJ-03 단계 5 — 크로스체크 스크립트(scripts/crosscheck_investor_flow.py) 단위 테스트.

네트워크·DB 불요: 표본 선정·판정·리포트 렌더 순수 부분만 검증
(pythonpath=["scripts"]로 플랫 임포트 — test_backfill_pykrx.py 컨벤션).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import crosscheck_investor_flow as cc
import pytest

from src.data.flow_crosscheck import CrosscheckStats, FieldDiff


# ── 표본 선정 ─────────────────────────────────────────────────────────


class TestSplitSample:
    def test_deterministic_with_seed(self):
        top = [f"T{i:03d}" for i in range(30)]
        pool = [f"P{i:03d}" for i in range(200)]
        a = cc.split_sample(top, pool, 50, seed=42)
        b = cc.split_sample(top, pool, 50, seed=42)
        assert a == b
        assert cc.split_sample(top, pool, 50, seed=7) != a

    def test_half_top_half_random(self):
        top = [f"T{i:03d}" for i in range(30)]
        pool = [f"P{i:03d}" for i in range(200)]
        top_half, rand_half = cc.split_sample(top, pool, 50, seed=42)
        assert top_half == top[:25]
        assert len(rand_half) == 25
        assert set(rand_half) <= set(pool)

    def test_top_excluded_from_random_pool(self):
        top = ["A", "B"]
        pool = ["A", "B", "C", "D"]
        top_half, rand_half = cc.split_sample(top, pool, 4, seed=1)
        assert top_half == ["A", "B"]
        assert set(rand_half) == {"C", "D"}  # top은 랜덤 풀에서 제외

    def test_small_pool_no_crash(self):
        top_half, rand_half = cc.split_sample(["A"], ["A", "B"], 50, seed=1)
        assert top_half == ["A"]
        assert rand_half == ["B"]


# ── 판정 ──────────────────────────────────────────────────────────────


def _stats(compared_rows=100, compared_cells=10_000, mismatched_cells=0, per_field=None):
    return CrosscheckStats(
        compared_rows=compared_rows,
        compared_cells=compared_cells,
        mismatched_cells=mismatched_cells,
        per_field=per_field or {},
    )


class TestVerdict:
    def test_pass_clean(self):
        ok, reasons = cc.verdict({"flow": _stats()})
        assert ok and reasons == []

    def test_fail_no_rows(self):
        ok, reasons = cc.verdict({"flow": _stats(compared_rows=0, compared_cells=0)})
        assert not ok
        assert "비교된 행이 없음" in reasons[0]

    def test_overall_rate_boundary(self):
        # 0.5% 정확히 = 통과, 초과 = 실패
        ok, _ = cc.verdict({"flow": _stats(mismatched_cells=50)})  # 50/10000 = 0.5%
        assert ok
        ok, reasons = cc.verdict({"flow": _stats(mismatched_cells=51)})
        assert not ok and "전체 셀 불일치율" in reasons[0]

    def test_field_rate_boundary(self):
        # compared >= 20 필드만 필드별 1% 기준 적용
        ok, _ = cc.verdict(
            {"flow": _stats(per_field={"frgn_net_qty": [100, 1]})}  # 1%
        )
        assert ok
        ok, reasons = cc.verdict(
            {"flow": _stats(per_field={"frgn_net_qty": [100, 2]})}  # 2%
        )
        assert not ok and "flow.frgn_net_qty" in reasons[0]

    def test_field_below_min_compared_ignored(self):
        ok, _ = cc.verdict(
            {"flow": _stats(per_field={"rare_field": [19, 19]})}  # 표본 부족 — 무시
        )
        assert ok

    def test_multi_table_aggregation(self):
        ok, _ = cc.verdict(
            {
                "flow": _stats(compared_cells=100, mismatched_cells=1),  # 1% 단독 초과
                "market_flow": _stats(compared_cells=9_900),
            }
        )
        assert ok  # 합산 1/10000 = 0.01% — 전체 기준으로 판정


# ── 리포트 렌더 ───────────────────────────────────────────────────────


class TestRenderStats:
    def test_render_smoke(self, capsys):
        stats = CrosscheckStats(
            compared_rows=10,
            revision_rows=1,
            cross_source_rows=2,
            compared_cells=500,
            mismatched_cells=3,
            per_field={"frgn_net_qty": [10, 1], "prsn_net_amt": [10, 0]},
            diffs=[
                FieldDiff(
                    key="005930",
                    day=date(2026, 7, 17),
                    kind="cross_source",
                    source_before="kis",
                    field="frgn_net_qty",
                    old=100,
                    new=200,
                )
            ],
        )
        cc.render_stats("flow", stats, max_samples=5)
        out = capsys.readouterr().out
        assert "flow — 비교 결과" in out
        assert "`frgn_net_qty` | 10 | 1" in out
        assert "| 005930 | 2026-07-17 | cross_source | kis" in out
        assert "| 100 | 200 | 100 |" in out  # delta 계산

    def test_render_null_delta(self, capsys):
        stats = CrosscheckStats(
            diffs=[
                FieldDiff(
                    key="kospi",
                    day=date(2026, 7, 17),
                    kind="revision",
                    source_before="kis",
                    field="index_close",
                    old=None,
                    new=Decimal("3175.77"),
                )
            ],
        )
        cc.render_stats("market_flow", stats, max_samples=5)
        out = capsys.readouterr().out
        assert "| None | 3175.77 | - |" in out  # None 델타는 '-'


# ── CLI 기본값 ────────────────────────────────────────────────────────


class TestParseArgs:
    def test_defaults(self):
        args = cc.parse_args([])
        assert args.days == 30
        assert args.end is None
        assert args.symbols is None
        assert args.sample_size == 50
        assert args.seed == 42
        assert args.max_mismatch_samples == 10

    def test_end_parsing(self):
        args = cc.parse_args(["--end", "2026-07-18", "--symbols", "005930,000660"])
        assert args.end == date(2026, 7, 18)
        assert args.symbols == "005930,000660"

    def test_invalid_end_exits(self):
        with pytest.raises(SystemExit):
            cc.parse_args(["--end", "notadate"])
