"""KIS API response Pydantic models.

KIS returns all values as strings. Each model provides ``to_domain()`` to
convert into the canonical Pydantic domain models in ``src/core/models``.

All models use ``extra="ignore"`` so unused KIS fields are silently dropped.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict

from src.core.enums import PositionStatus
from src.core.models import (
    OHLCV,
    InvestorFlowRecord,
    LoanTransRecord,
    MarketInvestorFlowRecord,
    Position,
    PriceInfo,
    ShortSaleRecord,
    TradingDayRecord,
)

# ── Helpers ───────────────────────────────────────────────────────────


def _to_decimal(value: str) -> Decimal:
    """Safely convert a KIS string value to Decimal. Returns 0 on failure."""
    if not value or not value.strip():
        return Decimal(0)
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        return Decimal(0)


def _to_int(value: str) -> int:
    """Safely convert a KIS string value to int. Returns 0 on failure."""
    if not value or not value.strip():
        return 0
    try:
        return int(value.strip())
    except ValueError:
        return 0


def _to_date(value: str) -> date:
    """Parse a KIS ``YYYYMMDD`` string into ``date``."""
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def _million_krw(value: str) -> Decimal:
    """수급 TR ``*_pbmn``(백만원 실측 — 게이트 ③) → 원(KRW) 변환의 단일 지점.

    원천 정밀도가 백만원 절사임에 유의(DB 스키마 주석과 동일 전제).
    """
    return _to_decimal(value) * 1_000_000


# ── Base Response ─────────────────────────────────────────────────────


class KISBaseResponse(BaseModel):
    """Common KIS API response wrapper (rt_cd / msg_cd / msg1)."""

    model_config = ConfigDict(extra="ignore")

    rt_cd: str = ""
    msg_cd: str = ""
    msg1: str = ""

    @property
    def is_ok(self) -> bool:
        return self.rt_cd == "0"


# ── 현재가 (FHKST01010100 output) ────────────────────────────────────


class KISPriceOutput(BaseModel):
    """Current price response — ``FHKST01010100`` ``output`` object."""

    model_config = ConfigDict(extra="ignore")

    stck_prpr: str = ""      # 주식 현재가
    prdy_vrss: str = ""      # 전일 대비
    prdy_vrss_sign: str = "" # 전일 대비 부호 (1~5)
    prdy_ctrt: str = ""      # 전일 대비율 (%)
    acml_vol: str = ""       # 누적 거래량
    stck_oprc: str = ""      # 시가
    stck_hgpr: str = ""      # 고가
    stck_lwpr: str = ""      # 저가
    stck_sdpr: str = ""      # 전일 종가 (기준가)

    def to_domain(self, symbol: str) -> PriceInfo:
        """Convert to ``PriceInfo`` domain model.

        ``prdy_vrss_sign`` handling:
        - "1" 상한, "2" 상승, "3" 보합 → change_price positive/zero
        - "4" 하한, "5" 하락 → change_price negated
        """
        change_price = _to_decimal(self.prdy_vrss)
        change_pct = _to_decimal(self.prdy_ctrt)
        if self.prdy_vrss_sign in ("4", "5"):
            change_price = -abs(change_price)
            change_pct = -abs(change_pct)
        return PriceInfo(
            symbol=symbol,
            current_price=_to_decimal(self.stck_prpr),
            previous_close=_to_decimal(self.stck_sdpr),
            change_price=change_price,
            change_percent=change_pct,
            high=_to_decimal(self.stck_hgpr),
            low=_to_decimal(self.stck_lwpr),
            volume=_to_int(self.acml_vol),
            timestamp=datetime.now(),
        )


# ── 일봉 (FHKST03010100 output2 배열) ───────────────────────────────


class KISDailyChartOutput(BaseModel):
    """Daily OHLCV item — ``FHKST03010100`` ``output2`` array element."""

    model_config = ConfigDict(extra="ignore")

    stck_bsop_date: str = ""  # 영업일 (YYYYMMDD)
    stck_clpr: str = ""       # 종가
    stck_oprc: str = ""       # 시가
    stck_hgpr: str = ""       # 고가
    stck_lwpr: str = ""       # 저가
    acml_vol: str = ""        # 누적 거래량
    acml_tr_pbmn: str = ""    # 누적 거래대금

    def to_domain(self, symbol: str) -> OHLCV:
        """Convert to ``OHLCV`` domain model."""
        return OHLCV(
            symbol=symbol,
            date=_to_date(self.stck_bsop_date),
            open=_to_decimal(self.stck_oprc),
            high=_to_decimal(self.stck_hgpr),
            low=_to_decimal(self.stck_lwpr),
            close=_to_decimal(self.stck_clpr),
            volume=_to_int(self.acml_vol),
            value=_to_decimal(self.acml_tr_pbmn),
        )


# ── 종목별 투자자매매동향 (FHPTJ04160001 output2 배열) — PRJ-03 ─────


class KISInvestorFlowOutput(BaseModel):
    """종목별 일별 투자자 수급 — ``FHPTJ04160001`` ``output2`` 배열 원소.

    실측 101필드(probe1_kis_api.md) 중 수급 90필드만 선언 — 시세 필드
    (종가·OHLC·거래량·누적대금)는 DailyOHLCV 완전 중복이라 ``extra="ignore"``로
    배제(PRJ-03 확정 11).

    KIS 원 필드명 quirk (표준 패턴에서 벗어나는 축 — probe1 실측 전수):
    - net qty: 표준 ``{axis}_ntby_qty``, 예외 ``pe_fund/etc_corp/etc_orgt``는 ``_ntby_vol``
    - net amt: 표준 ``{axis}_ntby_tr_pbmn``, 예외 ``frgn_reg/frgn_nreg``는 ``_ntby_pbmn``
    - sell/buy qty: 표준 ``{axis}_seln_vol``/``{axis}_shnu_vol``,
      예외 ``frgn_reg/frgn_nreg``는 ``_askp_qty``/``_bidp_qty``
    - sell/buy amt: 표준 ``{axis}_seln_tr_pbmn``/``{axis}_shnu_tr_pbmn``,
      예외 ``frgn_reg/frgn_nreg``는 ``_askp_pbmn``/``_bidp_pbmn``

    ``*_pbmn``은 전부 백만원 단위(게이트 ③ 실측) — ``to_domain``에서 원으로 변환.
    """

    model_config = ConfigDict(extra="ignore")

    stck_bsop_date: str = ""  # 영업일 (YYYYMMDD)

    # ── 외국인 합계 (frgn) ──
    frgn_ntby_qty: str = ""
    frgn_ntby_tr_pbmn: str = ""
    frgn_seln_vol: str = ""
    frgn_shnu_vol: str = ""
    frgn_seln_tr_pbmn: str = ""
    frgn_shnu_tr_pbmn: str = ""

    # ── 등록 외국인 (frgn_reg) — net amt·sell/buy가 quirk ──
    frgn_reg_ntby_qty: str = ""
    frgn_reg_ntby_pbmn: str = ""   # quirk: _ntby_tr_pbmn 아님
    frgn_reg_askp_qty: str = ""    # quirk: 매도 수량 (seln_vol 아님)
    frgn_reg_bidp_qty: str = ""    # quirk: 매수 수량
    frgn_reg_askp_pbmn: str = ""   # quirk: 매도 대금
    frgn_reg_bidp_pbmn: str = ""   # quirk: 매수 대금

    # ── 비등록 외국인 (frgn_nreg) — frgn_reg와 동일 quirk ──
    frgn_nreg_ntby_qty: str = ""
    frgn_nreg_ntby_pbmn: str = ""
    frgn_nreg_askp_qty: str = ""
    frgn_nreg_bidp_qty: str = ""
    frgn_nreg_askp_pbmn: str = ""
    frgn_nreg_bidp_pbmn: str = ""

    # ── 개인 (prsn) ──
    prsn_ntby_qty: str = ""
    prsn_ntby_tr_pbmn: str = ""
    prsn_seln_vol: str = ""
    prsn_shnu_vol: str = ""
    prsn_seln_tr_pbmn: str = ""
    prsn_shnu_tr_pbmn: str = ""

    # ── 기관합계 (orgn) ──
    orgn_ntby_qty: str = ""
    orgn_ntby_tr_pbmn: str = ""
    orgn_seln_vol: str = ""
    orgn_shnu_vol: str = ""
    orgn_seln_tr_pbmn: str = ""
    orgn_shnu_tr_pbmn: str = ""

    # ── 금융투자 (scrt) ──
    scrt_ntby_qty: str = ""
    scrt_ntby_tr_pbmn: str = ""
    scrt_seln_vol: str = ""
    scrt_shnu_vol: str = ""
    scrt_seln_tr_pbmn: str = ""
    scrt_shnu_tr_pbmn: str = ""

    # ── 투신 (ivtr) ──
    ivtr_ntby_qty: str = ""
    ivtr_ntby_tr_pbmn: str = ""
    ivtr_seln_vol: str = ""
    ivtr_shnu_vol: str = ""
    ivtr_seln_tr_pbmn: str = ""
    ivtr_shnu_tr_pbmn: str = ""

    # ── 사모 (pe_fund) — net qty가 quirk ──
    pe_fund_ntby_vol: str = ""     # quirk: _ntby_qty 아님
    pe_fund_ntby_tr_pbmn: str = ""
    pe_fund_seln_vol: str = ""
    pe_fund_shnu_vol: str = ""
    pe_fund_seln_tr_pbmn: str = ""
    pe_fund_shnu_tr_pbmn: str = ""

    # ── 은행 (bank) ──
    bank_ntby_qty: str = ""
    bank_ntby_tr_pbmn: str = ""
    bank_seln_vol: str = ""
    bank_shnu_vol: str = ""
    bank_seln_tr_pbmn: str = ""
    bank_shnu_tr_pbmn: str = ""

    # ── 보험 (insu) ──
    insu_ntby_qty: str = ""
    insu_ntby_tr_pbmn: str = ""
    insu_seln_vol: str = ""
    insu_shnu_vol: str = ""
    insu_seln_tr_pbmn: str = ""
    insu_shnu_tr_pbmn: str = ""

    # ── 종금·기타금융 (mrbn) ──
    mrbn_ntby_qty: str = ""
    mrbn_ntby_tr_pbmn: str = ""
    mrbn_seln_vol: str = ""
    mrbn_shnu_vol: str = ""
    mrbn_seln_tr_pbmn: str = ""
    mrbn_shnu_tr_pbmn: str = ""

    # ── 연기금 (fund) ──
    fund_ntby_qty: str = ""
    fund_ntby_tr_pbmn: str = ""
    fund_seln_vol: str = ""
    fund_shnu_vol: str = ""
    fund_seln_tr_pbmn: str = ""
    fund_shnu_tr_pbmn: str = ""

    # ── 기타 합계 (etc) ──
    etc_ntby_qty: str = ""
    etc_ntby_tr_pbmn: str = ""
    etc_seln_vol: str = ""
    etc_shnu_vol: str = ""
    etc_seln_tr_pbmn: str = ""
    etc_shnu_tr_pbmn: str = ""

    # ── 기타법인 (etc_corp) — net qty가 quirk ──
    etc_corp_ntby_vol: str = ""    # quirk: _ntby_qty 아님
    etc_corp_ntby_tr_pbmn: str = ""
    etc_corp_seln_vol: str = ""
    etc_corp_shnu_vol: str = ""
    etc_corp_seln_tr_pbmn: str = ""
    etc_corp_shnu_tr_pbmn: str = ""

    # ── 기타단체 (etc_orgt) — net qty가 quirk ──
    etc_orgt_ntby_vol: str = ""    # quirk: _ntby_qty 아님
    etc_orgt_ntby_tr_pbmn: str = ""
    etc_orgt_seln_vol: str = ""
    etc_orgt_shnu_vol: str = ""
    etc_orgt_seln_tr_pbmn: str = ""
    etc_orgt_shnu_tr_pbmn: str = ""

    def to_domain(self, symbol: str) -> InvestorFlowRecord:
        """KIS 원 필드 → 정규화 컬럼(``{axis}_{net|sell|buy}_{qty|amt}``) 매핑의 단일 지점.

        net 값은 원천이 부호를 가지므로 부호 재처리 없음. 대금은 백만원 → 원.
        """
        return InvestorFlowRecord(
            symbol=symbol,
            date=_to_date(self.stck_bsop_date),
            frgn_net_qty=_to_int(self.frgn_ntby_qty),
            frgn_net_amt=_million_krw(self.frgn_ntby_tr_pbmn),
            frgn_sell_qty=_to_int(self.frgn_seln_vol),
            frgn_buy_qty=_to_int(self.frgn_shnu_vol),
            frgn_sell_amt=_million_krw(self.frgn_seln_tr_pbmn),
            frgn_buy_amt=_million_krw(self.frgn_shnu_tr_pbmn),
            frgn_reg_net_qty=_to_int(self.frgn_reg_ntby_qty),
            frgn_reg_net_amt=_million_krw(self.frgn_reg_ntby_pbmn),
            frgn_reg_sell_qty=_to_int(self.frgn_reg_askp_qty),
            frgn_reg_buy_qty=_to_int(self.frgn_reg_bidp_qty),
            frgn_reg_sell_amt=_million_krw(self.frgn_reg_askp_pbmn),
            frgn_reg_buy_amt=_million_krw(self.frgn_reg_bidp_pbmn),
            frgn_nreg_net_qty=_to_int(self.frgn_nreg_ntby_qty),
            frgn_nreg_net_amt=_million_krw(self.frgn_nreg_ntby_pbmn),
            frgn_nreg_sell_qty=_to_int(self.frgn_nreg_askp_qty),
            frgn_nreg_buy_qty=_to_int(self.frgn_nreg_bidp_qty),
            frgn_nreg_sell_amt=_million_krw(self.frgn_nreg_askp_pbmn),
            frgn_nreg_buy_amt=_million_krw(self.frgn_nreg_bidp_pbmn),
            prsn_net_qty=_to_int(self.prsn_ntby_qty),
            prsn_net_amt=_million_krw(self.prsn_ntby_tr_pbmn),
            prsn_sell_qty=_to_int(self.prsn_seln_vol),
            prsn_buy_qty=_to_int(self.prsn_shnu_vol),
            prsn_sell_amt=_million_krw(self.prsn_seln_tr_pbmn),
            prsn_buy_amt=_million_krw(self.prsn_shnu_tr_pbmn),
            orgn_net_qty=_to_int(self.orgn_ntby_qty),
            orgn_net_amt=_million_krw(self.orgn_ntby_tr_pbmn),
            orgn_sell_qty=_to_int(self.orgn_seln_vol),
            orgn_buy_qty=_to_int(self.orgn_shnu_vol),
            orgn_sell_amt=_million_krw(self.orgn_seln_tr_pbmn),
            orgn_buy_amt=_million_krw(self.orgn_shnu_tr_pbmn),
            scrt_net_qty=_to_int(self.scrt_ntby_qty),
            scrt_net_amt=_million_krw(self.scrt_ntby_tr_pbmn),
            scrt_sell_qty=_to_int(self.scrt_seln_vol),
            scrt_buy_qty=_to_int(self.scrt_shnu_vol),
            scrt_sell_amt=_million_krw(self.scrt_seln_tr_pbmn),
            scrt_buy_amt=_million_krw(self.scrt_shnu_tr_pbmn),
            ivtr_net_qty=_to_int(self.ivtr_ntby_qty),
            ivtr_net_amt=_million_krw(self.ivtr_ntby_tr_pbmn),
            ivtr_sell_qty=_to_int(self.ivtr_seln_vol),
            ivtr_buy_qty=_to_int(self.ivtr_shnu_vol),
            ivtr_sell_amt=_million_krw(self.ivtr_seln_tr_pbmn),
            ivtr_buy_amt=_million_krw(self.ivtr_shnu_tr_pbmn),
            pe_fund_net_qty=_to_int(self.pe_fund_ntby_vol),
            pe_fund_net_amt=_million_krw(self.pe_fund_ntby_tr_pbmn),
            pe_fund_sell_qty=_to_int(self.pe_fund_seln_vol),
            pe_fund_buy_qty=_to_int(self.pe_fund_shnu_vol),
            pe_fund_sell_amt=_million_krw(self.pe_fund_seln_tr_pbmn),
            pe_fund_buy_amt=_million_krw(self.pe_fund_shnu_tr_pbmn),
            bank_net_qty=_to_int(self.bank_ntby_qty),
            bank_net_amt=_million_krw(self.bank_ntby_tr_pbmn),
            bank_sell_qty=_to_int(self.bank_seln_vol),
            bank_buy_qty=_to_int(self.bank_shnu_vol),
            bank_sell_amt=_million_krw(self.bank_seln_tr_pbmn),
            bank_buy_amt=_million_krw(self.bank_shnu_tr_pbmn),
            insu_net_qty=_to_int(self.insu_ntby_qty),
            insu_net_amt=_million_krw(self.insu_ntby_tr_pbmn),
            insu_sell_qty=_to_int(self.insu_seln_vol),
            insu_buy_qty=_to_int(self.insu_shnu_vol),
            insu_sell_amt=_million_krw(self.insu_seln_tr_pbmn),
            insu_buy_amt=_million_krw(self.insu_shnu_tr_pbmn),
            mrbn_net_qty=_to_int(self.mrbn_ntby_qty),
            mrbn_net_amt=_million_krw(self.mrbn_ntby_tr_pbmn),
            mrbn_sell_qty=_to_int(self.mrbn_seln_vol),
            mrbn_buy_qty=_to_int(self.mrbn_shnu_vol),
            mrbn_sell_amt=_million_krw(self.mrbn_seln_tr_pbmn),
            mrbn_buy_amt=_million_krw(self.mrbn_shnu_tr_pbmn),
            fund_net_qty=_to_int(self.fund_ntby_qty),
            fund_net_amt=_million_krw(self.fund_ntby_tr_pbmn),
            fund_sell_qty=_to_int(self.fund_seln_vol),
            fund_buy_qty=_to_int(self.fund_shnu_vol),
            fund_sell_amt=_million_krw(self.fund_seln_tr_pbmn),
            fund_buy_amt=_million_krw(self.fund_shnu_tr_pbmn),
            etc_net_qty=_to_int(self.etc_ntby_qty),
            etc_net_amt=_million_krw(self.etc_ntby_tr_pbmn),
            etc_sell_qty=_to_int(self.etc_seln_vol),
            etc_buy_qty=_to_int(self.etc_shnu_vol),
            etc_sell_amt=_million_krw(self.etc_seln_tr_pbmn),
            etc_buy_amt=_million_krw(self.etc_shnu_tr_pbmn),
            etc_corp_net_qty=_to_int(self.etc_corp_ntby_vol),
            etc_corp_net_amt=_million_krw(self.etc_corp_ntby_tr_pbmn),
            etc_corp_sell_qty=_to_int(self.etc_corp_seln_vol),
            etc_corp_buy_qty=_to_int(self.etc_corp_shnu_vol),
            etc_corp_sell_amt=_million_krw(self.etc_corp_seln_tr_pbmn),
            etc_corp_buy_amt=_million_krw(self.etc_corp_shnu_tr_pbmn),
            etc_orgt_net_qty=_to_int(self.etc_orgt_ntby_vol),
            etc_orgt_net_amt=_million_krw(self.etc_orgt_ntby_tr_pbmn),
            etc_orgt_sell_qty=_to_int(self.etc_orgt_seln_vol),
            etc_orgt_buy_qty=_to_int(self.etc_orgt_shnu_vol),
            etc_orgt_sell_amt=_million_krw(self.etc_orgt_seln_tr_pbmn),
            etc_orgt_buy_amt=_million_krw(self.etc_orgt_shnu_tr_pbmn),
        )


# ── 시장 단위 투자자매매동향 (FHPTJ04040000 output 배열) — PRJ-03 ───


class KISMarketInvestorFlowOutput(BaseModel):
    """시장 단위 일별 투자자 수급 + 지수 OHLC — ``FHPTJ04040000`` ``output`` 원소.

    실측 39필드(probe1_kis_api.md). net qty/amt quirk는 종목 TR과 동일
    (``pe_fund/etc_corp/etc_orgt`` → ``_ntby_vol``,
    ``frgn_reg/frgn_nreg`` → ``_ntby_pbmn``).
    """

    model_config = ConfigDict(extra="ignore")

    stck_bsop_date: str = ""       # 영업일 (YYYYMMDD)

    # ── 지수 OHLC ──
    bstp_nmix_prpr: str = ""       # 지수 현재가(= 종가)
    bstp_nmix_oprc: str = ""       # 지수 시가
    bstp_nmix_hgpr: str = ""       # 지수 고가
    bstp_nmix_lwpr: str = ""       # 지수 저가
    stck_prdy_clpr: str = ""       # 전일 종가
    bstp_nmix_prdy_vrss: str = ""  # 전일 대비
    prdy_vrss_sign: str = ""       # 전일 대비 부호 (1~5)
    bstp_nmix_prdy_ctrt: str = ""  # 전일 대비율 (%)

    # ── 수급 15축 × net qty/amt ──
    frgn_ntby_qty: str = ""
    frgn_ntby_tr_pbmn: str = ""
    frgn_reg_ntby_qty: str = ""
    frgn_reg_ntby_pbmn: str = ""   # quirk
    frgn_nreg_ntby_qty: str = ""
    frgn_nreg_ntby_pbmn: str = ""  # quirk
    prsn_ntby_qty: str = ""
    prsn_ntby_tr_pbmn: str = ""
    orgn_ntby_qty: str = ""
    orgn_ntby_tr_pbmn: str = ""
    scrt_ntby_qty: str = ""
    scrt_ntby_tr_pbmn: str = ""
    ivtr_ntby_qty: str = ""
    ivtr_ntby_tr_pbmn: str = ""
    pe_fund_ntby_vol: str = ""     # quirk
    pe_fund_ntby_tr_pbmn: str = ""
    bank_ntby_qty: str = ""
    bank_ntby_tr_pbmn: str = ""
    insu_ntby_qty: str = ""
    insu_ntby_tr_pbmn: str = ""
    mrbn_ntby_qty: str = ""
    mrbn_ntby_tr_pbmn: str = ""
    fund_ntby_qty: str = ""
    fund_ntby_tr_pbmn: str = ""
    etc_ntby_qty: str = ""
    etc_ntby_tr_pbmn: str = ""
    etc_corp_ntby_vol: str = ""    # quirk
    etc_corp_ntby_tr_pbmn: str = ""
    etc_orgt_ntby_vol: str = ""    # quirk
    etc_orgt_ntby_tr_pbmn: str = ""

    def to_domain(self, market: str) -> MarketInvestorFlowRecord:
        """지수 전일 대비는 ``prdy_vrss_sign`` 4/5 → 음수화 (KISPriceOutput 패턴).

        ``*_ntby_qty``는 천주 단위 실측 확정(2026-07-15 EC2 — 시장 총량이
        단일 종목 순매수보다 작아지는 모순으로 판정) — ×1,000으로 주 단위 변환.
        원천이 천주 반올림이므로 변환 후에도 ±500주 정밀도 한계가 내재한다.
        """
        index_change = _to_decimal(self.bstp_nmix_prdy_vrss)
        index_change_rate = _to_decimal(self.bstp_nmix_prdy_ctrt)
        if self.prdy_vrss_sign in ("4", "5"):
            index_change = -abs(index_change)
            index_change_rate = -abs(index_change_rate)
        return MarketInvestorFlowRecord(
            market=market,
            date=_to_date(self.stck_bsop_date),
            index_open=_to_decimal(self.bstp_nmix_oprc),
            index_high=_to_decimal(self.bstp_nmix_hgpr),
            index_low=_to_decimal(self.bstp_nmix_lwpr),
            index_close=_to_decimal(self.bstp_nmix_prpr),
            index_prev_close=_to_decimal(self.stck_prdy_clpr),
            index_change=index_change,
            index_change_rate=index_change_rate,
            frgn_net_qty=_to_int(self.frgn_ntby_qty) * 1000,
            frgn_net_amt=_million_krw(self.frgn_ntby_tr_pbmn),
            frgn_reg_net_qty=_to_int(self.frgn_reg_ntby_qty) * 1000,
            frgn_reg_net_amt=_million_krw(self.frgn_reg_ntby_pbmn),
            frgn_nreg_net_qty=_to_int(self.frgn_nreg_ntby_qty) * 1000,
            frgn_nreg_net_amt=_million_krw(self.frgn_nreg_ntby_pbmn),
            prsn_net_qty=_to_int(self.prsn_ntby_qty) * 1000,
            prsn_net_amt=_million_krw(self.prsn_ntby_tr_pbmn),
            orgn_net_qty=_to_int(self.orgn_ntby_qty) * 1000,
            orgn_net_amt=_million_krw(self.orgn_ntby_tr_pbmn),
            scrt_net_qty=_to_int(self.scrt_ntby_qty) * 1000,
            scrt_net_amt=_million_krw(self.scrt_ntby_tr_pbmn),
            ivtr_net_qty=_to_int(self.ivtr_ntby_qty) * 1000,
            ivtr_net_amt=_million_krw(self.ivtr_ntby_tr_pbmn),
            pe_fund_net_qty=_to_int(self.pe_fund_ntby_vol) * 1000,
            pe_fund_net_amt=_million_krw(self.pe_fund_ntby_tr_pbmn),
            bank_net_qty=_to_int(self.bank_ntby_qty) * 1000,
            bank_net_amt=_million_krw(self.bank_ntby_tr_pbmn),
            insu_net_qty=_to_int(self.insu_ntby_qty) * 1000,
            insu_net_amt=_million_krw(self.insu_ntby_tr_pbmn),
            mrbn_net_qty=_to_int(self.mrbn_ntby_qty) * 1000,
            mrbn_net_amt=_million_krw(self.mrbn_ntby_tr_pbmn),
            fund_net_qty=_to_int(self.fund_ntby_qty) * 1000,
            fund_net_amt=_million_krw(self.fund_ntby_tr_pbmn),
            etc_net_qty=_to_int(self.etc_ntby_qty) * 1000,
            etc_net_amt=_million_krw(self.etc_ntby_tr_pbmn),
            etc_corp_net_qty=_to_int(self.etc_corp_ntby_vol) * 1000,
            etc_corp_net_amt=_million_krw(self.etc_corp_ntby_tr_pbmn),
            etc_orgt_net_qty=_to_int(self.etc_orgt_ntby_vol) * 1000,
            etc_orgt_net_amt=_million_krw(self.etc_orgt_ntby_tr_pbmn),
        )


# ── 공매도 일별추이 (FHPST04830000 output2 배열) — PRJ-03 ───────────


class KISShortSaleOutput(BaseModel):
    """종목별 일별 공매도 — ``FHPST04830000`` ``output2`` 배열 원소.

    실측 21필드 중 고유 일별 필드 5종만 선언(단계 1 확정 ②) — 시세 중복 9필드와
    조회 창 의존 누적 계열(``acml_ssts_*``, ``stnd_*_smtn``)은 ``extra="ignore"`` 배제.
    """

    model_config = ConfigDict(extra="ignore")

    stck_bsop_date: str = ""    # 영업일 (YYYYMMDD)
    ssts_cntg_qty: str = ""     # 공매도 체결 수량
    ssts_vol_rlim: str = ""     # 공매도 거래량 비중 (%)
    ssts_tr_pbmn: str = ""      # 공매도 거래대금 — ⚠️ 원 단위 실측 (백만원 아님)
    ssts_tr_pbmn_rlim: str = "" # 공매도 거래대금 비중 (%)
    avrg_prc: str = ""          # 공매도 평균가

    def to_domain(self, symbol: str) -> ShortSaleRecord:
        """``ssts_tr_pbmn``은 필드명과 달리 **원 단위 실측**(probe1 검산:
        65,122,268,750원) — ``_million_krw`` 변환 금지."""
        return ShortSaleRecord(
            symbol=symbol,
            date=_to_date(self.stck_bsop_date),
            short_sale_qty=_to_int(self.ssts_cntg_qty),
            short_sale_vol_ratio=_to_decimal(self.ssts_vol_rlim),
            short_sale_amt=_to_decimal(self.ssts_tr_pbmn),
            short_sale_amt_ratio=_to_decimal(self.ssts_tr_pbmn_rlim),
            avg_price=_to_decimal(self.avrg_prc),
        )


# ── 대차거래추이 (HHPST074500C0 output1 배열) — PRJ-03 ──────────────


class KISLoanTransOutput(BaseModel):
    """종목별 일별 대차거래 — ``HHPST074500C0`` ``output1`` 배열 원소.

    종목 모드(``MRKT_DIV_CLS_CODE="3"``) 스키마는 2026-07-15 EC2 실측으로 확정
    (prj03_stage2_verify.md — 실측 11필드 중 시세 5필드는 ``extra="ignore"`` 배제).
    """

    model_config = ConfigDict(extra="ignore")

    bsop_date: str = ""         # 영업일 (YYYYMMDD)
    new_stcn: str = ""          # 신규 대차 주수
    rdmp_stcn: str = ""         # 상환 주수
    prdy_rmnd_vrss: str = ""    # 전일 대비 잔고 증감
    rmnd_stcn: str = ""         # 대차잔고 주수
    rmnd_amt: str = ""          # 대차잔고 금액 (백만원 — EC2 실측 확정)

    def to_domain(self, symbol: str) -> LoanTransRecord:
        """``rmnd_amt``는 백만원 단위 실측 확정(2026-07-15 EC2 —
        rmnd_amt/rmnd_stcn ×1e6 ≈ 현재가 검산) — 원 단위로 변환."""
        return LoanTransRecord(
            symbol=symbol,
            date=_to_date(self.bsop_date),
            loan_new_qty=_to_int(self.new_stcn),
            loan_redemption_qty=_to_int(self.rdmp_stcn),
            loan_balance_diff=_to_int(self.prdy_rmnd_vrss),
            loan_balance_qty=_to_int(self.rmnd_stcn),
            loan_balance_amt=_million_krw(self.rmnd_amt),
        )


# ── 국내휴장일조회 (CTCA0903R output 배열) — F-23 ───────────────────


class KISHolidayOutput(BaseModel):
    """일자별 영업일/거래일/개장일/결제일 여부 — ``CTCA0903R`` ``output`` 원소.

    KIS 공식 안내: 주문 가능 여부 판정은 개장일여부(``opnd_yn``)를 사용.
    원장 연관 서비스라 가급적 1일 1회 호출(SDK chk_holiday.py 명시).
    """

    model_config = ConfigDict(extra="ignore")

    bass_dt: str = ""       # 기준일자 (YYYYMMDD)
    wday_dvsn_cd: str = ""  # 요일구분코드
    bzdy_yn: str = ""       # 영업일여부 (Y/N)
    tr_day_yn: str = ""     # 거래일여부 (Y/N)
    opnd_yn: str = ""       # 개장일여부 (Y/N)
    sttl_day_yn: str = ""   # 결제일여부 (Y/N)

    def to_domain(self) -> TradingDayRecord:
        return TradingDayRecord(
            date=_to_date(self.bass_dt),
            wday_dvsn_cd=self.wday_dvsn_cd or None,
            is_business_day=self.bzdy_yn == "Y",
            is_trade_day=self.tr_day_yn == "Y",
            is_open=self.opnd_yn == "Y",
            is_settlement_day=self.sttl_day_yn == "Y",
        )


# ── 주문 응답 (TTTC0012U / TTTC0011U output) ────────────────────────


class KISOrderOutput(BaseModel):
    """Order submission response — ``TTTC0012U``/``TTTC0011U`` ``output``."""

    model_config = ConfigDict(extra="ignore")

    KRX_FWDG_ORD_ORGNO: str = ""  # 한국거래소 주문조직번호
    ODNO: str = ""                 # 주문번호
    ORD_TMD: str = ""              # 주문시각 (HHMMSS)


# ── 정정취소가능주문조회 (TTTC0084R output 배열) ─────────────────────


class KISRvseCnclPsblOutput(BaseModel):
    """Cancelable/amendable order — ``TTTC0084R`` ``output`` array element.

    KIS 엔드포인트 ``/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl``.
    정정취소 TR(``order-rvsecncl``) 호출 전 필수 파라미터를 원주문번호(``odno``)로
    역조회하기 위해 사용한다.
    """

    model_config = ConfigDict(extra="ignore")

    odno: str = ""             # 주문번호 (= broker_order_id 매칭 키)
    ord_gno_brno: str = ""     # 주문채번지점번호 (→ KRX_FWDG_ORD_ORGNO)
    ord_dvsn_cd: str = ""      # 주문구분코드 (→ ORD_DVSN)
    psbl_qty: str = ""         # 정정취소 가능수량
    ord_unpr: str = ""         # 주문단가
    sll_buy_dvsn_cd: str = ""  # 매도매수구분코드 (로깅용)
    pdno: str = ""             # 종목코드 (로깅용)


# ── 잔고 — 보유종목 (TTTC8434R output1 배열) ─────────────────────────


class KISBalanceOutput1(BaseModel):
    """Individual position — ``TTTC8434R`` ``output1`` array element."""

    model_config = ConfigDict(extra="ignore")

    pdno: str = ""             # 종목코드
    prdt_name: str = ""        # 종목명
    hldg_qty: str = ""         # 보유수량
    pchs_avg_pric: str = ""    # 매입평균가
    pchs_amt: str = ""         # 매입금액
    prpr: str = ""             # 현재가
    evlu_amt: str = ""         # 평가금액
    evlu_pfls_amt: str = ""    # 평가손익금액
    evlu_pfls_rt: str = ""     # 평가손익률 (%)

    def to_domain(self) -> Position:
        """Convert to ``Position`` domain model."""
        qty = _to_int(self.hldg_qty)
        return Position(
            symbol=self.pdno,
            quantity=qty,
            average_cost=_to_decimal(self.pchs_avg_pric),
            current_price=_to_decimal(self.prpr),
            market_value=_to_decimal(self.evlu_amt),
            unrealized_pnl=_to_decimal(self.evlu_pfls_amt),
            unrealized_pnl_pct=_to_decimal(self.evlu_pfls_rt),
            status=PositionStatus.OPEN if qty > 0 else PositionStatus.CLOSED,
            entry_date=datetime.now(),
        )


# ── 잔고 — 계좌요약 (TTTC8434R output2 첫 번째 항목) ─────────────────


class KISBalanceOutput2(BaseModel):
    """Account summary — ``TTTC8434R`` ``output2`` first element."""

    model_config = ConfigDict(extra="ignore")

    dnca_tot_amt: str = ""          # 예수금 총액
    tot_evlu_amt: str = ""          # 총 평가금액
    nass_amt: str = ""              # 순자산금액
    thdt_buy_amt: str = ""          # 당일 매수금액
    thdt_sll_amt: str = ""          # 당일 매도금액
    pchs_amt_smtl_amt: str = ""     # 매입금액 합계
    evlu_amt_smtl_amt: str = ""     # 평가금액 합계
    evlu_pfls_smtl_amt: str = ""    # 평가손익 합계


# ── 잔고 실현손익 — 계좌요약 (TTTC8494R/VTTC8494R output2 첫 번째 항목) ──


class KISBalanceRlzPlOutput2(BaseModel):
    """주식잔고조회_실현손익 계좌요약 — ``TTTC8494R``/``VTTC8494R`` ``output2`` 첫 요소.

    KIS 엔드포인트 ``/uapi/domestic-stock/v1/trading/inquire-balance-rlz-pl``.
    ``PRCS_DVSN="01"``(전일매매 미포함)로 조회하면 당일분 실현손익만 집계된다.
    """

    model_config = ConfigDict(extra="ignore")

    rlzt_pfls: str = ""             # 실현손익 (당일 합계)
    rlzt_erng_rt: str = ""          # 실현수익률


# ── 매수가능조회 (TTTC8908R/VTTC8908R output) ───────────────────────

class KISPsblOrderOutput(BaseModel):
    """매수가능조회 응답 — ``TTTC8908R``/``VTTC8908R`` ``output`` 객체.

    KIS 엔드포인트 ``/uapi/domestic-stock/v1/trading/inquire-psbl-order``.

    미수/신용 없이 매수 가능한 금액·수량을 조회한다.

    주요 필드:
    - ``nrcvb_buy_amt``: 미수없는매수금액 — **현금 한도 내 매수 가능 금액**
    - ``nrcvb_buy_qty``: 미수없는매수수량 — 현금 한도 내 매수 가능 수량
    - ``ord_psbl_cash``: 주문가능현금 (미수 포함, 사용 지양)
    - ``max_buy_amt`` / ``max_buy_qty``: 미수 포함 최대치 (사용 지양)
    """

    model_config = ConfigDict(extra="ignore")

    ord_psbl_cash: str = ""         # 주문가능현금 (미수 포함)
    nrcvb_buy_amt: str = ""         # 미수없는매수금액
    nrcvb_buy_qty: str = ""         # 미수없는매수수량
    max_buy_amt: str = ""           # 최대매수금액 (미수 포함)
    max_buy_qty: str = ""           # 최대매수수량 (미수 포함)
    psbl_qty_calc_unpr: str = ""    # 가능수량계산단가


# ── 매도가능조회 (TTTC8408R output) ────────────────────────────────

class KISPsblSellOutput(BaseModel):
    """매도가능수량조회 응답 — ``TTTC8408R`` ``output`` 객체.

    KIS 엔드포인트 ``/uapi/domestic-stock/v1/trading/inquire-psbl-sell``.

    보유수량(``cblc_qty``)에서 미체결 매도주문·결제미수 등으로 줄어든
    **실제 주문가능수량(``ord_psbl_qty``)**을 조회한다(F-12 매도 preflight).
    KIS 모의투자(VTTC...)는 본 TR을 미지원할 수 있으므로 호출부에서
    graceful 폴백(None)을 전제로 한다.
    """

    model_config = ConfigDict(extra="ignore")

    ord_psbl_qty: str = ""          # 주문가능수량 (실제 매도 가능)
    cblc_qty: str = ""              # 잔고수량 (보유)


# ── 주문체결조회 (TTTC0081R/VTTC0081R output1 배열) ─────────────────

class KISOrderCcldOutput(BaseModel):
    """Daily order/fill inquiry — ``TTTC0081R``/``VTTC0081R`` ``output1`` row.

    주요 필드:
    - ``odno``: 주문번호 (KIS가 place_order 응답 ODNO와 매칭)
    - ``sll_buy_dvsn_cd``: "01"=매도, "02"=매수
    - ``tot_ccld_qty``: 총 체결 수량
    - ``avg_prvs``: 평균가 (체결 단가 평균)
    - ``rmn_qty``: 미체결 수량 (잔량)
    - ``cncl_yn``: 취소 여부 ("Y"/"N")
    - ``rjct_qty``: 거부 수량
    """

    model_config = ConfigDict(extra="ignore")

    odno: str = ""                  # 주문번호
    orgn_odno: str = ""             # 원주문번호 (정정·취소용)
    pdno: str = ""                  # 종목코드
    sll_buy_dvsn_cd: str = ""       # 01 매도, 02 매수
    ord_qty: str = ""               # 주문 수량
    ord_unpr: str = ""              # 주문 단가
    tot_ccld_qty: str = ""          # 총 체결 수량
    avg_prvs: str = ""              # 평균 체결가
    tot_ccld_amt: str = ""          # 총 체결 금액
    rmn_qty: str = ""               # 잔량 (미체결)
    cncl_yn: str = ""               # 취소여부 Y/N
    rjct_qty: str = ""              # 거부수량
    ord_tmd: str = ""               # 주문시각 (HHMMSS)
    ccld_cndt_name: str = ""        # 체결조건명


# ── Export helpers (used in client.py) ────────────────────────────────

__all__ = [
    "KISBaseResponse",
    "KISPriceOutput",
    "KISDailyChartOutput",
    "KISInvestorFlowOutput",
    "KISMarketInvestorFlowOutput",
    "KISShortSaleOutput",
    "KISLoanTransOutput",
    "KISHolidayOutput",
    "KISOrderOutput",
    "KISOrderCcldOutput",
    "KISBalanceOutput1",
    "KISBalanceOutput2",
    "KISBalanceRlzPlOutput2",
    "KISPsblOrderOutput",
    "KISPsblSellOutput",
    "_to_decimal",
    "_to_int",
]
