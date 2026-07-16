"""PRJ-03 단계 4 — pykrx 백필 공용 로직 단위 테스트.

네트워크·DB 불요: pykrx 프레임은 합성 DataFrame, upsert는 mock 세션의
compiled-SQL 검증(test_data_provider.py 컨벤션).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import backfill_pykrx_common as bc
import pandas as pd
import pytest
import sqlalchemy.dialects.postgresql as _pg

from src.db.models.investor_flow import InvestorFlowDaily, MarketInvestorFlowDaily
from src.db.models.market_data import DailyOHLCV

D1 = date(2026, 7, 13)
D2 = date(2026, 7, 14)

_DETAIL_COLS = [*bc.DIRECT_KRX_COLS.keys(), "외국인", "기타외국인", "전체"]


def _detail_frame(rows: dict[date, list], columns: list[str] | None = None) -> pd.DataFrame:
    """날짜 인덱스 detail=True 합성 프레임."""
    return pd.DataFrame(
        list(rows.values()),
        index=pd.DatetimeIndex([pd.Timestamp(d) for d in rows], name="날짜"),
        columns=columns or _DETAIL_COLS,
    )


# 금융투자 보험 투신 사모 은행 기타금융 연기금 기타법인 개인 외국인 기타외국인 전체
_VAL_NET_D1 = [-100, -5, 10, 20, 1, 2, 30, 7, 500, -400, -65, 0]


# ═══════════════════════════════════════════════════════════════════════
# 상수 정합성 — 모델 컬럼과의 계약
# ═══════════════════════════════════════════════════════════════════════


class TestColumnContracts:
    def test_flow_update_cols_cover_90_data_columns(self):
        assert len(bc.FLOW_UPDATE_COLS) == 90
        orm_cols = {c.name for c in InvestorFlowDaily.__table__.columns}
        assert set(bc.FLOW_UPDATE_COLS) <= orm_cols

    def test_market_update_cols_include_pykrx_only_axes(self):
        assert {"index_volume", "index_trading_value", "index_market_cap"} <= set(
            bc.MARKET_UPDATE_COLS
        )
        assert len(set(bc.MARKET_UPDATE_COLS)) == len(bc.MARKET_UPDATE_COLS)
        orm_cols = {c.name for c in MarketInvestorFlowDaily.__table__.columns}
        assert set(bc.MARKET_UPDATE_COLS) <= orm_cols

    def test_ohlcv_row_keys_match_orm(self):
        rows = bc.build_ohlcv_rows(
            "005930",
            pd.DataFrame(
                [[100, 110, 90, 105, 1000, 1.5]],
                index=pd.DatetimeIndex([pd.Timestamp(D1)], name="날짜"),
                columns=["시가", "고가", "저가", "종가", "거래량", "등락률"],
            ),
            pd.DataFrame(),
        )
        orm_cols = {c.name for c in DailyOHLCV.__table__.columns}
        assert set(rows[0]) <= orm_cols


# ═══════════════════════════════════════════════════════════════════════
# build_flow_rows
# ═══════════════════════════════════════════════════════════════════════


class TestBuildFlowRows:
    def test_axis_mapping_and_derived_sums(self):
        frames = {("value", "순매수"): _detail_frame({D1: _VAL_NET_D1})}
        rows = bc.build_flow_rows("005930", frames)
        assert len(rows) == 1
        row = rows[0]
        assert row["symbol"] == "005930"
        assert row["date"] == D1
        assert row["scrt_net_amt"] == Decimal(-100)
        assert row["insu_net_amt"] == Decimal(-5)
        assert row["ivtr_net_amt"] == Decimal(10)
        assert row["pe_fund_net_amt"] == Decimal(20)
        assert row["bank_net_amt"] == Decimal(1)
        assert row["mrbn_net_amt"] == Decimal(2)
        assert row["fund_net_amt"] == Decimal(30)
        assert row["etc_corp_net_amt"] == Decimal(7)
        assert row["prsn_net_amt"] == Decimal(500)
        # frgn = 외국인 + 기타외국인 / orgn = 기관 7축 합
        assert row["frgn_net_amt"] == Decimal(-465)
        assert row["orgn_net_amt"] == Decimal(-42)

    def test_kis_only_axes_stay_none(self):
        frames = {("value", "순매수"): _detail_frame({D1: _VAL_NET_D1})}
        row = bc.build_flow_rows("005930", frames)[0]
        for key in (
            "frgn_reg_net_amt",
            "frgn_nreg_net_amt",
            "etc_net_amt",
            "etc_orgt_net_amt",
        ):
            assert row[key] is None

    def test_on_routes_to_slot_and_kind_to_suffix(self):
        vol_net = [-10, 0, 0, 0, 0, 0, 0, 0, 9, -10, 1, 0]
        val_sell = [50, 0, 0, 0, 0, 0, 0, 0, 1000, 200, 30, 0]
        frames = {
            ("volume", "순매수"): _detail_frame({D1: vol_net}),
            ("value", "매도"): _detail_frame({D1: val_sell}),
        }
        row = bc.build_flow_rows("005930", frames)[0]
        assert row["frgn_net_qty"] == -9
        assert isinstance(row["frgn_net_qty"], int)
        assert row["prsn_sell_amt"] == Decimal(1000)
        assert row["frgn_sell_amt"] == Decimal(230)
        # 반대 슬롯은 비어 있어야 한다
        assert row["frgn_net_amt"] is None
        assert row["prsn_buy_amt"] is None

    def test_date_union_with_missing_frame_dates(self):
        frames = {
            ("value", "순매수"): _detail_frame({D1: _VAL_NET_D1, D2: _VAL_NET_D1}),
            ("volume", "순매수"): _detail_frame({D1: [1] * 12}),
        }
        rows = bc.build_flow_rows("005930", frames)
        assert [r["date"] for r in rows] == [D1, D2]
        assert rows[0]["frgn_net_qty"] == 2  # 외국인 1 + 기타외국인 1
        assert rows[1]["frgn_net_qty"] is None  # volume 프레임에 D2 부재

    def test_nan_becomes_none_and_partial_orgn_sum(self):
        with_nan = list(map(float, _VAL_NET_D1))
        with_nan[1] = float("nan")  # 보험 NaN
        frames = {("value", "순매수"): _detail_frame({D1: with_nan})}
        row = bc.build_flow_rows("005930", frames)[0]
        assert row["insu_net_amt"] is None
        # orgn은 NaN 축 제외 합: -42 - (-5) = -37
        assert row["orgn_net_amt"] == Decimal(-37)

    def test_row_keys_are_homogeneous_full_set(self):
        frames = {("value", "순매수"): _detail_frame({D1: _VAL_NET_D1})}
        row = bc.build_flow_rows("005930", frames)[0]
        assert set(row) == {"symbol", "date", *bc.FLOW_UPDATE_COLS}

    def test_empty_frames_yield_no_rows(self):
        frames = {("value", "순매수"): pd.DataFrame()}
        assert bc.build_flow_rows("005930", frames) == []


# ═══════════════════════════════════════════════════════════════════════
# build_ohlcv_rows
# ═══════════════════════════════════════════════════════════════════════


class TestBuildOhlcvRows:
    def _ohlcv_frame(self):
        d3, d4 = date(2026, 7, 15), date(2026, 7, 16)
        return pd.DataFrame(
            [
                [100.0, 110.0, 90.0, 105.0, 1000.0, 1.2345678],  # 정상
                [100.0, 110.0, 90.0, float("nan"), 0.0, 0.0],  # 종가 NaN → 제외
                [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 종가 0 → 제외
                [105.0, 106.0, 104.0, 106.0, float("nan"), -0.5],  # 거래량 NaN → 0
            ],
            index=pd.DatetimeIndex([pd.Timestamp(d) for d in (D1, D2, d3, d4)], name="날짜"),
            columns=["시가", "고가", "저가", "종가", "거래량", "등락률"],
        )

    def _cap_frame(self):
        return pd.DataFrame(
            [[1_000_000, 1000, 123_456_789, 500]],
            index=pd.DatetimeIndex([pd.Timestamp(D1)], name="날짜"),
            columns=["시가총액", "거래량", "거래대금", "상장주식수"],
        )

    def test_join_drop_and_conversions(self):
        rows = bc.build_ohlcv_rows("005930", self._ohlcv_frame(), self._cap_frame())
        assert [r["date"] for r in rows] == [D1, date(2026, 7, 16)]
        first, last = rows
        assert first["open"] == Decimal(100)
        assert first["close"] == Decimal(105)
        assert first["volume"] == 1000
        assert first["trading_value"] == Decimal(123_456_789)  # cap 조인
        assert first["change_rate"] == Decimal("1.2346")  # 소수 4자리 반올림
        assert last["trading_value"] is None  # cap에 날짜 부재
        assert last["volume"] == 0  # NaN 거래량 → 0 (NOT NULL 컬럼)

    def test_missing_rate_column_and_empty_frames(self):
        df = self._ohlcv_frame().drop(columns=["등락률"])
        rows = bc.build_ohlcv_rows("005930", df, pd.DataFrame())
        assert rows[0]["change_rate"] is None
        assert bc.build_ohlcv_rows("005930", pd.DataFrame(), pd.DataFrame()) == []


# ═══════════════════════════════════════════════════════════════════════
# build_market_rows
# ═══════════════════════════════════════════════════════════════════════


class TestBuildMarketRows:
    def test_index_block_prev_close_derivation_and_trim(self):
        d0 = date(2026, 7, 10)  # start 이전 — prev_close 시드 후 절사
        idx = pd.DataFrame(
            [
                [2000.0, 2010.0, 1990.0, 2005.0, 100, 5_000, 900_000],
                [2005.0, 2020.0, 2000.0, 2015.0, 110, 5_100, 910_000],
                [2015.0, 2030.0, 2010.0, 2010.0, 120, 5_200, 905_000],
            ],
            index=pd.DatetimeIndex([pd.Timestamp(d) for d in (d0, D1, D2)], name="날짜"),
            columns=["시가", "고가", "저가", "종가", "거래량", "거래대금", "상장시가총액"],
        )
        frames = {
            "index": idx,
            "value": _detail_frame({D1: _VAL_NET_D1}),
            "volume": _detail_frame({D1: [1] * 12}),
        }
        rows = bc.build_market_rows("kospi", frames, start=D1)
        assert [r["date"] for r in rows] == [D1, D2]

        r1 = rows[0]
        assert r1["market"] == "kospi"
        assert r1["index_close"] == Decimal(2015)
        assert r1["index_prev_close"] == Decimal(2005)  # 절사된 d0에서 시드
        assert r1["index_change"] == Decimal(10)
        assert r1["index_change_rate"] == (
            Decimal(10) / Decimal(2005) * 100
        ).quantize(Decimal("0.0001"))
        assert r1["index_volume"] == 110
        assert r1["index_trading_value"] == Decimal(5_100)
        assert r1["index_market_cap"] == Decimal(910_000)
        # 수급 net 축
        assert r1["frgn_net_amt"] == Decimal(-465)
        assert r1["orgn_net_qty"] == 7
        # D2는 지수만 있고 수급 프레임에 없음
        assert rows[1]["frgn_net_amt"] is None
        assert rows[1]["index_prev_close"] == Decimal(2015)

    def test_row_keys_homogeneous(self):
        frames = {"value": _detail_frame({D1: _VAL_NET_D1})}
        row = bc.build_market_rows("kosdaq", frames, start=D1)[0]
        assert set(row) == {"market", "date", *bc.MARKET_UPDATE_COLS}


# ═══════════════════════════════════════════════════════════════════════
# upsert_rows — compiled SQL (test_data_provider.py 컨벤션)
# ═══════════════════════════════════════════════════════════════════════


def _mock_session_factory():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    return factory, session


def _compiled_sql(session, call_index: int = -1) -> tuple[str, dict]:
    stmt = session.execute.call_args_list[call_index][0][0]
    compiled = stmt.compile(dialect=_pg.dialect())
    return str(compiled), dict(compiled.params)


def _flow_row(d: date) -> dict:
    row = {"symbol": "005930", "date": d}
    row.update(dict.fromkeys(bc.FLOW_UPDATE_COLS))
    row["frgn_net_qty"] = 10
    return row


class TestUpsertRows:
    @pytest.mark.asyncio
    async def test_update_unless_kis_sql_shape(self):
        factory, session = _mock_session_factory()
        n = await bc.upsert_rows(
            factory,
            model=InvestorFlowDaily,
            constraint="uq_investor_flow_daily_symbol_date",
            rows=[_flow_row(D1)],
            batch_size=bc.FLOW_BATCH,
            mode="update_unless_kis",
            source="krx",
            update_cols=bc.FLOW_UPDATE_COLS,
        )
        assert n == 1
        sql, params = _compiled_sql(session)
        assert "ON CONFLICT ON CONSTRAINT uq_investor_flow_daily_symbol_date" in sql
        assert "investor_flow_daily.source != " in sql  # KIS 행 불가침 가드
        assert "kis" in params.values()
        assert "krx" in params.values()  # source 주입
        assert "updated_at = now()" in sql
        assert "frgn_net_qty = excluded.frgn_net_qty" in sql
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_do_nothing_mode_for_ohlcv(self):
        factory, session = _mock_session_factory()
        rows = [
            {
                "symbol": "005930",
                "date": D1,
                "open": Decimal(100),
                "high": Decimal(110),
                "low": Decimal(90),
                "close": Decimal(105),
                "volume": 1000,
                "trading_value": None,
                "change_rate": None,
            }
        ]
        await bc.upsert_rows(
            factory,
            model=DailyOHLCV,
            constraint="uq_daily_ohlcv_symbol_date",
            rows=rows,
            batch_size=bc.OHLCV_BATCH,
            mode="do_nothing",
        )
        sql, params = _compiled_sql(session)
        assert "ON CONFLICT ON CONSTRAINT uq_daily_ohlcv_symbol_date DO NOTHING" in sql
        assert "DO UPDATE" not in sql
        assert "source" not in params  # daily_ohlcv에는 source 없음

    @pytest.mark.asyncio
    async def test_market_mode_updates_pykrx_only_index_cols(self):
        factory, session = _mock_session_factory()
        row = {"market": "kospi", "date": D1}
        row.update(dict.fromkeys(bc.MARKET_UPDATE_COLS))
        await bc.upsert_rows(
            factory,
            model=MarketInvestorFlowDaily,
            constraint="uq_market_investor_flow_daily_market_date",
            rows=[row],
            batch_size=bc.MARKET_BATCH,
            mode="update_unless_kis",
            source="krx",
            update_cols=bc.MARKET_UPDATE_COLS,
        )
        sql, _ = _compiled_sql(session)
        # KIS provider와 달리 백필은 pykrx 전용 지수 3컬럼도 SET에 포함
        assert "index_volume = excluded.index_volume" in sql
        assert "index_trading_value = excluded.index_trading_value" in sql
        assert "index_market_cap = excluded.index_market_cap" in sql

    @pytest.mark.asyncio
    async def test_batch_split_at_flow_batch_size(self):
        factory, session = _mock_session_factory()
        rows = [_flow_row(date(2020, 1, 1) + pd.Timedelta(days=i).to_pytimedelta())
                for i in range(650)]
        n = await bc.upsert_rows(
            factory,
            model=InvestorFlowDaily,
            constraint="uq_investor_flow_daily_symbol_date",
            rows=rows,
            batch_size=bc.FLOW_BATCH,
            mode="update_unless_kis",
            source="krx",
            update_cols=bc.FLOW_UPDATE_COLS,
        )
        assert session.execute.await_count == 3  # 300 + 300 + 50
        assert n == 3  # rowcount 1 × 3 배치
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_rows_no_session(self):
        factory, session = _mock_session_factory()
        n = await bc.upsert_rows(
            factory,
            model=InvestorFlowDaily,
            constraint="uq_investor_flow_daily_symbol_date",
            rows=[],
            batch_size=bc.FLOW_BATCH,
            mode="do_nothing",
        )
        assert n == 0
        factory.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# Checkpoint
# ═══════════════════════════════════════════════════════════════════════


class TestCheckpoint:
    def _cp(self):
        return bc.Checkpoint(
            start="2021-07-17",
            end="2026-07-16",
            universe=["000020", "005930", "041510"],
        )

    def test_round_trip(self, tmp_path):
        cp = self._cp()
        cp.mark_done("005930", {"flow_rows": 1187, "ohlcv_rows": 1221})
        cp.mark_failed("041510", "boom")
        cp.market_done = True
        cp.expected_trading_days = 1222
        path = tmp_path / "cp.json"
        bc.save_checkpoint(path, cp)
        loaded = bc.load_checkpoint(path)
        assert loaded == cp
        assert not path.with_suffix(".json.tmp").exists()

    def test_pending_and_retry_failed(self):
        cp = self._cp()
        cp.mark_done("005930", {})
        cp.mark_failed("041510", "boom")
        assert cp.pending() == ["000020"]
        assert cp.pending(retry_failed=True) == ["000020", "041510"]

    def test_mark_done_clears_failed(self):
        cp = self._cp()
        cp.mark_failed("005930", "boom")
        cp.mark_done("005930", {})
        assert cp.failed == {}

    def test_load_missing_returns_none(self, tmp_path):
        assert bc.load_checkpoint(tmp_path / "absent.json") is None

    def test_version_mismatch_raises(self, tmp_path):
        path = tmp_path / "cp.json"
        path.write_text('{"version": 2, "start": "a", "end": "b"}', encoding="utf-8")
        with pytest.raises(ValueError, match="버전"):
            bc.load_checkpoint(path)


# ═══════════════════════════════════════════════════════════════════════
# krx_call / force_krx_relogin
# ═══════════════════════════════════════════════════════════════════════


class TestKrxCall:
    def test_retries_with_forced_relogin_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(bc.time, "sleep", lambda s: None)
        relogins: list[int] = []
        monkeypatch.setattr(bc, "force_krx_relogin", lambda: relogins.append(1))
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ValueError("boom")
            return "ok"

        assert bc.krx_call(flaky) == "ok"
        assert calls["n"] == 3
        assert len(relogins) == 2

    def test_raises_after_exhausted_retries(self, monkeypatch):
        monkeypatch.setattr(bc.time, "sleep", lambda s: None)
        monkeypatch.setattr(bc, "force_krx_relogin", lambda: None)

        def always_fail():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            bc.krx_call(always_fail)

    def test_auth_error_aborts_immediately(self, monkeypatch):
        monkeypatch.setattr(bc.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def auth_dead():
            calls["n"] += 1
            raise bc.KrxAuthError("anonymous fallback")

        with pytest.raises(bc.KrxAuthError):
            bc.krx_call(auth_dead)
        assert calls["n"] == 1  # 재시도 없이 즉시 중단

    def test_force_relogin_raises_when_session_none(self, monkeypatch):
        import pykrx.website.comm.auth as pykrx_auth

        monkeypatch.setattr(pykrx_auth, "build_krx_session", lambda: None)
        with pytest.raises(bc.KrxAuthError, match="KRX_ID/KRX_PW"):
            bc.force_krx_relogin()

    def test_force_relogin_sets_session(self, monkeypatch):
        import pykrx.website.comm.auth as pykrx_auth

        sentinel = object()
        captured: list[object] = []
        monkeypatch.setattr(pykrx_auth, "build_krx_session", lambda: sentinel)
        monkeypatch.setattr(pykrx_auth, "set_auth_session", captured.append)
        bc.force_krx_relogin()
        assert captured == [sentinel]


# ═══════════════════════════════════════════════════════════════════════
# build_universe
# ═══════════════════════════════════════════════════════════════════════


class TestBuildUniverse:
    def test_monthly_snapshots_union_and_weekend_shift(self, monkeypatch):
        monkeypatch.setattr(bc.time, "sleep", lambda s: None)
        seen: list[str] = []
        payloads = [["000020", "005930"], ["005930", "041510"], ["041510"]]

        def fake_ticker_list(date_str: str, market: str = "ALL"):
            assert market == "ALL"
            seen.append(date_str)
            return payloads[len(seen) - 1]

        monkeypatch.setattr(
            bc, "_stock", lambda: SimpleNamespace(get_market_ticker_list=fake_ticker_list)
        )
        universe, snapshots = bc.build_universe(
            date(2026, 1, 2), date(2026, 2, 10), freq_days=30
        )
        # 스냅샷: 01-02(금) / 02-01(일)→02-02(월) 시프트 / end 02-10(화)
        assert seen == ["20260102", "20260202", "20260210"]
        assert universe == ["000020", "005930", "041510"]
        assert snapshots == [("20260102", 2), ("20260202", 2), ("20260210", 1)]
