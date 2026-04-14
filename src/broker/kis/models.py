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
from src.core.models import OHLCV, Position, PriceInfo

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
            date=date(
                int(self.stck_bsop_date[:4]),
                int(self.stck_bsop_date[4:6]),
                int(self.stck_bsop_date[6:8]),
            ),
            open=_to_decimal(self.stck_oprc),
            high=_to_decimal(self.stck_hgpr),
            low=_to_decimal(self.stck_lwpr),
            close=_to_decimal(self.stck_clpr),
            volume=_to_int(self.acml_vol),
            value=_to_decimal(self.acml_tr_pbmn),
        )


# ── 주문 응답 (TTTC0012U / TTTC0011U output) ────────────────────────


class KISOrderOutput(BaseModel):
    """Order submission response — ``TTTC0012U``/``TTTC0011U`` ``output``."""

    model_config = ConfigDict(extra="ignore")

    KRX_FWDG_ORD_ORGNO: str = ""  # 한국거래소 주문조직번호
    ODNO: str = ""                 # 주문번호
    ORD_TMD: str = ""              # 주문시각 (HHMMSS)


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
    "KISOrderOutput",
    "KISOrderCcldOutput",
    "KISBalanceOutput1",
    "KISBalanceOutput2",
    "_to_decimal",
    "_to_int",
]
