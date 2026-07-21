"""PRJ-03 단계 6 — 수급 지표 순수 함수(src/analysis/investor_flow.py) 단위 테스트.

결정론적 합성 데이터 사용 (순수 계산이므로 mock 불필요 —
test_technical_indicators.py 컨벤션). 경계·NULL 축 처리 중심.
"""

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.analysis.investor_flow import (
    AXIS_LABELS,
    DEFAULT_AXES,
    DEFAULT_WINDOWS,
    SCRT_CAVEAT,
    VALID_AXES,
    compute_flow_summary,
    compute_market_flow_summary,
    cumulative_net,
    flow_intensity,
    net_streak,
)
from src.core.models import InvestorFlowRecord, MarketInvestorFlowRecord

# ── 픽스처 헬퍼 ───────────────────────────────────────────────────────


def _days(n: int, start: date = date(2026, 6, 1)) -> list[date]:
    """연속 평일 n개 (주말 스킵)."""
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _row(d: date, **kw) -> InvestorFlowRecord:
    """미지정 필드 전부 None인 종목 수급 레코드."""
    return InvestorFlowRecord(symbol="005930", date=d, **kw)


def _mrow(d: date, **kw) -> MarketInvestorFlowRecord:
    """미지정 필드 전부 None인 시장 수급 레코드."""
    kw.setdefault("market", "kospi")
    return MarketInvestorFlowRecord(date=d, **kw)


def _amt_rows(amts: list[Decimal | int | None]) -> list[InvestorFlowRecord]:
    """frgn_net_amt 시퀀스로 행 목록 생성 (오름차순)."""
    return [
        _row(d, frgn_net_amt=None if a is None else Decimal(a))
        for d, a in zip(_days(len(amts)), amts, strict=True)
    ]


# ── cumulative_net ────────────────────────────────────────────────────


class TestCumulativeNet:
    def test_basic_amt_sum(self):
        rows = _amt_rows([100, 200, -50, 300, 150])
        assert cumulative_net(rows, "frgn", window=5) == Decimal(700)

    def test_window_slices_latest(self):
        rows = _amt_rows([10_000, 100, 200, 300])
        # 마지막 3행만 — 첫 행 10,000은 윈도 밖
        assert cumulative_net(rows, "frgn", window=3) == Decimal(600)

    def test_window_larger_than_rows_partial_sum(self):
        rows = _amt_rows([100, 200])
        assert cumulative_net(rows, "frgn", window=60) == Decimal(300)

    def test_qty_returns_int(self):
        days = _days(3)
        rows = [_row(d, frgn_net_qty=q) for d, q in zip(days, [10, -3, 5], strict=True)]
        result = cumulative_net(rows, "frgn", window=3, value="qty")
        assert result == 12
        assert isinstance(result, int)

    def test_none_days_skipped(self):
        rows = _amt_rows([100, None, 200, None, 300])
        assert cumulative_net(rows, "frgn", window=5) == Decimal(600)

    def test_all_none_returns_none(self):
        rows = _amt_rows([None, None, None])
        assert cumulative_net(rows, "frgn", window=5) is None

    def test_empty_rows_returns_none(self):
        assert cumulative_net([], "frgn", window=5) is None

    def test_mixed_sign_net(self):
        rows = _amt_rows([500, -800])
        assert cumulative_net(rows, "frgn", window=2) == Decimal(-300)

    def test_window_one(self):
        rows = _amt_rows([100, 200, 999])
        assert cumulative_net(rows, "frgn", window=1) == Decimal(999)

    def test_market_record_works(self):
        days = _days(2)
        rows = [
            _mrow(d, orgn_net_amt=Decimal(a))
            for d, a in zip(days, [1_000, 2_000], strict=True)
        ]
        assert cumulative_net(rows, "orgn", window=5) == Decimal(3_000)

    def test_invalid_inputs_raise(self):
        rows = _amt_rows([100])
        with pytest.raises(ValueError, match="알 수 없는 투자자 축"):
            cumulative_net(rows, "foreign")
        with pytest.raises(ValueError, match="window"):
            cumulative_net(rows, "frgn", window=0)
        with pytest.raises(ValueError, match="value"):
            cumulative_net(rows, "frgn", value="vol")  # type: ignore[arg-type]


