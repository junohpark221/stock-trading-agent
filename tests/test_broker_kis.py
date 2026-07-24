"""Unit tests for KIS models, KISClient, and InMemoryBroker.

All HTTP calls are mocked — no external dependencies.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from src.broker.credentials import AccountCredentials
from src.broker.kis.auth import KISAuth
from src.broker.kis.client import KISClient
from src.broker.kis.models import (
    KISBalanceOutput1,
    KISBalanceOutput2,
    KISBalanceRlzPlOutput2,
    KISBaseResponse,
    KISDailyChartOutput,
    KISInvestorFlowOutput,
    KISLoanTransOutput,
    KISMarketInvestorFlowOutput,
    KISPriceOutput,
    KISShortSaleOutput,
    _million_krw,
    _to_decimal,
    _to_int,
)
from src.broker.mock.client import InMemoryBroker
from src.core.enums import (
    MarketType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
)
from src.core.exceptions import (
    APIError,
    BrokerError,
    InsufficientFundsError,
    KISResponseError,
    OrderError,
    RateLimitError,
    TokenExpiredError,
)
from src.core.models import OHLCV, OrderRequest, PriceInfo, StockInfo
from src.data.cache import RedisCache

from conftest import AsyncContextManagerMock, make_settings, mock_aiohttp_response


# ── Helpers ───────────────────────────────────────────────────────────


def _make_kis_client(settings=None, cache=None):
    """KISClient with mocked _session/_auth (skip connect())."""
    s = settings or make_settings()
    c = cache or MagicMock(spec=RedisCache)
    client = KISClient(settings=s, cache=c)
    client._session = MagicMock(spec=aiohttp.ClientSession)
    client._session.closed = False
    client._auth = MagicMock(spec=KISAuth)
    client._auth.get_token = AsyncMock(return_value="test_token")
    client._auth.build_headers = MagicMock(return_value={"tr_id": "TEST"})
    return client


def _build_mst_zip(lines: list[str], encoding: str = "cp949") -> bytes:
    """Build an in-memory .mst.zip with given lines."""
    content = "\n".join(lines).encode(encoding)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("test.mst", content)
    return buf.getvalue()


def _make_ok_response(output=None, output1=None, output2=None, tr_cont=""):
    """Build a KIS OK response dict."""
    data = {"rt_cd": "0", "msg_cd": "0000", "msg1": "정상처리"}
    if output is not None:
        data["output"] = output
    if output1 is not None:
        data["output1"] = output1
    if output2 is not None:
        data["output2"] = output2
    return mock_aiohttp_response(json_data=data, headers={"tr_cont": tr_cont})


# ── PRJ-03 수급 TR 픽스처 (probe1_kis_api.md 실측 응답 원문) ─────────

# FHPTJ04160001 output2 실측 행 (005930, 20260710) — 101필드 전량.
# 시세 10필드·bold_yn 포함 → extra="ignore" 배제도 함께 검증된다.
_INVESTOR_FLOW_ROW = {
    "stck_bsop_date": "20260710", "stck_clpr": "285000", "prdy_vrss": "7000",
    "prdy_vrss_sign": "2", "prdy_ctrt": "2.52", "acml_vol": "20088811",
    "acml_tr_pbmn": "5816051148000", "stck_oprc": "291000",
    "stck_hgpr": "298000", "stck_lwpr": "282000",
    "frgn_ntby_qty": "625985", "frgn_reg_ntby_qty": "635576",
    "frgn_nreg_ntby_qty": "-9591", "prsn_ntby_qty": "-2851466",
    "orgn_ntby_qty": "2313745", "scrt_ntby_qty": "1089735",
    "ivtr_ntby_qty": "205371", "pe_fund_ntby_vol": "963995",
    "bank_ntby_qty": "12672", "insu_ntby_qty": "56359",
    "mrbn_ntby_qty": "4640", "fund_ntby_qty": "-19027",
    "etc_ntby_qty": "-88264", "etc_corp_ntby_vol": "-88264",
    "etc_orgt_ntby_vol": "0", "frgn_reg_ntby_pbmn": "193652",
    "frgn_ntby_tr_pbmn": "190851", "frgn_nreg_ntby_pbmn": "-2801",
    "prsn_ntby_tr_pbmn": "-832871", "orgn_ntby_tr_pbmn": "667280",
    "scrt_ntby_tr_pbmn": "316543", "pe_fund_ntby_tr_pbmn": "278494",
    "ivtr_ntby_tr_pbmn": "58758", "bank_ntby_tr_pbmn": "3637",
    "insu_ntby_tr_pbmn": "16357", "mrbn_ntby_tr_pbmn": "1353",
    "fund_ntby_tr_pbmn": "-7863", "etc_ntby_tr_pbmn": "-25260",
    "etc_corp_ntby_tr_pbmn": "-25260", "etc_orgt_ntby_tr_pbmn": "0",
    "frgn_seln_vol": "6783626", "frgn_shnu_vol": "7409611",
    "frgn_seln_tr_pbmn": "1955385", "frgn_shnu_tr_pbmn": "2146237",
    "frgn_reg_askp_qty": "6754657", "frgn_reg_bidp_qty": "7390233",
    "frgn_reg_askp_pbmn": "1946964", "frgn_reg_bidp_pbmn": "2140616",
    "frgn_nreg_askp_qty": "28969", "frgn_nreg_bidp_qty": "19378",
    "frgn_nreg_askp_pbmn": "8422", "frgn_nreg_bidp_pbmn": "5621",
    "prsn_seln_vol": "7261142", "prsn_shnu_vol": "4409676",
    "prsn_seln_tr_pbmn": "2109725", "prsn_shnu_tr_pbmn": "1276854",
    "orgn_seln_vol": "5796961", "orgn_shnu_vol": "8110706",
    "orgn_seln_tr_pbmn": "1679520", "orgn_shnu_tr_pbmn": "2346800",
    "scrt_seln_vol": "813016", "scrt_shnu_vol": "1902751",
    "scrt_seln_tr_pbmn": "234142", "scrt_shnu_tr_pbmn": "550686",
    "ivtr_seln_vol": "460568", "ivtr_shnu_vol": "665939",
    "ivtr_seln_tr_pbmn": "132249", "ivtr_shnu_tr_pbmn": "191007",
    "pe_fund_seln_tr_pbmn": "28108", "pe_fund_seln_vol": "96281",
    "pe_fund_shnu_tr_pbmn": "306602", "pe_fund_shnu_vol": "1060276",
    "bank_seln_vol": "192", "bank_shnu_vol": "12864",
    "bank_seln_tr_pbmn": "57", "bank_shnu_tr_pbmn": "3694",
    "insu_seln_vol": "21826", "insu_shnu_vol": "78185",
    "insu_seln_tr_pbmn": "6356", "insu_shnu_tr_pbmn": "22713",
    "mrbn_seln_vol": "60", "mrbn_shnu_vol": "4700",
    "mrbn_seln_tr_pbmn": "17", "mrbn_shnu_tr_pbmn": "1371",
    "fund_seln_vol": "4405018", "fund_shnu_vol": "4385991",
    "fund_seln_tr_pbmn": "1278590", "fund_shnu_tr_pbmn": "1270727",
    "etc_seln_vol": "247082", "etc_shnu_vol": "158818",
    "etc_seln_tr_pbmn": "71421", "etc_shnu_tr_pbmn": "46160",
    "etc_orgt_seln_vol": "0", "etc_orgt_shnu_vol": "0",
    "etc_orgt_seln_tr_pbmn": "0", "etc_orgt_shnu_tr_pbmn": "0",
    "etc_corp_seln_vol": "247082", "etc_corp_shnu_vol": "158818",
    "etc_corp_seln_tr_pbmn": "71421", "etc_corp_shnu_tr_pbmn": "46160",
    "bold_yn": "N",
}

# FHPTJ04040000 output 실측 행 (KSP, 20260710) — 39필드 전량.
_MARKET_FLOW_ROW = {
    "stck_bsop_date": "20260710", "bstp_nmix_prpr": "7475.94",
    "bstp_nmix_prdy_vrss": "184.03", "prdy_vrss_sign": "2",
    "bstp_nmix_prdy_ctrt": "2.52", "bstp_nmix_oprc": "7552.49",
    "bstp_nmix_hgpr": "7704.93", "bstp_nmix_lwpr": "7429.51",
    "stck_prdy_clpr": "7291.91",
    "frgn_ntby_qty": "32633", "frgn_reg_ntby_qty": "32490",
    "frgn_nreg_ntby_qty": "144", "prsn_ntby_qty": "-37060",
    "orgn_ntby_qty": "5190", "scrt_ntby_qty": "3809",
    "ivtr_ntby_qty": "420", "pe_fund_ntby_vol": "726",
    "bank_ntby_qty": "-1437", "insu_ntby_qty": "283",
    "mrbn_ntby_qty": "243", "fund_ntby_qty": "1145",
    "etc_ntby_qty": "-762", "etc_orgt_ntby_vol": "0",
    "etc_corp_ntby_vol": "-762",
    "frgn_ntby_tr_pbmn": "-322775", "frgn_reg_ntby_pbmn": "-330147",
    "frgn_nreg_ntby_pbmn": "7372", "prsn_ntby_tr_pbmn": "-780456",
    "orgn_ntby_tr_pbmn": "1131391", "scrt_ntby_tr_pbmn": "1134892",
    "ivtr_ntby_tr_pbmn": "-430258", "pe_fund_ntby_tr_pbmn": "280431",
    "bank_ntby_tr_pbmn": "6943", "insu_ntby_tr_pbmn": "57923",
    "mrbn_ntby_tr_pbmn": "45994", "fund_ntby_tr_pbmn": "35467",
    "etc_ntby_tr_pbmn": "-28160", "etc_orgt_ntby_tr_pbmn": "0",
    "etc_corp_ntby_tr_pbmn": "-28160",
}

# FHPST04830000 output2 실측 행 (005930, 20260710) — 21필드 전량.
# 시세·조회창 의존 누적 필드 포함 → ignore 배제 검증 겸용.
_SHORT_SALE_ROW = {
    "stck_bsop_date": "20260710", "stck_clpr": "285000", "prdy_vrss": "7000",
    "prdy_vrss_sign": "2", "prdy_ctrt": "2.52", "acml_vol": "20088811",
    "stnd_vol_smtn": "1857451028", "ssts_cntg_qty": "225060",
    "ssts_vol_rlim": "1.12", "acml_ssts_cntg_qty": "56509707",
    "acml_ssts_cntg_qty_rlim": "3.04", "acml_tr_pbmn": "5816051148000",
    "stnd_tr_pbmn_smtn": "545375592278777", "ssts_tr_pbmn": "65122268750",
    "ssts_tr_pbmn_rlim": "1.12", "acml_ssts_tr_pbmn": "16735690205106",
    "acml_ssts_tr_pbmn_rlim": "3.07", "stck_oprc": "291000",
    "stck_hgpr": "298000", "stck_lwpr": "282000", "avrg_prc": "289355",
}

# HHPST074500C0 output1 실측 행 — ⚠️ 시장 단위 오호출 실측(스키마 가정용).
# 종목 모드("3") 실측 행 — prj03_stage2_verify.md (2026-07-15, 005930)
_LOAN_TRANS_ROW = {
    "bsop_date": "20260710", "stck_prpr": "279500.00", "prdy_vrss_sign": "2",
    "prdy_vrss": "16500.00", "prdy_ctrt": "6.27", "acml_vol": "24873414",
    "new_stcn": "1575049", "rdmp_stcn": "2198394",
    "prdy_rmnd_vrss": "-623345", "rmnd_stcn": "82414108",
    "rmnd_amt": "23034743",
}


def _investor_flow_row(date_str="20260710", **overrides):
    row = dict(_INVESTOR_FLOW_ROW, stck_bsop_date=date_str)
    row.update(overrides)
    return row


def _market_flow_row(date_str="20260710", **overrides):
    row = dict(_MARKET_FLOW_ROW, stck_bsop_date=date_str)
    row.update(overrides)
    return row


def _short_sale_row(date_str="20260710", **overrides):
    row = dict(_SHORT_SALE_ROW, stck_bsop_date=date_str)
    row.update(overrides)
    return row


def _loan_trans_row(date_str="20260710", **overrides):
    row = dict(_LOAN_TRANS_ROW, bsop_date=date_str)
    row.update(overrides)
    return row


def _daily_rows(factory, count, newest: date):
    """newest부터 하루씩 과거로 count개 행 생성 (날짜 내림차순 — KIS 응답 순서)."""
    return [
        factory((newest - timedelta(days=i)).strftime("%Y%m%d")) for i in range(count)
    ]


# ═══════════════════════════════════════════════════════════════════════
# 2.1 KIS Models
# ═══════════════════════════════════════════════════════════════════════


class TestToDecimal:
    def test_valid_decimal(self):
        assert _to_decimal("72000.50") == Decimal("72000.50")

    def test_valid_integer(self):
        assert _to_decimal("100") == Decimal("100")

    def test_empty_string(self):
        assert _to_decimal("") == Decimal(0)

    def test_whitespace_only(self):
        assert _to_decimal("  ") == Decimal(0)

    def test_invalid_string(self):
        assert _to_decimal("abc") == Decimal(0)

    def test_negative(self):
        assert _to_decimal("-500") == Decimal("-500")

    def test_strips_whitespace(self):
        assert _to_decimal(" 123 ") == Decimal("123")


class TestToInt:
    def test_valid(self):
        assert _to_int("42") == 42

    def test_empty(self):
        assert _to_int("") == 0

    def test_whitespace(self):
        assert _to_int("   ") == 0

    def test_invalid(self):
        assert _to_int("abc") == 0

    def test_float_string(self):
        assert _to_int("3.14") == 0

    def test_negative(self):
        assert _to_int("-10") == -10

    def test_strips(self):
        assert _to_int(" 99 ") == 99


class TestKISBaseResponse:
    def test_is_ok_true(self):
        r = KISBaseResponse(rt_cd="0")
        assert r.is_ok is True

    def test_is_ok_false(self):
        r = KISBaseResponse(rt_cd="1")
        assert r.is_ok is False

    def test_extra_fields_ignored(self):
        r = KISBaseResponse(rt_cd="0", unknown_field="hello")
        assert r.is_ok is True


class TestKISPriceOutput:
    def test_to_domain_positive_sign(self):
        out = KISPriceOutput(
            stck_prpr="72000", prdy_vrss="500", prdy_vrss_sign="2",
            prdy_ctrt="0.70", acml_vol="15000000",
        )
        pi = out.to_domain("005930")
        assert pi.change_price == Decimal("500")
        assert pi.change_percent == Decimal("0.70")

    def test_to_domain_negative_sign_4(self):
        out = KISPriceOutput(
            stck_prpr="71000", prdy_vrss="500", prdy_vrss_sign="4",
            prdy_ctrt="0.70",
        )
        pi = out.to_domain("005930")
        assert pi.change_price == Decimal("-500")
        assert pi.change_percent == Decimal("-0.70")

    def test_to_domain_negative_sign_5(self):
        out = KISPriceOutput(
            stck_prpr="71000", prdy_vrss="1000", prdy_vrss_sign="5",
            prdy_ctrt="1.40",
        )
        pi = out.to_domain("005930")
        assert pi.change_price == Decimal("-1000")

    def test_to_domain_flat_sign_3(self):
        out = KISPriceOutput(
            stck_prpr="72000", prdy_vrss="0", prdy_vrss_sign="3",
            prdy_ctrt="0",
        )
        pi = out.to_domain("005930")
        assert pi.change_price == Decimal(0)

    def test_to_domain_returns_price_info(self):
        out = KISPriceOutput(
            stck_prpr="72000", prdy_vrss="500", prdy_vrss_sign="2",
            prdy_ctrt="0.70", acml_vol="15000000", stck_hgpr="72500",
            stck_lwpr="71000", stck_sdpr="71500",
        )
        pi = out.to_domain("005930")
        assert pi.symbol == "005930"
        assert pi.current_price == Decimal("72000")
        assert pi.previous_close == Decimal("71500")
        assert pi.high == Decimal("72500")
        assert pi.low == Decimal("71000")
        assert pi.volume == 15000000
        assert pi.timestamp is not None


class TestKISDailyChartOutput:
    def test_to_domain_date_parsing(self):
        out = KISDailyChartOutput(stck_bsop_date="20260301", stck_clpr="100")
        bar = out.to_domain("005930")
        assert bar.date == date(2026, 3, 1)

    def test_to_domain_fields_mapped(self):
        out = KISDailyChartOutput(
            stck_bsop_date="20260301", stck_clpr="72000",
            stck_oprc="71500", stck_hgpr="72500", stck_lwpr="71000",
            acml_vol="15000000", acml_tr_pbmn="1080000000000",
        )
        bar = out.to_domain("005930")
        assert bar.open == Decimal("71500")
        assert bar.high == Decimal("72500")
        assert bar.low == Decimal("71000")
        assert bar.close == Decimal("72000")
        assert bar.volume == 15000000
        assert bar.value == Decimal("1080000000000")

    def test_to_domain_zero_values(self):
        out = KISDailyChartOutput(stck_bsop_date="20260301")
        bar = out.to_domain("005930")
        assert bar.open == Decimal(0)
        assert bar.volume == 0


class TestMillionKrw:
    def test_converts_million_to_krw(self):
        assert _million_krw("193652") == Decimal("193652000000")

    def test_negative(self):
        assert _million_krw("-2801") == Decimal("-2801000000")

    def test_empty(self):
        assert _million_krw("") == Decimal(0)


class TestKISInvestorFlowOutput:
    """FHPTJ04160001 — probe1 실측 행 기반 매핑·단위 검증."""

    def _domain(self, **overrides):
        out = KISInvestorFlowOutput.model_validate(_investor_flow_row(**overrides))
        return out.to_domain("005930")

    def test_date_and_symbol(self):
        rec = self._domain()
        assert rec.symbol == "005930"
        assert rec.date == date(2026, 7, 10)

    def test_standard_axis_mapping(self):
        rec = self._domain()
        # frgn: 표준 패턴 (ntby_qty / ntby_tr_pbmn / seln_vol / shnu_vol)
        assert rec.frgn_net_qty == 625985
        assert rec.frgn_net_amt == Decimal("190851000000")
        assert rec.frgn_sell_qty == 6783626
        assert rec.frgn_buy_qty == 7409611
        assert rec.frgn_sell_amt == Decimal("1955385000000")
        assert rec.frgn_buy_amt == Decimal("2146237000000")

    def test_quirk_net_qty_ntby_vol(self):
        """pe_fund/etc_corp/etc_orgt는 KIS 원명이 _ntby_vol (_ntby_qty 아님)."""
        rec = self._domain()
        assert rec.pe_fund_net_qty == 963995
        assert rec.etc_corp_net_qty == -88264
        assert rec.etc_orgt_net_qty == 0

    def test_quirk_net_amt_ntby_pbmn(self):
        """frgn_reg/frgn_nreg net 대금은 KIS 원명이 _ntby_pbmn (_ntby_tr_pbmn 아님)."""
        rec = self._domain()
        assert rec.frgn_reg_net_amt == Decimal("193652000000")
        assert rec.frgn_nreg_net_amt == Decimal("-2801000000")

    def test_quirk_askp_bidp_sell_buy(self):
        """frgn_reg/frgn_nreg 매도/매수는 KIS 원명이 askp/bidp (seln/shnu 아님)."""
        rec = self._domain()
        assert rec.frgn_reg_sell_qty == 6754657
        assert rec.frgn_reg_buy_qty == 7390233
        assert rec.frgn_reg_sell_amt == Decimal("1946964000000")
        assert rec.frgn_reg_buy_amt == Decimal("2140616000000")
        assert rec.frgn_nreg_sell_qty == 28969
        assert rec.frgn_nreg_buy_qty == 19378

    def test_negative_net_preserved(self):
        """net 값은 원천 부호 그대로 — 부호 재처리 없음."""
        rec = self._domain()
        assert rec.prsn_net_qty == -2851466
        assert rec.prsn_net_amt == Decimal("-832871000000")
        assert rec.fund_net_qty == -19027

    def test_price_fields_ignored(self):
        """시세 10필드·bold_yn은 extra="ignore"로 배제 (확정 11)."""
        out = KISInvestorFlowOutput.model_validate(_investor_flow_row())
        assert not hasattr(out, "stck_clpr")
        assert not hasattr(out, "acml_vol")
        assert not hasattr(out, "bold_yn")

    def test_empty_fields_default_zero(self):
        out = KISInvestorFlowOutput(stck_bsop_date="20260710")
        rec = out.to_domain("005930")
        assert rec.frgn_net_qty == 0
        assert rec.frgn_net_amt == Decimal(0)
        assert rec.etc_orgt_buy_amt == Decimal(0)


class TestKISMarketInvestorFlowOutput:
    """FHPTJ04040000 — probe1 실측 행(KSP) 기반 매핑·단위 검증."""

    def _domain(self, market="kospi", **overrides):
        out = KISMarketInvestorFlowOutput.model_validate(_market_flow_row(**overrides))
        return out.to_domain(market)

    def test_index_ohlc_mapping(self):
        rec = self._domain()
        assert rec.market == "kospi"
        assert rec.date == date(2026, 7, 10)
        assert rec.index_open == Decimal("7552.49")
        assert rec.index_high == Decimal("7704.93")
        assert rec.index_low == Decimal("7429.51")
        assert rec.index_close == Decimal("7475.94")
        assert rec.index_prev_close == Decimal("7291.91")
        assert rec.index_change == Decimal("184.03")
        assert rec.index_change_rate == Decimal("2.52")

    def test_sign_5_negates_change(self):
        rec = self._domain(prdy_vrss_sign="5")
        assert rec.index_change == Decimal("-184.03")
        assert rec.index_change_rate == Decimal("-2.52")

    def test_net_qty_thousand_shares_scaled(self):
        """*_ntby_qty는 천주 단위 실측 확정(2026-07-15 EC2) — ×1000 주 단위 변환 고정."""
        rec = self._domain()
        assert rec.frgn_net_qty == 32_633_000
        assert rec.prsn_net_qty == -37_060_000

    def test_net_amt_million_krw(self):
        rec = self._domain()
        assert rec.frgn_net_amt == Decimal("-322775000000")
        assert rec.orgn_net_amt == Decimal("1131391000000")

    def test_quirk_fields(self):
        rec = self._domain()
        assert rec.pe_fund_net_qty == 726_000      # pe_fund_ntby_vol (천주 ×1000)
        assert rec.etc_corp_net_qty == -762_000    # etc_corp_ntby_vol (천주 ×1000)
        assert rec.frgn_reg_net_amt == Decimal("-330147000000")  # frgn_reg_ntby_pbmn


class TestKISShortSaleOutput:
    """FHPST04830000 — probe1 실측 행 기반 매핑·단위 검증."""

    def test_field_mapping(self):
        out = KISShortSaleOutput.model_validate(_short_sale_row())
        rec = out.to_domain("005930")
        assert rec.symbol == "005930"
        assert rec.date == date(2026, 7, 10)
        assert rec.short_sale_qty == 225060
        assert rec.short_sale_vol_ratio == Decimal("1.12")
        assert rec.short_sale_amt_ratio == Decimal("1.12")
        assert rec.avg_price == Decimal("289355")

    def test_amt_is_krw_not_million(self):
        """ssts_tr_pbmn은 필드명과 달리 원 단위 실측 — ×1e6 회귀 방지 고정."""
        out = KISShortSaleOutput.model_validate(_short_sale_row())
        rec = out.to_domain("005930")
        assert rec.short_sale_amt == Decimal("65122268750")

    def test_window_dependent_fields_ignored(self):
        """조회 창 의존 누적 필드(acml_ssts_*, stnd_*_smtn)는 배제 (단계 1 확정 ②)."""
        out = KISShortSaleOutput.model_validate(_short_sale_row())
        assert not hasattr(out, "acml_ssts_cntg_qty")
        assert not hasattr(out, "stnd_vol_smtn")
        assert not hasattr(out, "stck_clpr")


class TestKISLoanTransOutput:
    """HHPST074500C0 — 종목 모드 실측 행(2026-07-15 EC2) 기반 매핑·단위 검증."""

    def test_field_mapping(self):
        out = KISLoanTransOutput.model_validate(_loan_trans_row())
        rec = out.to_domain("005930")
        assert rec.symbol == "005930"
        assert rec.date == date(2026, 7, 10)
        assert rec.loan_new_qty == 1575049
        assert rec.loan_redemption_qty == 2198394
        assert rec.loan_balance_qty == 82414108

    def test_negative_balance_diff(self):
        out = KISLoanTransOutput.model_validate(_loan_trans_row())
        rec = out.to_domain("005930")
        assert rec.loan_balance_diff == -623345

    def test_balance_amt_million_krw(self):
        """rmnd_amt는 백만원 단위 실측 확정 — ×1e6 고정
        (검산: 잔고 82,414,108주 × 주가 279,500원 ≈ 23.03조원)."""
        out = KISLoanTransOutput.model_validate(_loan_trans_row())
        rec = out.to_domain("005930")
        assert rec.loan_balance_amt == Decimal("23034743000000")


class TestKISBalanceOutput1:
    def test_to_domain_open(self):
        out = KISBalanceOutput1(pdno="005930", hldg_qty="10", prpr="72000")
        pos = out.to_domain()
        assert pos.status == PositionStatus.OPEN
        assert pos.quantity == 10

    def test_to_domain_closed(self):
        out = KISBalanceOutput1(pdno="005930", hldg_qty="0")
        pos = out.to_domain()
        assert pos.status == PositionStatus.CLOSED

    def test_to_domain_field_mapping(self):
        out = KISBalanceOutput1(
            pdno="005930", hldg_qty="10", pchs_avg_pric="70000",
            prpr="72000", evlu_amt="720000", evlu_pfls_amt="20000",
            evlu_pfls_rt="2.86",
        )
        pos = out.to_domain()
        assert pos.symbol == "005930"
        assert pos.average_cost == Decimal("70000")
        assert pos.current_price == Decimal("72000")
        assert pos.market_value == Decimal("720000")


class TestKISBalanceRlzPlOutput2:
    """TTTC8494R output2(실현손익 요약) 파싱."""

    def test_parses_realized_fields(self):
        out = KISBalanceRlzPlOutput2(rlzt_pfls="123456", rlzt_erng_rt="2.34")
        assert _to_decimal(out.rlzt_pfls) == Decimal("123456")
        assert _to_decimal(out.rlzt_erng_rt) == Decimal("2.34")

    def test_defaults_empty(self):
        out = KISBalanceRlzPlOutput2()
        assert _to_decimal(out.rlzt_pfls) == Decimal(0)
        assert _to_decimal(out.rlzt_erng_rt) == Decimal(0)

    def test_ignores_extra_fields(self):
        out = KISBalanceRlzPlOutput2.model_validate(
            {"rlzt_pfls": "-5000", "rlzt_erng_rt": "-1.2", "dnca_tot_amt": "999"}
        )
        assert _to_decimal(out.rlzt_pfls) == Decimal("-5000")


# ═══════════════════════════════════════════════════════════════════════
# 2.2 KISClient
# ═══════════════════════════════════════════════════════════════════════


class TestKISClientLifecycle:
    @pytest.mark.asyncio
    async def test_connect_creates_session_and_auth(self):
        client = KISClient(settings=make_settings(), cache=MagicMock(spec=RedisCache))
        with (
            patch("src.broker.kis.client.aiohttp.ClientSession") as mock_sess_cls,
            patch("src.broker.kis.client.KISAuth") as mock_auth_cls,
        ):
            mock_session = MagicMock()
            mock_sess_cls.return_value = mock_session
            mock_auth = MagicMock()
            mock_auth.get_token = AsyncMock(return_value="token")
            mock_auth_cls.return_value = mock_auth

            await client.connect()

            mock_sess_cls.assert_called_once()
            mock_auth_cls.assert_called_once()
            mock_auth.get_token.assert_awaited_once()
            assert client._session is mock_session

    @pytest.mark.asyncio
    async def test_disconnect_closes_session(self):
        client = _make_kis_client()
        session = client._session
        session.closed = False
        session.close = AsyncMock()

        await client.disconnect()

        session.close.assert_awaited_once()
        assert client._session is None
        assert client._auth is None

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self):
        client = KISClient(settings=make_settings(), cache=MagicMock(spec=RedisCache))
        await client.disconnect()  # no error

    @pytest.mark.asyncio
    async def test_context_manager(self):
        with (
            patch("src.broker.kis.client.aiohttp.ClientSession") as mock_sess_cls,
            patch("src.broker.kis.client.KISAuth") as mock_auth_cls,
        ):
            mock_session = MagicMock()
            mock_session.closed = False
            mock_session.close = AsyncMock()
            mock_sess_cls.return_value = mock_session
            mock_auth = MagicMock()
            mock_auth.get_token = AsyncMock(return_value="token")
            mock_auth_cls.return_value = mock_auth

            client = KISClient(settings=make_settings(), cache=MagicMock(spec=RedisCache))
            async with client:
                assert client._session is not None
            assert client._session is None


class TestKISClientRequest:
    @pytest.mark.asyncio
    async def test_get_request_success(self):
        client = _make_kis_client()
        resp = _make_ok_response(output={"stck_prpr": "72000"})
        client._session.get = AsyncMock(return_value=resp)

        data = await client._request("GET", "/test", "FHKST01010100", params={"k": "v"})
        assert data["output"]["stck_prpr"] == "72000"
        client._session.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_post_request_success(self):
        client = _make_kis_client()
        resp = _make_ok_response(output={"ODNO": "123"})
        client._session.post = AsyncMock(return_value=resp)

        data = await client._request("POST", "/test", "TTTC0012U", body={"k": "v"})
        assert data["output"]["ODNO"] == "123"
        client._session.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_not_connected_raises(self):
        client = _make_kis_client()
        client._session = None
        with pytest.raises(BrokerError, match="not connected"):
            await client._request("GET", "/test", "TEST")

    @pytest.mark.asyncio
    async def test_network_error_raises_api_error(self):
        client = _make_kis_client()
        client._session.get = AsyncMock(side_effect=aiohttp.ClientError("fail"))
        with pytest.raises(APIError, match="KIS request failed"):
            await client._request("GET", "/test", "TEST")

    @pytest.mark.asyncio
    async def test_json_parse_error(self):
        client = _make_kis_client()
        resp = MagicMock()
        resp.json = AsyncMock(side_effect=ValueError("bad json"))
        resp.text = AsyncMock(return_value="not json")
        resp.headers = {"tr_cont": ""}
        client._session.get = AsyncMock(return_value=resp)
        with pytest.raises(APIError, match="JSON parse error"):
            await client._request("GET", "/test", "TEST")

    @pytest.mark.asyncio
    async def test_rate_limit_sleep(self):
        client = _make_kis_client(settings=make_settings(KIS_RATE_LIMIT_INTERVAL=0.1))
        resp = _make_ok_response()
        client._session.get = AsyncMock(return_value=resp)

        with patch("src.broker.kis.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await client._request("GET", "/test", "TEST")
            mock_sleep.assert_awaited_once_with(0.1)

    @pytest.mark.asyncio
    async def test_tr_cont_header_injected(self):
        client = _make_kis_client()
        resp = _make_ok_response(tr_cont="M")
        client._session.get = AsyncMock(return_value=resp)

        data = await client._request("GET", "/test", "TEST")
        assert data["_tr_cont"] == "M"


class TestKISClientHandleError:
    @pytest.mark.asyncio
    async def test_token_expired_egw00123_retries(self):
        client = _make_kis_client()
        err_resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "expired"},
            headers={"tr_cont": ""},
        )
        ok_resp = _make_ok_response(output={"result": "ok"})
        client._session.get = AsyncMock(side_effect=[err_resp, ok_resp])
        client._auth.refresh_token = AsyncMock()

        data = await client._request("GET", "/test", "TEST")
        assert data["output"]["result"] == "ok"
        client._auth.refresh_token.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_token_expired_egw00121_retries(self):
        client = _make_kis_client()
        err_resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "EGW00121", "msg1": "expired"},
            headers={"tr_cont": ""},
        )
        ok_resp = _make_ok_response(output={"result": "ok"})
        client._session.get = AsyncMock(side_effect=[err_resp, ok_resp])
        client._auth.refresh_token = AsyncMock()

        data = await client._request("GET", "/test", "TEST")
        client._auth.refresh_token.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_token_expired_on_retry_raises(self):
        client = _make_kis_client()
        err_resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "expired"},
            headers={"tr_cont": ""},
        )
        # Both calls return error
        client._session.get = AsyncMock(side_effect=[err_resp, err_resp])
        client._auth.refresh_token = AsyncMock()

        with pytest.raises(TokenExpiredError):
            await client._request("GET", "/test", "TEST")

    @pytest.mark.asyncio
    async def test_rate_limit_egw00201_exhausts_retries(self):
        """max_retries(3)회 재시도 후에도 rate limit이면 RateLimitError 발생."""
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "rate limit"},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)
        with patch("src.broker.kis.client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RateLimitError, match="after 3 retries"):
                await client._request("GET", "/test", "TEST")
        # 최초 1회 + 재시도 3회 = 총 4회 호출
        assert client._session.get.await_count == 4

    @pytest.mark.asyncio
    async def test_rate_limit_retry_then_success(self):
        """1회 rate limit 후 2회째 성공하면 정상 반환."""
        client = _make_kis_client()
        err_resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "rate limit"},
            headers={"tr_cont": ""},
        )
        ok_resp = _make_ok_response(output={"result": "ok"})
        client._session.get = AsyncMock(side_effect=[err_resp, ok_resp])
        with patch("src.broker.kis.client.asyncio.sleep", new_callable=AsyncMock):
            data = await client._request("GET", "/test", "TEST")
        assert data["output"]["result"] == "ok"
        assert client._session.get.await_count == 2

    @pytest.mark.asyncio
    async def test_rate_limit_backoff_sleep(self):
        """재시도 시 지수 백오프로 sleep이 호출되는지 확인."""
        client = _make_kis_client()
        client._rate_limit_backoff_base = 1.0
        client._max_rate_limit_retries = 3
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "rate limit"},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)
        with patch("src.broker.kis.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(RateLimitError):
                await client._request("GET", "/test", "TEST")
        # 일반 rate limit sleep(semaphore 내) 4회 + backoff sleep 3회
        backoff_calls = [c.args[0] for c in mock_sleep.await_args_list]
        # backoff: 1.0, 2.0, 4.0 이 포함되어야 함
        assert 1.0 in backoff_calls
        assert 2.0 in backoff_calls
        assert 4.0 in backoff_calls

    @pytest.mark.asyncio
    async def test_rate_limit_egw00215_retry_then_success(self):
        """원장 초당한도(EGW00215)도 게이트웨이 한도처럼 백오프 재시도 후 성공 (F-20)."""
        client = _make_kis_client()
        err_resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "EGW00215", "msg1": "ledger rate limit"},
            headers={"tr_cont": ""},
        )
        ok_resp = _make_ok_response(output={"result": "ok"})
        client._session.get = AsyncMock(side_effect=[err_resp, ok_resp])
        with patch("src.broker.kis.client.asyncio.sleep", new_callable=AsyncMock):
            data = await client._request("GET", "/test", "TEST")
        assert data["output"]["result"] == "ok"
        assert client._session.get.await_count == 2

    @pytest.mark.asyncio
    async def test_rate_limit_egw00215_exhausts_retries(self):
        """EGW00215도 재시도 소진 시 RateLimitError(현행 실패 의미론 유지) (F-20)."""
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "EGW00215", "msg1": "ledger rate limit"},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)
        with patch("src.broker.kis.client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RateLimitError, match=r"EGW00215.*after 3 retries"):
                await client._request("GET", "/test", "TEST")
        assert client._session.get.await_count == 4

    @pytest.mark.asyncio
    async def test_insufficient_funds_apbk0013(self):
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "APBK0013", "msg1": "no funds"},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)
        with pytest.raises(InsufficientFundsError):
            await client._request("GET", "/test", "TEST")

    @pytest.mark.asyncio
    async def test_generic_error(self):
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "UNKNOWN", "msg1": "something"},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)
        with pytest.raises(KISResponseError) as exc_info:
            await client._request("GET", "/test", "TEST")
        assert exc_info.value.msg_cd == "UNKNOWN"
        assert exc_info.value.msg1 == "something"


class TestKISClientGetPrice:
    @pytest.mark.asyncio
    async def test_returns_price_info(self):
        client = _make_kis_client()
        resp = _make_ok_response(output={
            "stck_prpr": "72000", "prdy_vrss": "500", "prdy_vrss_sign": "2",
            "prdy_ctrt": "0.70", "acml_vol": "15000000", "stck_hgpr": "72500",
            "stck_lwpr": "71000", "stck_sdpr": "71500",
        })
        client._session.get = AsyncMock(return_value=resp)

        pi = await client.get_price("005930")
        assert pi.symbol == "005930"
        assert pi.current_price == Decimal("72000")

    @pytest.mark.asyncio
    async def test_empty_output(self):
        client = _make_kis_client()
        resp = _make_ok_response(output={})
        client._session.get = AsyncMock(return_value=resp)

        pi = await client.get_price("005930")
        assert pi.current_price == Decimal(0)


class TestKISClientGetDailyOHLCV:
    def _make_items(self, count, start_date_str="20260201"):
        """Generate count items with sequential dates."""
        items = []
        y, m, d = int(start_date_str[:4]), int(start_date_str[4:6]), int(start_date_str[6:8])
        for i in range(count):
            dt = date(y, m, d) - __import__("datetime").timedelta(days=i)
            items.append({
                "stck_bsop_date": dt.strftime("%Y%m%d"),
                "stck_clpr": "72000", "stck_oprc": "71500",
                "stck_hgpr": "72500", "stck_lwpr": "71000",
                "acml_vol": "15000000",
            })
        return items

    @pytest.mark.asyncio
    async def test_single_page_under_100(self):
        client = _make_kis_client()
        items = self._make_items(50)
        resp = _make_ok_response(output2=items)
        client._session.get = AsyncMock(return_value=resp)

        bars = await client.get_daily_ohlcv("005930", period_days=100)
        assert len(bars) == 50

    @pytest.mark.asyncio
    async def test_pagination_two_pages(self):
        client = _make_kis_client()
        page1 = self._make_items(100, "20260301")
        page2 = self._make_items(30, "20251122")

        resp1 = _make_ok_response(output2=page1)
        resp2 = _make_ok_response(output2=page2)
        client._session.get = AsyncMock(side_effect=[resp1, resp2])

        bars = await client.get_daily_ohlcv("005930", period_days=365)
        # 100 + 30 = 130, minus any date overlap between pages
        assert len(bars) >= 129
        assert client._session.get.await_count == 2

    @pytest.mark.asyncio
    async def test_empty_result(self):
        client = _make_kis_client()
        resp = _make_ok_response(output2=[])
        client._session.get = AsyncMock(return_value=resp)

        bars = await client.get_daily_ohlcv("005930")
        assert bars == []

    @pytest.mark.asyncio
    async def test_dedup_same_date(self):
        client = _make_kis_client()
        items = [
            {"stck_bsop_date": "20260301", "stck_clpr": "72000", "stck_oprc": "71500",
             "stck_hgpr": "72500", "stck_lwpr": "71000", "acml_vol": "15000000"},
            {"stck_bsop_date": "20260301", "stck_clpr": "72100", "stck_oprc": "71600",
             "stck_hgpr": "72600", "stck_lwpr": "71100", "acml_vol": "15000001"},
        ]
        resp = _make_ok_response(output2=items)
        client._session.get = AsyncMock(return_value=resp)

        bars = await client.get_daily_ohlcv("005930")
        assert len(bars) == 1

    @pytest.mark.asyncio
    async def test_skips_empty_bsop_date(self):
        client = _make_kis_client()
        items = [
            {"stck_bsop_date": "", "stck_clpr": "72000"},
            {"stck_bsop_date": "20260301", "stck_clpr": "72000", "stck_oprc": "71500",
             "stck_hgpr": "72500", "stck_lwpr": "71000", "acml_vol": "15000000"},
        ]
        resp = _make_ok_response(output2=items)
        client._session.get = AsyncMock(return_value=resp)

        bars = await client.get_daily_ohlcv("005930")
        assert len(bars) == 1

    @pytest.mark.asyncio
    async def test_max_pages_safety(self):
        client = _make_kis_client()
        # Always return 100 items with unique dates
        call_count = 0

        async def _side_effect(*args, **kwargs):
            nonlocal call_count
            items = self._make_items(100, f"2026{(call_count + 1):02d}01")
            call_count += 1
            return _make_ok_response(output2=items)

        client._session.get = AsyncMock(side_effect=_side_effect)

        bars = await client.get_daily_ohlcv("005930", period_days=10000)
        assert client._session.get.await_count <= 20


class TestKISClientGetInvestorFlow:
    @pytest.mark.asyncio
    async def test_single_call_without_start_date(self):
        """start_date 미지정 → 재앵커 없이 정확히 1콜, 날짜 오름차순 반환."""
        client = _make_kis_client()
        rows = _daily_rows(_investor_flow_row, 30, date(2026, 7, 10))
        client._session.get = AsyncMock(return_value=_make_ok_response(output2=rows))

        records = await client.get_investor_flow("005930")
        assert client._session.get.await_count == 1
        assert len(records) == 30
        assert records[0].date < records[-1].date
        assert records[-1].date == date(2026, 7, 10)

    @pytest.mark.asyncio
    async def test_request_params(self):
        client = _make_kis_client()
        rows = _daily_rows(_investor_flow_row, 30, date(2026, 7, 10))
        client._session.get = AsyncMock(return_value=_make_ok_response(output2=rows))

        await client.get_investor_flow("005930", anchor_date=date(2026, 7, 10))
        params = client._session.get.call_args.kwargs["params"]
        assert params["FID_COND_MRKT_DIV_CODE"] == "J"
        assert params["FID_INPUT_ISCD"] == "005930"
        assert params["FID_INPUT_DATE_1"] == "20260710"

    @pytest.mark.asyncio
    async def test_reanchor_extension(self):
        """start_date 지정 → oldest-1일 재앵커, 경계 중복 dedupe, 범위 필터."""
        client = _make_kis_client()
        page1 = _daily_rows(_investor_flow_row, 30, date(2026, 7, 10))  # ~20260611
        page2 = _daily_rows(_investor_flow_row, 30, date(2026, 6, 10))  # ~20260512
        client._session.get = AsyncMock(side_effect=[
            _make_ok_response(output2=page1),
            _make_ok_response(output2=page2),
        ])

        records = await client.get_investor_flow(
            "005930", start_date=date(2026, 5, 20), anchor_date=date(2026, 7, 10)
        )
        assert client._session.get.await_count == 2
        # 2콜째 앵커 = 1페이지 oldest(20260611) - 1일
        params2 = client._session.get.call_args_list[1].kwargs["params"]
        assert params2["FID_INPUT_DATE_1"] == "20260610"
        # start_date 미만 필터 + 오름차순
        assert records[0].date == date(2026, 5, 20)
        assert records[-1].date == date(2026, 7, 10)
        assert all(r.date >= date(2026, 5, 20) for r in records)

    @pytest.mark.asyncio
    async def test_under_30_rows_stops(self):
        """앵커당 30행 미만 → 과거 끝으로 판단하고 재앵커 중단."""
        client = _make_kis_client()
        rows = _daily_rows(_investor_flow_row, 10, date(2026, 7, 10))
        client._session.get = AsyncMock(return_value=_make_ok_response(output2=rows))

        records = await client.get_investor_flow(
            "005930", start_date=date(2020, 1, 1), anchor_date=date(2026, 7, 10)
        )
        assert client._session.get.await_count == 1
        assert len(records) == 10

    @pytest.mark.asyncio
    async def test_empty_response(self):
        client = _make_kis_client()
        client._session.get = AsyncMock(return_value=_make_ok_response(output2=[]))
        records = await client.get_investor_flow("005930")
        assert records == []

    @pytest.mark.asyncio
    async def test_page_cap(self):
        """항상 30행 신규 반환 → _MAX_INVESTOR_FLOW_PAGES(20)에서 중단."""
        client = _make_kis_client()
        call_count = 0

        async def _side_effect(*args, **kwargs):
            nonlocal call_count
            newest = date(2026, 7, 10) - timedelta(days=30 * call_count)
            call_count += 1
            return _make_ok_response(
                output2=_daily_rows(_investor_flow_row, 30, newest)
            )

        client._session.get = AsyncMock(side_effect=_side_effect)
        await client.get_investor_flow(
            "005930", start_date=date(2000, 1, 1), anchor_date=date(2026, 7, 10)
        )
        assert client._session.get.await_count == 20

    @pytest.mark.asyncio
    async def test_rate_limit_retry_inherited(self):
        """EGW00201 1회 후 성공 — F-20 백오프 상속 스모크."""
        client = _make_kis_client()
        err_resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "rate limit"},
            headers={"tr_cont": ""},
        )
        ok_resp = _make_ok_response(
            output2=_daily_rows(_investor_flow_row, 30, date(2026, 7, 10))
        )
        client._session.get = AsyncMock(side_effect=[err_resp, ok_resp])
        with patch("src.broker.kis.client.asyncio.sleep", new_callable=AsyncMock):
            records = await client.get_investor_flow("005930")
        assert len(records) == 30
        assert client._session.get.await_count == 2


class TestKISClientGetMarketInvestorFlow:
    @pytest.mark.asyncio
    async def test_kospi_params(self):
        client = _make_kis_client()
        client._session.get = AsyncMock(
            return_value=_make_ok_response(output=[_market_flow_row()])
        )
        records = await client.get_market_investor_flow(
            "kospi", anchor_date=date(2026, 7, 10)
        )
        params = client._session.get.call_args.kwargs["params"]
        assert params["FID_COND_MRKT_DIV_CODE"] == "U"
        assert params["FID_INPUT_ISCD"] == "0001"
        assert params["FID_INPUT_ISCD_1"] == "KSP"
        assert params["FID_INPUT_ISCD_2"] == "0001"
        assert params["FID_INPUT_DATE_1"] == "20260710"
        assert params["FID_INPUT_DATE_2"] == "20260710"
        assert len(records) == 1
        assert records[0].market == "kospi"

    @pytest.mark.asyncio
    async def test_kosdaq_params(self):
        client = _make_kis_client()
        client._session.get = AsyncMock(
            return_value=_make_ok_response(output=[_market_flow_row()])
        )
        await client.get_market_investor_flow("kosdaq")
        params = client._session.get.call_args.kwargs["params"]
        assert params["FID_INPUT_ISCD"] == "1001"
        assert params["FID_INPUT_ISCD_1"] == "KSQ"

    @pytest.mark.asyncio
    async def test_unknown_market_raises(self):
        client = _make_kis_client()
        client._session.get = AsyncMock()
        with pytest.raises(ValueError, match="unknown market"):
            await client.get_market_investor_flow("nasdaq")
        client._session.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sorted_and_filtered(self):
        client = _make_kis_client()
        rows = _daily_rows(_market_flow_row, 5, date(2026, 7, 10))
        client._session.get = AsyncMock(return_value=_make_ok_response(output=rows))

        records = await client.get_market_investor_flow(
            "kospi", start_date=date(2026, 7, 8)
        )
        assert [r.date for r in records] == [
            date(2026, 7, 8), date(2026, 7, 9), date(2026, 7, 10)
        ]

    @pytest.mark.asyncio
    async def test_single_dict_output_promoted(self):
        """output이 list가 아니라 dict 단건이어도 방어 승격."""
        client = _make_kis_client()
        client._session.get = AsyncMock(
            return_value=_make_ok_response(output=_market_flow_row())
        )
        records = await client.get_market_investor_flow("kospi")
        assert len(records) == 1


class TestKISClientGetDailyShortSale:
    @pytest.mark.asyncio
    async def test_params_and_parse(self):
        client = _make_kis_client()
        rows = _daily_rows(_short_sale_row, 3, date(2026, 7, 10))
        client._session.get = AsyncMock(return_value=_make_ok_response(output2=rows))

        records = await client.get_daily_short_sale(
            "005930", start_date=date(2026, 7, 8), end_date=date(2026, 7, 10)
        )
        params = client._session.get.call_args_list[0].kwargs["params"]
        assert params["FID_COND_MRKT_DIV_CODE"] == "J"
        assert params["FID_INPUT_ISCD"] == "005930"
        assert params["FID_INPUT_DATE_1"] == "20260708"
        assert params["FID_INPUT_DATE_2"] == "20260710"
        assert len(records) == 3
        assert records[0].date < records[-1].date

    @pytest.mark.asyncio
    async def test_full_range_single_call(self):
        """반환 행이 start_date까지 도달하면 1콜로 종료 (캡 없음 정상 케이스)."""
        client = _make_kis_client()
        rows = _daily_rows(_short_sale_row, 10, date(2026, 7, 10))  # ~20260701
        client._session.get = AsyncMock(return_value=_make_ok_response(output2=rows))

        records = await client.get_daily_short_sale(
            "005930", start_date=date(2026, 7, 1), end_date=date(2026, 7, 10)
        )
        assert client._session.get.await_count == 1
        assert len(records) == 10

    @pytest.mark.asyncio
    async def test_repeat_response_terminates(self):
        """창 이동 후 동일 응답(신규 행 0)이어도 무한루프 없이 종료."""
        client = _make_kis_client()
        rows = _daily_rows(_short_sale_row, 5, date(2026, 7, 10))  # ~20260706
        client._session.get = AsyncMock(side_effect=[
            _make_ok_response(output2=rows),
            _make_ok_response(output2=rows),
        ])

        records = await client.get_daily_short_sale(
            "005930", start_date=date(2026, 6, 1), end_date=date(2026, 7, 10)
        )
        assert client._session.get.await_count == 2
        assert len(records) == 5


class TestKISClientGetDailyLoanTrans:
    @pytest.mark.asyncio
    async def test_stock_mode_params(self):
        """MRKT_DIV_CLS_CODE는 반드시 "3"(종목) — "1"은 코스피 시장 단위(프로브 오호출)."""
        client = _make_kis_client()
        rows = _daily_rows(_loan_trans_row, 3, date(2026, 7, 10))
        client._session.get = AsyncMock(return_value=_make_ok_response(output1=rows))

        records = await client.get_daily_loan_trans(
            "005930", start_date=date(2026, 7, 8), end_date=date(2026, 7, 10)
        )
        params = client._session.get.call_args.kwargs["params"]
        assert params["MRKT_DIV_CLS_CODE"] == "3"
        assert params["MKSC_SHRN_ISCD"] == "005930"
        assert params["START_DATE"] == "20260708"
        assert params["END_DATE"] == "20260710"
        assert params["CTS"] == ""
        assert len(records) == 3

    @pytest.mark.asyncio
    async def test_100_row_cap_pagination(self):
        """100행 = 캡 도달 → END_DATE를 oldest-1일로 당겨 다음 창 조회."""
        client = _make_kis_client()
        page1 = _daily_rows(_loan_trans_row, 100, date(2026, 7, 10))  # ~20260402
        page2 = _daily_rows(_loan_trans_row, 50, date(2026, 4, 1))    # ~20260211
        client._session.get = AsyncMock(side_effect=[
            _make_ok_response(output1=page1),
            _make_ok_response(output1=page2),
        ])

        records = await client.get_daily_loan_trans(
            "005930", start_date=date(2026, 2, 15), end_date=date(2026, 7, 10)
        )
        assert client._session.get.await_count == 2
        params2 = client._session.get.call_args_list[1].kwargs["params"]
        assert params2["END_DATE"] == "20260401"  # page1 oldest(20260402) - 1일
        assert params2["START_DATE"] == "20260215"
        assert all(r.date >= date(2026, 2, 15) for r in records)
        assert records[0].date < records[-1].date

    @pytest.mark.asyncio
    async def test_under_100_rows_single_call(self):
        client = _make_kis_client()
        rows = _daily_rows(_loan_trans_row, 40, date(2026, 7, 10))
        client._session.get = AsyncMock(return_value=_make_ok_response(output1=rows))

        records = await client.get_daily_loan_trans(
            "005930", start_date=date(2026, 5, 1), end_date=date(2026, 7, 10)
        )
        assert client._session.get.await_count == 1
        assert len(records) == 40

    @pytest.mark.asyncio
    async def test_empty_response(self):
        client = _make_kis_client()
        client._session.get = AsyncMock(return_value=_make_ok_response(output1=[]))
        records = await client.get_daily_loan_trans(
            "005930", start_date=date(2026, 7, 1), end_date=date(2026, 7, 10)
        )
        assert records == []


class TestKISClientGetStockMaster:
    @pytest.mark.asyncio
    async def test_combines_kospi_kosdaq(self):
        client = _make_kis_client()
        kospi = [StockInfo(symbol="005930", name="삼성전자", market_type=MarketType.KOSPI)]
        kosdaq = [StockInfo(symbol="035720", name="카카오", market_type=MarketType.KOSDAQ)]

        with (
            patch.object(client, "get_industry_code_map", new_callable=AsyncMock, return_value={}),
            patch.object(client, "_parse_mst", new_callable=AsyncMock, side_effect=[kospi, kosdaq]),
        ):
            result = await client.get_stock_master()

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_empty_on_failure(self):
        client = _make_kis_client()
        with (
            patch.object(client, "get_industry_code_map", new_callable=AsyncMock, return_value={}),
            patch.object(client, "_parse_mst", new_callable=AsyncMock, return_value=[]),
        ):
            result = await client.get_stock_master()
        assert result == []

    @pytest.mark.asyncio
    async def test_correct_params(self):
        client = _make_kis_client()
        with (
            patch.object(client, "get_industry_code_map", new_callable=AsyncMock, return_value={}),
            patch.object(client, "_parse_mst", new_callable=AsyncMock, return_value=[]) as mock,
        ):
            await client.get_stock_master()
            calls = mock.call_args_list
            # KOSPI part2_len=228, KOSDAQ part2_len=222
            assert calls[0].args[2] == 228
            assert calls[1].args[2] == 222


class TestKISClientParseMst:
    @pytest.mark.asyncio
    async def test_valid_zip(self):
        client = _make_kis_client()
        # part1: short_code(9) + standard_code(12) + korean_name
        # part2: part2_len characters
        part2_len = 228
        short_code = "005930   "  # 9 chars
        standard_code = "KR7005930003"  # 12 chars
        korean_name = "삼성전자"
        part1 = short_code + standard_code + korean_name
        part2 = "X" * part2_len
        line = part1 + part2

        zip_bytes = _build_mst_zip([line])
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=zip_bytes)
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        result = await client._parse_mst("http://test.zip", MarketType.KOSPI, part2_len)
        assert len(result) == 1
        assert result[0].symbol == "005930"
        assert result[0].name == "삼성전자"

    @pytest.mark.asyncio
    async def test_not_connected(self):
        client = _make_kis_client()
        client._session = None
        with pytest.raises(BrokerError, match="not connected"):
            await client._parse_mst("http://test.zip", MarketType.KOSPI, 228)

    @pytest.mark.asyncio
    async def test_non_200_returns_empty(self):
        client = _make_kis_client()
        resp = MagicMock()
        resp.status = 404
        resp.read = AsyncMock(return_value=b"")
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        result = await client._parse_mst("http://test.zip", MarketType.KOSPI, 228)
        assert result == []

    @pytest.mark.asyncio
    async def test_network_error_returns_empty(self):
        client = _make_kis_client()
        client._session.get = MagicMock(side_effect=aiohttp.ClientError("fail"))
        result = await client._parse_mst("http://test.zip", MarketType.KOSPI, 228)
        assert result == []

    @pytest.mark.asyncio
    async def test_filters_non_6digit(self):
        client = _make_kis_client()
        part2_len = 228
        # 9-digit code should be filtered
        line1 = "123456789" + "KR7005930003" + "테스트" + "X" * part2_len
        # Valid 6-digit
        line2 = "005930   " + "KR7005930003" + "삼성전자" + "X" * part2_len

        zip_bytes = _build_mst_zip([line1, line2])
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=zip_bytes)
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        result = await client._parse_mst("http://test.zip", MarketType.KOSPI, part2_len)
        assert len(result) == 1
        assert result[0].symbol == "005930"

    @pytest.mark.asyncio
    async def test_keeps_alphanumeric_new_style_code(self):
        """F-22: KRX 신형 영숫자 단축코드(예: 0001A0)를 유지한다."""
        client = _make_kis_client()
        part2_len = 228
        line1 = "0001A0   " + "KR70001A0009" + "신형코드종목" + "X" * part2_len
        line2 = "005930   " + "KR7005930003" + "삼성전자" + "X" * part2_len

        zip_bytes = _build_mst_zip([line1, line2])
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=zip_bytes)
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        result = await client._parse_mst("http://test.zip", MarketType.KOSPI, part2_len)
        assert [s.symbol for s in result] == ["0001A0", "005930"]
        assert result[0].name == "신형코드종목"

    @pytest.mark.asyncio
    async def test_still_drops_non_6char_and_lowercase(self):
        """F-22: 완화 후에도 7자리(ETN)·5자리·소문자 포함 코드는 드랍된다."""
        client = _make_kis_client()
        part2_len = 228
        etn_7digit = "5800115  " + "KR7580011506" + "ETN상품" + "X" * part2_len
        five_char = "12345    " + "KR7123450001" + "다섯자리" + "X" * part2_len
        lowercase = "0001a0   " + "KR70001A0009" + "소문자코드" + "X" * part2_len
        valid = "0001A0   " + "KR70001A0009" + "신형코드종목" + "X" * part2_len

        zip_bytes = _build_mst_zip([etn_7digit, five_char, lowercase, valid])
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=zip_bytes)
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        result = await client._parse_mst("http://test.zip", MarketType.KOSPI, part2_len)
        assert [s.symbol for s in result] == ["0001A0"]

    @pytest.mark.asyncio
    async def test_short_lines_skipped(self):
        client = _make_kis_client()
        part2_len = 228
        short_line = "X" * 10  # Too short

        zip_bytes = _build_mst_zip([short_line])
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=zip_bytes)
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        result = await client._parse_mst("http://test.zip", MarketType.KOSPI, part2_len)
        assert result == []

    @staticmethod
    def _build_line(
        mid_code: str, part2_len: int = 228, security_group: str = "ZZ"
    ) -> str:
        """part1 + part2, where part2[0:2] = 증권그룹코드, part2[7:11] = 지수업종중분류."""
        part1 = "005930   " + "KR7005930003" + "삼성전자"
        # offset 7 = 그룹코드2 + 시총규모1 + 지수업종대분류4
        part2 = (
            security_group
            + "Z" * (7 - len(security_group))
            + mid_code
            + "X" * (part2_len - 7 - len(mid_code))
        )
        return part1 + part2

    @pytest.mark.asyncio
    async def test_security_group_parsed(self):
        """part2 선두 2바이트 증권그룹코드가 security_group으로 추출된다 (PRJ-03 단계 8)."""
        client = _make_kis_client()
        line = self._build_line("0002", security_group="ST")
        zip_bytes = _build_mst_zip([line])
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=zip_bytes)
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        result = await client._parse_mst("http://test.zip", MarketType.KOSPI, 228)
        assert len(result) == 1
        assert result[0].security_group == "ST"

    @pytest.mark.asyncio
    async def test_security_group_blank_is_none(self):
        """그룹코드 자리가 공백이면 security_group=None (보수적 소비 제외 대상)."""
        client = _make_kis_client()
        line = self._build_line("0002", security_group="  ")
        zip_bytes = _build_mst_zip([line])
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=zip_bytes)
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        result = await client._parse_mst("http://test.zip", MarketType.KOSPI, 228)
        assert result[0].security_group is None

    @pytest.mark.asyncio
    async def test_sector_resolved_to_name(self):
        """지수업종중분류 코드가 sector_map으로 업종명 해석된다."""
        client = _make_kis_client()
        line = self._build_line("0002")
        zip_bytes = _build_mst_zip([line])
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=zip_bytes)
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        result = await client._parse_mst(
            "http://test.zip", MarketType.KOSPI, 228, sector_map={"0002": "반도체"}
        )
        assert len(result) == 1
        assert result[0].sector == "반도체"

    @pytest.mark.asyncio
    async def test_sector_falls_back_to_code_on_miss(self):
        """매핑 미스/맵부재 시 원시 코드를 sector로 저장한다('기타' collapse 방지)."""
        client = _make_kis_client()
        line = self._build_line("0002")
        zip_bytes = _build_mst_zip([line])
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=zip_bytes)
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        # sector_map 없음 → 원시 코드
        result = await client._parse_mst("http://test.zip", MarketType.KOSPI, 228)
        assert result[0].sector == "0002"

    @pytest.mark.asyncio
    async def test_sector_empty_when_code_blank(self):
        """중분류 코드가 공백이면 sector는 빈 문자열."""
        client = _make_kis_client()
        line = self._build_line("    ")  # 4 spaces
        zip_bytes = _build_mst_zip([line])
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=zip_bytes)
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        result = await client._parse_mst(
            "http://test.zip", MarketType.KOSPI, 228, sector_map={"0002": "반도체"}
        )
        assert result[0].sector == ""


class TestKISClientGetIndustryCodeMap:
    @pytest.mark.asyncio
    async def test_parses_code_to_name(self):
        client = _make_kis_client()
        # idx_div(1) + idx_code(4) + idx_name
        lines = ["00002반도체", "10003화학"]
        zip_bytes = _build_mst_zip(lines)
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=zip_bytes)
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        result = await client.get_industry_code_map()
        assert result == {"0002": "반도체", "0003": "화학"}

    @pytest.mark.asyncio
    async def test_not_connected(self):
        client = _make_kis_client()
        client._session = None
        with pytest.raises(BrokerError, match="not connected"):
            await client.get_industry_code_map()

    @pytest.mark.asyncio
    async def test_non_200_returns_empty(self):
        client = _make_kis_client()
        resp = MagicMock()
        resp.status = 500
        resp.read = AsyncMock(return_value=b"")
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))
        assert await client.get_industry_code_map() == {}

    @pytest.mark.asyncio
    async def test_network_error_returns_empty(self):
        client = _make_kis_client()
        client._session.get = MagicMock(side_effect=aiohttp.ClientError("fail"))
        assert await client.get_industry_code_map() == {}


class TestKISClientPlaceOrder:
    @pytest.mark.asyncio
    async def test_buy_market(self):
        client = _make_kis_client()
        resp = _make_ok_response(output={"ODNO": "12345", "KRX_FWDG_ORD_ORGNO": "", "ORD_TMD": "100000"})
        client._session.post = AsyncMock(return_value=resp)

        order = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        result = await client.place_order(order)

        call_kwargs = client._session.post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["ORD_DVSN"] == "01"
        assert body["ORD_UNPR"] == "0"
        assert result.order_id == "12345"

    @pytest.mark.asyncio
    async def test_sell_limit(self):
        client = _make_kis_client()
        resp = _make_ok_response(output={"ODNO": "67890", "KRX_FWDG_ORD_ORGNO": "", "ORD_TMD": "100000"})
        client._session.post = AsyncMock(return_value=resp)

        order = OrderRequest(
            symbol="005930", side=OrderSide.SELL,
            order_type=OrderType.LIMIT, quantity=5, price=Decimal("75000"),
        )
        result = await client.place_order(order)

        call_kwargs = client._session.post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["ORD_DVSN"] == "00"
        assert "SLL_TYPE" in body
        assert result.side == OrderSide.SELL

    def _cancelable_row(self, **overrides):
        row = {
            "odno": "12345",
            "ord_gno_brno": "00950",
            "ord_dvsn_cd": "00",
            "psbl_qty": "10",
            "ord_unpr": "75000",
            "sll_buy_dvsn_cd": "02",
            "pdno": "005930",
        }
        row.update(overrides)
        return row

    @pytest.mark.asyncio
    async def test_cancel_order_success_builds_cancel_body(self):
        client = _make_kis_client()
        client._is_paper = lambda: False  # type: ignore[method-assign]
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"output": [self._cancelable_row()], "_tr_cont": ""},
                {"output": {}, "_tr_cont": ""},
            ]
        )

        assert await client.cancel_order("12345") is True
        assert client._request.await_count == 2

        # 1번째: 정정취소가능주문조회(GET)
        first = client._request.call_args_list[0]
        assert first.args[0] == "GET"
        assert "inquire-psbl-rvsecncl" in first.args[1]
        assert first.args[2] == "TTTC0084R"

        # 2번째: 정정취소(POST) — body 검증
        second = client._request.call_args_list[1]
        assert second.args[0] == "POST"
        assert "order-rvsecncl" in second.args[1]
        assert second.args[2] == "TTTC0013U"  # 실전
        body = second.kwargs["body"]
        assert body["RVSE_CNCL_DVSN_CD"] == "02"
        assert body["QTY_ALL_ORD_YN"] == "Y"
        assert body["EXCG_ID_DVSN_CD"] == "KRX"
        assert body["KRX_FWDG_ORD_ORGNO"] == "00950"
        assert body["ORGN_ODNO"] == "12345"
        assert body["ORD_DVSN"] == "00"
        assert body["ORD_QTY"] == "10"
        assert body["ORD_UNPR"] == "75000"

    @pytest.mark.asyncio
    async def test_cancel_order_paper_uses_demo_tr_id(self):
        client = _make_kis_client()
        client._is_paper = lambda: True  # type: ignore[method-assign]
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"output": [self._cancelable_row()], "_tr_cont": ""},
                {"output": {}, "_tr_cont": ""},
            ]
        )

        assert await client.cancel_order("12345") is True
        assert client._request.call_args_list[1].args[2] == "VTTC0013U"

    @pytest.mark.asyncio
    async def test_cancel_order_not_in_cancelable_list_returns_false(self):
        client = _make_kis_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            return_value={"output": [self._cancelable_row(odno="99999")], "_tr_cont": ""}
        )

        assert await client.cancel_order("12345") is False
        # 취소 TR(POST)은 호출되지 않음 — 조회 1회만
        assert client._request.await_count == 1

    @pytest.mark.asyncio
    async def test_cancel_order_no_psbl_qty_returns_false(self):
        client = _make_kis_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "output": [self._cancelable_row(psbl_qty="0")],
                "_tr_cont": "",
            }
        )

        assert await client.cancel_order("12345") is False
        assert client._request.await_count == 1

    @pytest.mark.asyncio
    async def test_cancel_order_kis_rejection_returns_false(self):
        client = _make_kis_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"output": [self._cancelable_row()], "_tr_cont": ""},
                KISResponseError(msg_cd="APBK1234", msg1="취소 불가", tr_id="TTTC0013U"),
            ]
        )

        # 업무 거부는 삼켜서 False 반환(호출측 reconcile에 위임)
        assert await client.cancel_order("12345") is False

    @pytest.mark.asyncio
    async def test_cancel_order_infra_error_propagates(self):
        client = _make_kis_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"output": [self._cancelable_row()], "_tr_cont": ""},
                RateLimitError("rate limited"),
            ]
        )

        # 인프라 예외는 전파(오취소 방지)
        with pytest.raises(RateLimitError):
            await client.cancel_order("12345")

    @pytest.mark.asyncio
    async def test_fetch_cancelable_orders_paginates(self):
        client = _make_kis_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {
                    "output": [self._cancelable_row(odno="A")],
                    "_tr_cont": "M",
                    "ctx_area_fk100": "FK",
                    "ctx_area_nk100": "NK",
                },
                {"output": [self._cancelable_row(odno="B")], "_tr_cont": ""},
            ]
        )

        rows = await client._fetch_cancelable_orders()
        assert [r.odno for r in rows] == ["A", "B"]
        # 2번째 호출에 연속조회 컨텍스트가 실렸는지
        second = client._request.call_args_list[1]
        assert second.kwargs["params"]["CTX_AREA_FK100"] == "FK"
        assert second.kwargs["params"]["CTX_AREA_NK100"] == "NK"
        assert second.kwargs["tr_cont"] == "N"

    @pytest.mark.asyncio
    async def test_order_result_fields(self):
        client = _make_kis_client()
        resp = _make_ok_response(output={"ODNO": "99999", "KRX_FWDG_ORD_ORGNO": "ORG", "ORD_TMD": "143000"})
        client._session.post = AsyncMock(return_value=resp)

        order = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        result = await client.place_order(order)
        assert result.order_id == "99999"
        assert result.status == OrderStatus.SUBMITTED
        assert result.symbol == "005930"
        assert result.quantity == 10


class TestKISClientGetBalance:
    @pytest.mark.asyncio
    async def test_balance_fields(self):
        client = _make_kis_client()
        # 1st GET = 잔고(TTTC8434R), 2nd GET = 실현손익(TTTC8494R).
        # 당일 매수만 한 날(thdt_buy_amt>0, thdt_sll_amt=0)이라도 daily_pnl은
        # 순매매현금흐름이 아니라 실현손익(rlzt_pfls)을 따라야 한다 — F-18 회귀 가드.
        balance_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [
                    {"pdno": "005930", "hldg_qty": "10", "pchs_avg_pric": "70000",
                     "prpr": "72000", "evlu_amt": "720000", "evlu_pfls_amt": "20000",
                     "pchs_amt": "700000", "evlu_pfls_rt": "2.86", "prdt_name": "삼성전자"},
                ],
                "output2": [
                    {"dnca_tot_amt": "50000000", "tot_evlu_amt": "50720000",
                     "nass_amt": "50720000", "thdt_buy_amt": "700000",
                     "thdt_sll_amt": "0", "pchs_amt_smtl_amt": "700000",
                     "evlu_amt_smtl_amt": "720000", "evlu_pfls_smtl_amt": "20000"},
                ],
            },
            headers={"tr_cont": ""},
        )
        rlzpl_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [],
                "output2": [{"rlzt_pfls": "0", "rlzt_erng_rt": "0"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(side_effect=[balance_resp, rlzpl_resp])

        bal = await client.get_balance()
        assert bal.cash == Decimal("50000000")
        assert bal.invested == Decimal("700000")
        assert bal.unrealized_pnl == Decimal("20000")
        # 매수만 한 날이라도 daily_pnl은 음수가 아니다(실현손익 0).
        assert bal.daily_pnl == Decimal(0)
        assert bal.daily_pnl >= Decimal(0)

    @pytest.mark.asyncio
    async def test_daily_pnl_uses_realized_pnl(self):
        """daily_pnl/daily_pnl_pct/realized_pnl이 실현손익 TR 값을 따른다."""
        client = _make_kis_client()
        balance_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [],
                "output2": [
                    {"dnca_tot_amt": "50000000", "tot_evlu_amt": "50000000",
                     "thdt_buy_amt": "0", "thdt_sll_amt": "0"},
                ],
            },
            headers={"tr_cont": ""},
        )
        rlzpl_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [],
                "output2": [{"rlzt_pfls": "150000", "rlzt_erng_rt": "1.5"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(side_effect=[balance_resp, rlzpl_resp])

        bal = await client.get_balance()
        assert bal.daily_pnl == Decimal("150000")
        assert bal.daily_pnl_pct == Decimal("1.5")
        assert bal.realized_pnl == Decimal("150000")

    @pytest.mark.asyncio
    async def test_positions_count_active_only(self):
        client = _make_kis_client()
        balance_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [
                    {"pdno": "005930", "hldg_qty": "10", "prpr": "72000"},
                    {"pdno": "000660", "hldg_qty": "0", "prpr": "185000"},
                ],
                "output2": [{"dnca_tot_amt": "50000000", "tot_evlu_amt": "50720000"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=balance_resp)

        bal = await client.get_balance()
        assert bal.positions_count == 1

    @pytest.mark.asyncio
    async def test_empty_balance(self):
        client = _make_kis_client()
        balance_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [], "output2": [],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=balance_resp)

        bal = await client.get_balance()
        assert bal.cash == Decimal(0)
        assert bal.positions_count == 0


class TestKISClientGetBuyableCash:
    """get_buyable_cash (TTTC8908R): 미수없는매수금액 조회."""

    @pytest.mark.asyncio
    async def test_returns_nrcvb_buy_amt(self):
        """정상 응답에서 nrcvb_buy_amt(미수없는매수금액)을 파싱한다."""
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output": {
                    "ord_psbl_cash": "25000000",        # 미수 포함 한도
                    "nrcvb_buy_amt": "10000000",        # 미수 없는 금액 (target)
                    "max_buy_amt": "25000000",
                    "max_buy_qty": "347",
                    "nrcvb_buy_qty": "138",
                },
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        buyable = await client.get_buyable_cash("005930", Decimal("72000"))
        assert buyable == Decimal("10000000")

    @pytest.mark.asyncio
    async def test_empty_symbol_returns_zero(self):
        """symbol이 비어있으면 네트워크 호출 없이 0 반환."""
        client = _make_kis_client()
        client._session.get = AsyncMock()

        buyable = await client.get_buyable_cash("", Decimal("72000"))
        assert buyable == Decimal(0)
        client._session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_price_returns_zero(self):
        """price가 0 이하이면 네트워크 호출 없이 0 반환."""
        client = _make_kis_client()
        client._session.get = AsyncMock()

        buyable = await client.get_buyable_cash("005930", Decimal(0))
        assert buyable == Decimal(0)
        client._session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_output_returns_zero(self):
        """output 필드 부재 시 0 반환 (방어적 처리)."""
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "0", "msg_cd": "0000", "msg1": "ok"},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        buyable = await client.get_buyable_cash("005930", Decimal("72000"))
        assert buyable == Decimal(0)


class TestKISClientFetchDailyRealizedPnl:
    """_fetch_daily_realized_pnl (TTTC8494R): 당일 실현손익/실현수익률 조회."""

    @pytest.mark.asyncio
    async def test_parses_realized_pnl(self):
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [],
                "output2": [{"rlzt_pfls": "-25000", "rlzt_erng_rt": "-0.8"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        pnl, rate = await client._fetch_daily_realized_pnl()
        assert pnl == Decimal("-25000")
        assert rate == Decimal("-0.8")

    @pytest.mark.asyncio
    async def test_empty_output2_returns_zero(self):
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                       "output1": [], "output2": []},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        pnl, rate = await client._fetch_daily_realized_pnl()
        assert pnl == Decimal(0)
        assert rate == Decimal(0)

    @pytest.mark.asyncio
    async def test_kis_error_falls_back_to_zero(self):
        """모의 미지원 등 KISResponseError 발생 시 (0, 0) 폴백."""
        client = _make_kis_client()
        client._session.get = AsyncMock(
            side_effect=KISResponseError(msg_cd="EGW00999", msg1="미지원", tr_id="VTTC8494R")
        )

        pnl, rate = await client._fetch_daily_realized_pnl()
        assert pnl == Decimal(0)
        assert rate == Decimal(0)

    @pytest.mark.asyncio
    async def test_real_uses_live_tr_id(self):
        client = _make_kis_client()
        client._is_paper = lambda: False
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                       "output1": [], "output2": [{"rlzt_pfls": "0", "rlzt_erng_rt": "0"}]},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        await client._fetch_daily_realized_pnl()
        # _do_request → build_headers(token, tr_id, tr_cont=...): tr_id는 두 번째 위치 인자.
        assert client._auth.build_headers.call_args.args[1] == "TTTC8494R"

    @pytest.mark.asyncio
    async def test_paper_uses_demo_tr_id(self):
        client = _make_kis_client()
        client._is_paper = lambda: True
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                       "output1": [], "output2": [{"rlzt_pfls": "0", "rlzt_erng_rt": "0"}]},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        await client._fetch_daily_realized_pnl()
        assert client._auth.build_headers.call_args.args[1] == "VTTC8494R"


class TestKISClientGetSellableQuantity:
    """get_sellable_quantity (TTTC8408R): 매도가능수량(ord_psbl_qty) 조회 — F-12."""

    @pytest.mark.asyncio
    async def test_returns_ord_psbl_qty(self):
        """정상 응답에서 ord_psbl_qty(주문가능수량)를 파싱한다."""
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output": {"ord_psbl_qty": "42", "cblc_qty": "100"},
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        sellable = await client.get_sellable_quantity("005930")
        assert sellable == 42

    @pytest.mark.asyncio
    async def test_output_as_list_parsed(self):
        """output이 단일원소 리스트로 와도 방어적으로 파싱한다."""
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output": [{"ord_psbl_qty": "7", "cblc_qty": "7"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        sellable = await client.get_sellable_quantity("005930")
        assert sellable == 7

    @pytest.mark.asyncio
    async def test_empty_symbol_returns_none(self):
        """symbol이 비어있으면 네트워크 호출 없이 None."""
        client = _make_kis_client()
        client._session.get = AsyncMock()

        sellable = await client.get_sellable_quantity("")
        assert sellable is None
        client._session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_output_returns_none(self):
        """output 부재 시 None (preflight 미적용 = 차단 안 함)."""
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "0", "msg_cd": "0000", "msg1": "ok"},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        sellable = await client.get_sellable_quantity("005930")
        assert sellable is None

    @pytest.mark.asyncio
    async def test_kis_error_falls_back_to_none(self):
        """모의 미지원 등 KISResponseError 발생 시 None 폴백(graceful)."""
        client = _make_kis_client()
        client._session.get = AsyncMock(
            side_effect=KISResponseError(msg_cd="EGW00999", msg1="미지원", tr_id="VTTC8408R")
        )

        sellable = await client.get_sellable_quantity("005930")
        assert sellable is None

    @pytest.mark.asyncio
    async def test_real_uses_live_tr_id(self):
        client = _make_kis_client()
        client._is_paper = lambda: False
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                       "output": {"ord_psbl_qty": "1", "cblc_qty": "1"}},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        await client.get_sellable_quantity("005930")
        assert client._auth.build_headers.call_args.args[1] == "TTTC8408R"

    @pytest.mark.asyncio
    async def test_paper_uses_demo_tr_id(self):
        client = _make_kis_client()
        client._is_paper = lambda: True
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                       "output": {"ord_psbl_qty": "1", "cblc_qty": "1"}},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        await client.get_sellable_quantity("005930")
        assert client._auth.build_headers.call_args.args[1] == "VTTC8408R"


class TestKISClientGetPositions:
    @pytest.mark.asyncio
    async def test_filters_zero_qty(self):
        client = _make_kis_client()
        balance_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [
                    {"pdno": "005930", "hldg_qty": "10", "prpr": "72000",
                     "pchs_avg_pric": "70000", "evlu_amt": "720000",
                     "evlu_pfls_amt": "20000", "evlu_pfls_rt": "2.86"},
                    {"pdno": "000660", "hldg_qty": "0", "prpr": "185000"},
                ],
                "output2": [{"dnca_tot_amt": "50000000"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=balance_resp)

        positions = await client.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "005930"

    @pytest.mark.asyncio
    async def test_empty(self):
        client = _make_kis_client()
        balance_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [
                    {"pdno": "005930", "hldg_qty": "0"},
                ],
                "output2": [{"dnca_tot_amt": "50000000"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=balance_resp)

        positions = await client.get_positions()
        assert positions == []


class TestKISClientFetchBalancePages:
    @pytest.mark.asyncio
    async def test_single_page(self):
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [{"pdno": "005930", "hldg_qty": "10"}],
                "output2": [{"dnca_tot_amt": "50000000"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        positions, summary = await client._fetch_balance_pages()
        assert len(positions) == 1
        assert client._session.get.await_count == 1

    @pytest.mark.asyncio
    async def test_multi_page_m_cont(self):
        client = _make_kis_client()
        resp1 = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [{"pdno": "005930", "hldg_qty": "10"}],
                "output2": [{"dnca_tot_amt": "50000000"}],
                "ctx_area_fk100": "FK", "ctx_area_nk100": "NK",
            },
            headers={"tr_cont": "M"},
        )
        resp2 = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [{"pdno": "000660", "hldg_qty": "5"}],
                "output2": [],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(side_effect=[resp1, resp2])

        positions, summary = await client._fetch_balance_pages()
        assert len(positions) == 2
        assert client._session.get.await_count == 2

    @pytest.mark.asyncio
    async def test_context_keys_forwarded(self):
        client = _make_kis_client()
        resp1 = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [], "output2": [{"dnca_tot_amt": "0"}],
                "ctx_area_fk100": "FKVAL", "ctx_area_nk100": "NKVAL",
            },
            headers={"tr_cont": "M"},
        )
        resp2 = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [], "output2": [],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(side_effect=[resp1, resp2])

        await client._fetch_balance_pages()

        # Check second call's params include context keys
        second_call = client._session.get.call_args_list[1]
        params = second_call.kwargs.get("params") or second_call[1].get("params", {})
        assert params.get("CTX_AREA_FK100") == "FKVAL"
        assert params.get("CTX_AREA_NK100") == "NKVAL"

    @pytest.mark.asyncio
    async def test_no_output2_default(self):
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        positions, summary = await client._fetch_balance_pages()
        assert isinstance(summary, KISBalanceOutput2)
        assert summary.dnca_tot_amt == ""

    @pytest.mark.asyncio
    async def test_ledger_pace_between_pages(self):
        """페이지 연사 사이에 원장 전용 간격(_ledger_pace)이 적용된다 (F-20)."""
        client = _make_kis_client(
            settings=make_settings(KIS_LEDGER_RATE_LIMIT_INTERVAL=0.2)
        )
        resp1 = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [{"pdno": "005930", "hldg_qty": "10"}],
                "output2": [{"dnca_tot_amt": "50000000"}],
                "ctx_area_fk100": "FK", "ctx_area_nk100": "NK",
            },
            headers={"tr_cont": "M"},
        )
        resp2 = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [{"pdno": "000660", "hldg_qty": "5"}], "output2": [],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(side_effect=[resp1, resp2])
        with patch(
            "src.broker.kis.client.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            await client._fetch_balance_pages()
        sleeps = [c.args[0] for c in mock_sleep.await_args_list]
        assert 0.2 in sleeps  # 다음 페이지 전 원장 간격 적용

    @pytest.mark.asyncio
    async def test_no_ledger_pace_single_page(self):
        """단일 페이지면 페이지 간 원장 간격을 두지 않는다 (F-20)."""
        client = _make_kis_client(
            settings=make_settings(KIS_LEDGER_RATE_LIMIT_INTERVAL=0.2)
        )
        resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [], "output2": [{"dnca_tot_amt": "0"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)
        with patch(
            "src.broker.kis.client.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            await client._fetch_balance_pages()
        sleeps = [c.args[0] for c in mock_sleep.await_args_list]
        assert 0.2 not in sleeps


class TestKISClientRateInterval:
    @pytest.mark.asyncio
    async def test_ledger_pace_before_realized_pnl(self):
        """실현손익 조회 직전 원장 전용 간격(_ledger_pace)이 적용된다 (F-20)."""
        client = _make_kis_client(
            settings=make_settings(KIS_LEDGER_RATE_LIMIT_INTERVAL=0.3)
        )
        resp = _make_ok_response(
            output2=[{"rlzt_pfls": "1000", "rlzt_erng_rt": "1.5"}]
        )
        client._session.get = AsyncMock(return_value=resp)
        with patch(
            "src.broker.kis.client.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            pnl, rate = await client._fetch_daily_realized_pnl()
        assert pnl == Decimal("1000")
        sleeps = [c.args[0] for c in mock_sleep.await_args_list]
        assert 0.3 in sleeps

    def test_global_rate_interval_takes_max(self):
        """_global_rate_interval은 인스턴스 생성 순서와 무관하게 최댓값을 채택한다 (F-20)."""
        cache = MagicMock(spec=RedisCache)
        KISClient(settings=make_settings(KIS_RATE_LIMIT_INTERVAL=0.05), cache=cache)
        KISClient(settings=make_settings(KIS_RATE_LIMIT_INTERVAL=0.5), cache=cache)
        assert KISClient._global_rate_interval == 0.5
        # 더 빠른 인터벌로 생성해도 낮아지지 않는다(last-writer-wins 아님).
        KISClient(settings=make_settings(KIS_RATE_LIMIT_INTERVAL=0.05), cache=cache)
        assert KISClient._global_rate_interval == 0.5

    def test_from_credentials_ledger_interval_prod(self):
        creds = AccountCredentials(
            account_id="a", app_key="k", app_secret="s",
            account_no="12345678-01", is_paper=False,
        )
        client = KISClient.from_credentials(creds, MagicMock(spec=RedisCache))
        assert client._ledger_rate_interval == 0.2

    def test_from_credentials_ledger_interval_paper(self):
        creds = AccountCredentials(
            account_id="a", app_key="k", app_secret="s",
            account_no="12345678-01", is_paper=True,
        )
        client = KISClient.from_credentials(creds, MagicMock(spec=RedisCache))
        assert client._ledger_rate_interval == 0.5


# ═══════════════════════════════════════════════════════════════════════
# 2.3 InMemoryBroker
# ═══════════════════════════════════════════════════════════════════════


class TestInMemoryBrokerLifecycle:
    @pytest.mark.asyncio
    async def test_connect(self):
        broker = InMemoryBroker()
        await broker.connect()
        assert broker._connected is True

    @pytest.mark.asyncio
    async def test_disconnect(self):
        broker = InMemoryBroker()
        await broker.connect()
        await broker.disconnect()
        assert broker._connected is False

    @pytest.mark.asyncio
    async def test_defaults(self):
        broker = InMemoryBroker()
        assert len(broker._stocks) == 3
        assert len(broker._prices) == 3
        assert broker._cash == Decimal("100_000_000")
        assert len(broker._positions) == 0
        assert len(broker._orders) == 0


class TestInMemoryBrokerGetPrice:
    @pytest.mark.asyncio
    async def test_known_symbol(self):
        broker = InMemoryBroker()
        price = await broker.get_price("005930")
        assert price.current_price == Decimal("72000")

    @pytest.mark.asyncio
    async def test_unknown_raises(self):
        broker = InMemoryBroker()
        with pytest.raises(OrderError, match="No price data"):
            await broker.get_price("999999")


class TestInMemoryBrokerGetDailyOHLCV:
    @pytest.mark.asyncio
    async def test_stored_data(self):
        bars = [
            OHLCV(symbol="005930", date=date(2026, 1, i + 1),
                   open=Decimal("72000"), high=Decimal("73000"),
                   low=Decimal("71000"), close=Decimal("72500"), volume=1000)
            for i in range(10)
        ]
        broker = InMemoryBroker(ohlcv_data={"005930": bars})
        result = await broker.get_daily_ohlcv("005930", period_days=5)
        assert len(result) == 5

    @pytest.mark.asyncio
    async def test_synthetic_known_price(self):
        broker = InMemoryBroker()
        result = await broker.get_daily_ohlcv("005930", period_days=10)
        assert len(result) == 10
        assert result[0].close == Decimal("72000")

    @pytest.mark.asyncio
    async def test_synthetic_unknown_price(self):
        broker = InMemoryBroker()
        result = await broker.get_daily_ohlcv("999999", period_days=5)
        assert len(result) == 5
        assert result[0].close == Decimal("50000")


class TestInMemoryBrokerPlaceOrder:
    @pytest.mark.asyncio
    async def test_buy_market_new(self):
        broker = InMemoryBroker()
        order = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        result = await broker.place_order(order)

        assert result.status == OrderStatus.FILLED
        assert broker._cash == Decimal("100_000_000") - Decimal("72000") * 10
        assert "005930" in broker._positions

    @pytest.mark.asyncio
    async def test_buy_existing_position(self):
        broker = InMemoryBroker()
        order1 = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        await broker.place_order(order1)

        order2 = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        await broker.place_order(order2)

        pos = broker._positions["005930"]
        assert pos.quantity == 20
        assert pos.average_cost == Decimal("72000")  # same price, same avg

    @pytest.mark.asyncio
    async def test_buy_insufficient_funds(self):
        broker = InMemoryBroker(initial_cash=Decimal("100"))
        order = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        with pytest.raises(InsufficientFundsError):
            await broker.place_order(order)

    @pytest.mark.asyncio
    async def test_buy_limit(self):
        broker = InMemoryBroker()
        order = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=10, price=Decimal("71000"),
        )
        result = await broker.place_order(order)
        assert result.price == Decimal("71000")
        pos = broker._positions["005930"]
        assert pos.average_cost == Decimal("71000")

    @pytest.mark.asyncio
    async def test_sell_full_close(self):
        broker = InMemoryBroker()
        buy = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=10, price=Decimal("70000"),
        )
        await broker.place_order(buy)
        initial_cash = broker._cash

        sell = OrderRequest(
            symbol="005930", side=OrderSide.SELL,
            order_type=OrderType.MARKET, quantity=10,
        )
        result = await broker.place_order(sell)

        assert result.status == OrderStatus.FILLED
        pos = broker._positions["005930"]
        assert pos.status == PositionStatus.CLOSED
        assert broker._realized_pnl == (Decimal("72000") - Decimal("70000")) * 10
        assert broker._cash == initial_cash + Decimal("72000") * 10

    @pytest.mark.asyncio
    async def test_sell_partial(self):
        broker = InMemoryBroker()
        buy = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        await broker.place_order(buy)

        sell = OrderRequest(
            symbol="005930", side=OrderSide.SELL,
            order_type=OrderType.MARKET, quantity=5,
        )
        await broker.place_order(sell)

        pos = broker._positions["005930"]
        assert pos.status == PositionStatus.PARTIALLY_CLOSED
        assert pos.quantity == 5

    @pytest.mark.asyncio
    async def test_sell_no_position(self):
        broker = InMemoryBroker()
        order = OrderRequest(
            symbol="005930", side=OrderSide.SELL,
            order_type=OrderType.MARKET, quantity=10,
        )
        with pytest.raises(OrderError, match="No position"):
            await broker.place_order(order)

    @pytest.mark.asyncio
    async def test_sell_excess_qty(self):
        broker = InMemoryBroker()
        buy = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=5,
        )
        await broker.place_order(buy)

        sell = OrderRequest(
            symbol="005930", side=OrderSide.SELL,
            order_type=OrderType.MARKET, quantity=10,
        )
        with pytest.raises(OrderError, match="Insufficient quantity"):
            await broker.place_order(sell)


class TestInMemoryBrokerCancelOrder:
    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self):
        broker = InMemoryBroker()
        assert await broker.cancel_order("NO_SUCH_ID") is False

    @pytest.mark.asyncio
    async def test_cancel_filled(self):
        broker = InMemoryBroker()
        order = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=1,
        )
        result = await broker.place_order(order)
        assert await broker.cancel_order(result.order_id) is False


class TestInMemoryBrokerBalance:
    @pytest.mark.asyncio
    async def test_initial(self):
        broker = InMemoryBroker()
        bal = await broker.get_balance()
        assert bal.total_assets == Decimal("100_000_000")
        assert bal.cash == Decimal("100_000_000")
        assert bal.invested == Decimal(0)

    @pytest.mark.asyncio
    async def test_after_buy(self):
        broker = InMemoryBroker()
        order = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        await broker.place_order(order)

        bal = await broker.get_balance()
        assert bal.cash == Decimal("100_000_000") - Decimal("72000") * 10
        assert bal.invested == Decimal("72000") * 10

    @pytest.mark.asyncio
    async def test_realized_pnl(self):
        broker = InMemoryBroker()
        buy = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=10, price=Decimal("70000"),
        )
        await broker.place_order(buy)
        sell = OrderRequest(
            symbol="005930", side=OrderSide.SELL,
            order_type=OrderType.MARKET, quantity=10,
        )
        await broker.place_order(sell)

        bal = await broker.get_balance()
        assert bal.realized_pnl == (Decimal("72000") - Decimal("70000")) * 10
