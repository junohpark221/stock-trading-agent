"""Investor flow ORM models: InvestorFlowDaily, ShortInterestDaily, MarketInvestorFlowDaily.

PRJ-03 수급 데이터 서브시스템 저장 테이블 3종.

공통 정책:
- ``source`` 컬럼('kis'/'krx')으로 수집 경로 구분 — pykrx 백필 행은 'krx',
  KIS 일일 증분 행은 'kis'.
- 데이터 컬럼은 전부 nullable — pykrx 백필 행은 KRX에 없는 축(외국인 등록/비등록,
  매도/매수 총량 분해 등)이 NULL로 남는다.
- 대금(``*_amt``) 컬럼은 **원(KRW) 단위** 통일 저장. KIS ``*_pbmn`` 필드는 백만원
  단위이므로 수집 계층에서 ×1,000,000 변환 책임(원천 정밀도가 백만원 절사임에 유의).
  KRX(pykrx)는 원 단위 그대로.
- 수량(``*_qty``)·대금 모두 순매수 축은 음수 허용.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import BigInteger, Date, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin


class InvestorFlowDaily(TimestampMixin, Base):
    """종목별 일별 투자자 수급 — KIS FHPTJ04160001 / pykrx 백필.

    (symbol, date) 유니크 제약으로 upsert를 보장한다.
    symbol에 FK를 걸지 않아 수집 순서 의존성을 제거한다(DailyOHLCV 동일 정책).

    투자자 주체 15축 × 슬롯 6종(net/sell/buy × qty/amt) = 90 데이터 컬럼.
    축 이름은 KIS 토큰, 접미사는 정규화(KIS 원 필드의 qty/vol·askp/bidp·pbmn
    비일관을 스키마에서 제거 — KIS→컬럼 매핑은 브로커 계층 to_domain 1곳 책임):

    - frgn: 외국인 합계(= KRX 외국인+기타외국인, 게이트 ③ 100% 일치 검증)
    - frgn_reg / frgn_nreg: 등록/비등록 외국인 (KRX 부재 축 — 백필 행 NULL)
    - prsn: 개인 / orgn: 기관합계
    - scrt: 금융투자(ETF LP 오염 축 — 소비 시 분리 표기) / ivtr: 투신 /
      pe_fund: 사모 / bank: 은행 / insu: 보험 / mrbn: 종금·기타금융 / fund: 연기금
    - etc: 기타 합계 / etc_corp: 기타법인 / etc_orgt: 기타단체

    시세 필드(종가·OHLC·거래량·누적대금)는 DailyOHLCV 완전 중복이라 제외(확정 11).
    인덱스는 (symbol, date) 유니크 + date 단독(크로스섹션 조회용)만 둔다 —
    symbol 단독 인덱스는 유니크 제약의 선두 컬럼과 중복이라 미채택.
    """

    __tablename__ = "investor_flow_daily"
    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_investor_flow_daily_symbol_date"),
        Index("ix_investor_flow_daily_date", "date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)

    # ── 외국인 합계 (frgn) ──
    frgn_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    frgn_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    frgn_sell_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    frgn_buy_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    frgn_sell_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    frgn_buy_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # ── 등록 외국인 (frgn_reg) ──
    frgn_reg_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    frgn_reg_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    frgn_reg_sell_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    frgn_reg_buy_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    frgn_reg_sell_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    frgn_reg_buy_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # ── 비등록 외국인 (frgn_nreg) ──
    frgn_nreg_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    frgn_nreg_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    frgn_nreg_sell_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    frgn_nreg_buy_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    frgn_nreg_sell_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    frgn_nreg_buy_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # ── 개인 (prsn) ──
    prsn_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    prsn_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    prsn_sell_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    prsn_buy_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    prsn_sell_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    prsn_buy_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # ── 기관합계 (orgn) ──
    orgn_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    orgn_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    orgn_sell_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    orgn_buy_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    orgn_sell_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    orgn_buy_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # ── 금융투자 (scrt) ──
    scrt_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    scrt_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    scrt_sell_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    scrt_buy_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    scrt_sell_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    scrt_buy_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # ── 투신 (ivtr) ──
    ivtr_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ivtr_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    ivtr_sell_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ivtr_buy_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ivtr_sell_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    ivtr_buy_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # ── 사모 (pe_fund) ──
    pe_fund_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pe_fund_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    pe_fund_sell_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pe_fund_buy_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pe_fund_sell_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    pe_fund_buy_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # ── 은행 (bank) ──
    bank_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bank_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    bank_sell_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bank_buy_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bank_sell_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    bank_buy_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # ── 보험 (insu) ──
    insu_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    insu_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    insu_sell_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    insu_buy_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    insu_sell_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    insu_buy_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # ── 종금·기타금융 (mrbn) ──
    mrbn_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mrbn_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    mrbn_sell_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mrbn_buy_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mrbn_sell_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    mrbn_buy_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # ── 연기금 (fund) ──
    fund_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fund_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    fund_sell_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fund_buy_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fund_sell_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    fund_buy_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # ── 기타 합계 (etc) ──
    etc_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    etc_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    etc_sell_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    etc_buy_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    etc_sell_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    etc_buy_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # ── 기타법인 (etc_corp) ──
    etc_corp_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    etc_corp_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    etc_corp_sell_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    etc_corp_buy_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    etc_corp_sell_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    etc_corp_buy_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # ── 기타단체 (etc_orgt) ──
    etc_orgt_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    etc_orgt_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    etc_orgt_sell_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    etc_orgt_buy_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    etc_orgt_sell_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    etc_orgt_buy_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)


class ShortInterestDaily(TimestampMixin, Base):
    """종목별 일별 공매도·대차 — KIS FHPST04830000(공매도) + HHPST074500C0(대차).

    두 TR이 같은 (symbol, date) 행의 각자 컬럼 절반을 채우는 partial upsert 전제 —
    전 데이터 컬럼 nullable. 시세 중복 필드와 조회 창 의존 누적 계열
    (acml_ssts_*, stnd_*_smtn — 요청 구간에 따라 값이 달라져 일별 저장 부적합)은 제외.

    - ``short_sale_amt``: FHPST04830000 대금은 **원 단위 실측**(게이트 ① 검산) —
      ×1e6 변환 불필요(백만원 단위인 수급 TR과 다름).
    - ``loan_balance_amt``: 단위 미실측 — 단계 2에서 실검증 후 원 단위 통일은
      수집 계층 책임. 대차 TR은 시세 필드에 지수 값이 혼입되는 실측 이상이 있어
      필드 신뢰도 주의.
    """

    __tablename__ = "short_interest_daily"
    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_short_interest_daily_symbol_date"),
        Index("ix_short_interest_daily_date", "date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)

    # ── 공매도 (FHPST04830000) ──
    short_sale_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    short_sale_vol_ratio: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    short_sale_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    short_sale_amt_ratio: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    avg_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)

    # ── 대차 (HHPST074500C0) ──
    loan_new_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    loan_redemption_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    loan_balance_diff: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    loan_balance_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    loan_balance_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)


class MarketInvestorFlowDaily(TimestampMixin, Base):
    """시장 단위 일별 투자자 수급 + 지수 OHLC — KIS FHPTJ04040000 / pykrx 지수 백필.

    (market, date) 유니크. market은 'kospi'/'kosdaq' 소문자
    (StockMaster.market_type 컨벤션과 동일). 지수 OHLC는 시스템 내 유일 소스(확정 16).

    - ``index_volume``/``index_trading_value``/``index_market_cap``: KRX(pykrx)
      백필 전용 축 — KIS TR에 없어 'kis' 행은 NULL.
    - 수급은 15축 × net_qty/net_amt (시장 TR에는 매도/매수 총량 분해 없음).
    - ⚠️ 시장 TR ``*_ntby_qty`` 수량 단위 미검증(전 시장 합계치가 천주 단위로 의심됨) —
      단계 2 실검증 후 주 단위 통일은 수집 계층 책임.
    """

    __tablename__ = "market_investor_flow_daily"
    __table_args__ = (
        UniqueConstraint("market", "date", name="uq_market_investor_flow_daily_market_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)

    # ── 지수 (KIS: bstp_nmix_* / pykrx 지수 OHLCV) ──
    index_open: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    index_high: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    index_low: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    index_close: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    index_prev_close: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    index_change: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    index_change_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    index_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    index_trading_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    index_market_cap: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # ── 수급 15축 × net_qty/net_amt ──
    frgn_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    frgn_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    frgn_reg_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    frgn_reg_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    frgn_nreg_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    frgn_nreg_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    prsn_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    prsn_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    orgn_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    orgn_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    scrt_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    scrt_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    ivtr_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ivtr_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    pe_fund_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pe_fund_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    bank_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bank_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    insu_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    insu_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    mrbn_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mrbn_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    fund_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fund_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    etc_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    etc_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    etc_corp_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    etc_corp_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    etc_orgt_net_qty: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    etc_orgt_net_amt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