# ── net_streak ────────────────────────────────────────────────────────


class TestNetStreak:
    def test_positive_streak(self):
        rows = _amt_rows([-100, 200, 300, 400])
        assert net_streak(rows, "frgn") == 3

    def test_negative_streak(self):
        rows = _amt_rows([500, -100, -200])
        assert net_streak(rows, "frgn") == -2

    def test_latest_none_is_zero(self):
        rows = _amt_rows([100, 200, None])
        assert net_streak(rows, "frgn") == 0

    def test_latest_zero_is_zero(self):
        rows = _amt_rows([100, 200, 0])
        assert net_streak(rows, "frgn") == 0

    def test_middle_none_breaks(self):
        # None 건너뛰기 금지 — None 이후(최신 쪽)만 카운트
        rows = _amt_rows([100, 100, None, 200, 300])
        assert net_streak(rows, "frgn") == 2

    def test_middle_zero_breaks(self):
        rows = _amt_rows([100, 0, 200, 300])
        assert net_streak(rows, "frgn") == 2

    def test_empty_rows_zero(self):
        assert net_streak([], "frgn") == 0

    def test_single_row_signed(self):
        assert net_streak(_amt_rows([700]), "frgn") == 1
        assert net_streak(_amt_rows([-700]), "frgn") == -1

    def test_qty_slot(self):
        days = _days(3)
        rows = [
            _row(d, frgn_net_qty=q, frgn_net_amt=Decimal(-1))
            for d, q in zip(days, [5, 3, 8], strict=True)
        ]
        # qty 슬롯 기준이면 +3 (amt 기준이면 -3)
        assert net_streak(rows, "frgn", value="qty") == 3
        assert net_streak(rows, "frgn", value="amt") == -3


# ── flow_intensity ────────────────────────────────────────────────────


class TestFlowIntensity:
    def test_basic_ratio_quantized(self):
        days = _days(2)
        rows = [
            _row(d, frgn_net_amt=Decimal(a))
            for d, a in zip(days, [100, 233], strict=True)
        ]
        tv = {days[0]: Decimal(1_000), days[1]: Decimal(2_000)}
        # 333 / 3000 = 0.111 → 소수 4자리
        assert flow_intensity(rows, tv, "frgn", window=5) == Decimal("0.1110")

    def test_none_net_pairwise_excluded(self):
        days = _days(2)
        rows = [_row(days[0], frgn_net_amt=None), _row(days[1], frgn_net_amt=Decimal(100))]
        tv = {days[0]: Decimal(999_999), days[1]: Decimal(1_000)}
        # 첫날은 분모에서도 제외 — 100/1000
        assert flow_intensity(rows, tv, "frgn", window=5) == Decimal("0.1000")

    def test_none_trading_value_pairwise_excluded(self):
        days = _days(2)
        rows = _amt_rows([100, 100])
        tv = {days[0]: None, days[1]: Decimal(1_000)}
        assert flow_intensity(rows, tv, "frgn", window=5) == Decimal("0.1000")

    def test_missing_date_key_excluded(self):
        days = _days(2)
        rows = _amt_rows([100, 100])
        tv = {days[1]: Decimal(1_000)}  # 첫날 키 부재
        assert flow_intensity(rows, tv, "frgn", window=5) == Decimal("0.1000")

    def test_zero_denominator_returns_none(self):
        days = _days(1)
        rows = _amt_rows([100])
        assert flow_intensity(rows, {days[0]: Decimal(0)}, "frgn") is None

    def test_no_valid_days_returns_none(self):
        rows = _amt_rows([None, None])
        assert flow_intensity(rows, {}, "frgn") is None

    def test_negative_ratio_sign_preserved(self):
        days = _days(1)
        rows = _amt_rows([-500])
        tv = {days[0]: Decimal(10_000)}
        assert flow_intensity(rows, tv, "frgn") == Decimal("-0.0500")


# ── compute_flow_summary ──────────────────────────────────────────────


class TestComputeFlowSummary:
    def _full_rows(self, n: int = 10) -> tuple[list[InvestorFlowRecord], dict]:
        days = _days(n)
        rows = [
            _row(
                d,
                frgn_net_amt=Decimal(100),
                frgn_net_qty=10,
                orgn_net_amt=Decimal(-50),
                orgn_net_qty=-5,
                prsn_net_amt=Decimal(20),
                prsn_net_qty=2,
                scrt_net_amt=Decimal(-10),
                scrt_net_qty=-1,
            )
            for d in days
        ]
        tv = {d: Decimal(10_000) for d in days}
        return rows, tv

    def test_structure_full(self):
        rows, tv = self._full_rows(10)
        s = compute_flow_summary(rows, tv)
        assert s.symbol == "005930"
        assert s.as_of == rows[-1].date
        assert s.days_available == 10
        assert [a.axis for a in s.axes] == list(DEFAULT_AXES)
        frgn = s.axes[0]
        assert [w.window for w in frgn.windows] == list(DEFAULT_WINDOWS)
        w5, w20, _w60 = frgn.windows
        assert (w5.days_in_window, w5.days_covered) == (5, 5)
        assert (w20.days_in_window, w20.days_covered) == (10, 10)  # 행 부족 부분 관측
        assert w5.net_amt == Decimal(500)
        assert w5.net_qty == 50
        assert w5.intensity == Decimal("0.0100")
        assert frgn.streak == 10

    def test_scrt_only_has_caveat(self):
        rows, tv = self._full_rows(3)
        s = compute_flow_summary(rows, tv)
        by_axis = {a.axis: a for a in s.axes}
        assert by_axis["scrt"].caveat == SCRT_CAVEAT
        assert all(by_axis[a].caveat is None for a in ("frgn", "orgn", "prsn"))

    def test_krx_backfill_shape_rows(self):
        # krx 백필형: net만 존재, frgn_reg 등 KIS 전용 축 None — 크래시 없이 요약
        days = _days(5)
        rows = [_row(d, frgn_net_amt=Decimal(100), frgn_net_qty=10) for d in days]
        s = compute_flow_summary(rows, axes=("frgn", "frgn_reg"))
        by_axis = {a.axis: a for a in s.axes}
        assert by_axis["frgn"].windows[0].net_amt == Decimal(500)
        reg_w5 = by_axis["frgn_reg"].windows[0]
        assert reg_w5.net_amt is None
        assert reg_w5.days_covered == 0
        assert by_axis["frgn_reg"].streak == 0

    def test_no_trading_values_intensity_none(self):
        rows, _ = self._full_rows(5)
        s = compute_flow_summary(rows)
        assert all(w.intensity is None for a in s.axes for w in a.windows)
        assert s.axes[0].windows[0].net_amt == Decimal(500)  # 누적은 유효

    def test_custom_axes_and_windows(self):
        rows, tv = self._full_rows(5)
        s = compute_flow_summary(rows, tv, axes=("prsn",), windows=(3,))
        assert len(s.axes) == 1
        assert s.axes[0].axis == "prsn"
        assert [w.window for w in s.axes[0].windows] == [3]
        assert s.axes[0].windows[0].net_amt == Decimal(60)

    def test_empty_rows(self):
        s = compute_flow_summary([])
        assert s.symbol == ""
        assert s.as_of is None
        assert s.days_available == 0
        assert [a.streak for a in s.axes] == [0] * len(DEFAULT_AXES)
        assert all(w.net_amt is None for a in s.axes for w in a.windows)

    def test_json_roundtrip(self):
        rows, tv = self._full_rows(5)
        payload = json.dumps(compute_flow_summary(rows, tv).model_dump(mode="json"))
        parsed = json.loads(payload)
        assert parsed["symbol"] == "005930"
        assert parsed["axes"][0]["windows"][0]["net_amt"] == "500"

    def test_labels_mapped(self):
        rows, _ = self._full_rows(2)
        s = compute_flow_summary(rows)
        assert [a.label for a in s.axes] == ["외국인", "기관합계", "개인", "금융투자"]

    def test_invalid_axis_raises(self):
        with pytest.raises(ValueError, match="알 수 없는 투자자 축"):
            compute_flow_summary([], axes=("bogus",))


# ── compute_market_flow_summary ───────────────────────────────────────


class TestComputeMarketFlowSummary:
    def _market_rows(self, n: int = 10) -> list[MarketInvestorFlowRecord]:
        days = _days(n)
        return [
            _mrow(
                d,
                frgn_net_amt=Decimal(1_000),
                orgn_net_amt=Decimal(-500),
                prsn_net_amt=Decimal(200),
                scrt_net_amt=Decimal(-100),
                index_close=Decimal(3_000) + Decimal(10) * i,
                index_change_rate=Decimal("0.33"),
            )
            for i, d in enumerate(days)
        ]

    def test_basic_summary_intensity_always_none(self):
        rows = self._market_rows(10)
        s = compute_market_flow_summary(rows)
        assert s.market == "kospi"
        assert s.as_of == rows[-1].date
        assert s.days_available == 10
        assert [a.axis for a in s.axes] == list(DEFAULT_AXES)
        assert all(w.intensity is None for a in s.axes for w in a.windows)
        assert s.axes[0].windows[0].net_amt == Decimal(5_000)  # frgn 5일
        assert s.axes[0].streak == 10

    def test_index_window_returns(self):
        rows = self._market_rows(10)
        s = compute_market_flow_summary(rows)
        # 5일 창: 3050 → 3090 = +1.31% (소수 2자리 quantize)
        assert s.index_window_returns[5] == Decimal("1.31")
        # 20일 창(행 10개): 3000 → 3090 = +3.00%
        assert s.index_window_returns[20] == Decimal("3.00")
        assert s.index_close == Decimal(3_090)
        assert s.index_change_rate == Decimal("0.33")

    def test_insufficient_valid_closes_none(self):
        days = _days(3)
        rows = [
            _mrow(days[0], frgn_net_amt=Decimal(1)),
            _mrow(days[1], frgn_net_amt=Decimal(1)),
            _mrow(days[2], frgn_net_amt=Decimal(1), index_close=Decimal(3_000)),
        ]
        s = compute_market_flow_summary(rows)
        # 유효 종가 1개뿐 — 전 윈도 None
        assert all(v is None for v in s.index_window_returns.values())

    def test_empty_rows(self):
        s = compute_market_flow_summary([])
        assert s.market == ""
        assert s.as_of is None
        assert s.index_close is None
        assert s.index_window_returns == {5: None, 20: None, 60: None}
        assert all(w.net_amt is None for a in s.axes for w in a.windows)

    def test_kosdaq_passthrough_latest_change_rate(self):
        days = _days(2)
        rows = [
            _mrow(days[0], market="kosdaq", index_change_rate=Decimal("-1.20")),
            _mrow(days[1], market="kosdaq", index_change_rate=Decimal("2.50")),
        ]
        s = compute_market_flow_summary(rows)
        assert s.market == "kosdaq"
        assert s.index_change_rate == Decimal("2.50")


# ── 상수 계약 ─────────────────────────────────────────────────────────


class TestConstants:
    def test_axis_labels_cover_all_valid_axes(self):
        assert set(AXIS_LABELS) == set(VALID_AXES)

    def test_default_axes_valid(self):
        assert set(DEFAULT_AXES) <= VALID_AXES
        assert DEFAULT_AXES == ("frgn", "orgn", "prsn", "scrt")
