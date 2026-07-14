"""Tests for investor flow DB models (PRJ-03 단계 1).

InvestorFlowDaily / ShortInterestDaily / MarketInvestorFlowDaily의
ORM 메타데이터 검증 — DB 접속 없이 inspect/__table__ 기반.
"""

from sqlalchemy import inspect

from src.db.models.investor_flow import (
    InvestorFlowDaily,
    MarketInvestorFlowDaily,
    ShortInterestDaily,
)

# 투자자 주체 15축 — 모델·마이그레이션과 동일 정의
AXES = (
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
)
FULL_SLOTS = ("net_qty", "net_amt", "sell_qty", "buy_qty", "sell_amt", "buy_amt")
NET_SLOTS = ("net_qty", "net_amt")


# ── Helpers ──────────────────────────────────────────────────────────


def _col_map(model):
    """Return {col_name: Column} dict from ORM mapper."""
    mapper = inspect(model)
    return {c.key: c for c in mapper.columns}


def _table(model):
    """Return the underlying Table object."""
    return model.__table__


def _unique_constraint_names(model):
    """Return set of unique constraint names."""
    return {
        c.name
        for c in _table(model).constraints
        if hasattr(c, "columns") and len(c.columns) > 0 and c.name and c.name.startswith("uq_")
    }


def _index_names(model):
    """Return set of index names."""
    return {idx.name for idx in _table(model).indexes}


# ── TestInvestorFlowDailyModel ───────────────────────────────────────


class TestInvestorFlowDailyModel:
    """investor_flow_daily 테이블 ORM 메타데이터 검증."""

    def test_tablename(self):
        assert InvestorFlowDaily.__tablename__ == "investor_flow_daily"

    def test_column_count(self):
        """96 columns: id + symbol + date + source + 90 flow + created/updated_at."""
        assert len(_col_map(InvestorFlowDaily)) == 96

    def test_primary_key(self):
        pk_cols = [c.name for c in _table(InvestorFlowDaily).primary_key.columns]
        assert pk_cols == ["id"]

    def test_key_columns_not_nullable(self):
        table = _table(InvestorFlowDaily)
        for name in ("symbol", "date", "source"):
            assert table.c[name].nullable is False, f"{name} should be NOT NULL"

    def test_all_flow_columns_exist_and_nullable(self):
        """15축 × 6슬롯 = 90 데이터 컬럼 전수 검증."""
        table = _table(InvestorFlowDaily)
        count = 0
        for axis in AXES:
            for slot in FULL_SLOTS:
                name = f"{axis}_{slot}"
                assert name in table.c, f"missing column: {name}"
                assert table.c[name].nullable is True, f"{name} should be nullable"
                count += 1
        assert count == 90

    def test_qty_columns_biginteger(self):
        table = _table(InvestorFlowDaily)
        for axis in AXES:
            for slot in ("net_qty", "sell_qty", "buy_qty"):
                col_type = str(table.c[f"{axis}_{slot}"].type).upper()
                assert "BIGINT" in col_type, f"{axis}_{slot} should be BigInteger"

    def test_amt_columns_numeric_20_0(self):
        """대금은 원(KRW) 단위 Numeric(20,0) — 확정 17."""
        table = _table(InvestorFlowDaily)
        for axis in AXES:
            for slot in ("net_amt", "sell_amt", "buy_amt"):
                col = table.c[f"{axis}_{slot}"]
                assert col.type.precision == 20, f"{axis}_{slot} precision"
                assert col.type.scale == 0, f"{axis}_{slot} scale"

    def test_unique_symbol_date(self):
        assert "uq_investor_flow_daily_symbol_date" in _unique_constraint_names(InvestorFlowDaily)

    def test_date_index(self):
        assert "ix_investor_flow_daily_date" in _index_names(InvestorFlowDaily)


# ── TestShortInterestDailyModel ──────────────────────────────────────


class TestShortInterestDailyModel:
    """short_interest_daily 테이블 ORM 메타데이터 검증."""

    def test_tablename(self):
        assert ShortInterestDaily.__tablename__ == "short_interest_daily"

    def test_column_count(self):
        """16 columns: id + symbol + date + source + 공매도 5 + 대차 5 + created/updated_at."""
        assert len(_col_map(ShortInterestDaily)) == 16

    def test_primary_key(self):
        pk_cols = [c.name for c in _table(ShortInterestDaily).primary_key.columns]
        assert pk_cols == ["id"]

    def test_key_columns_not_nullable(self):
        table = _table(ShortInterestDaily)
        for name in ("symbol", "date", "source"):
            assert table.c[name].nullable is False, f"{name} should be NOT NULL"

    def test_data_columns_all_nullable(self):
        """공매도·대차 두 TR이 각자 절반씩 채우는 partial upsert 전제."""
        table = _table(ShortInterestDaily)
        for name in (
            "short_sale_qty",
            "short_sale_vol_ratio",
            "short_sale_amt",
            "short_sale_amt_ratio",
            "avg_price",
            "loan_new_qty",
            "loan_redemption_qty",
            "loan_balance_diff",
            "loan_balance_qty",
            "loan_balance_amt",
        ):
            assert table.c[name].nullable is True, f"{name} should be nullable"

    def test_ratio_precision(self):
        table = _table(ShortInterestDaily)
        for name in ("short_sale_vol_ratio", "short_sale_amt_ratio"):
            assert table.c[name].type.precision == 10
            assert table.c[name].type.scale == 4

    def test_amt_precision(self):
        table = _table(ShortInterestDaily)
        for name in ("short_sale_amt", "loan_balance_amt"):
            assert table.c[name].type.precision == 20
            assert table.c[name].type.scale == 0

    def test_unique_symbol_date(self):
        assert "uq_short_interest_daily_symbol_date" in _unique_constraint_names(ShortInterestDaily)

    def test_date_index(self):
        assert "ix_short_interest_daily_date" in _index_names(ShortInterestDaily)


# ── TestMarketInvestorFlowDailyModel ─────────────────────────────────


class TestMarketInvestorFlowDailyModel:
    """market_investor_flow_daily 테이블 ORM 메타데이터 검증."""

    def test_tablename(self):
        assert MarketInvestorFlowDaily.__tablename__ == "market_investor_flow_daily"

    def test_column_count(self):
        """46 columns: id + market + date + source + 지수 10 + 수급 30 + created/updated_at."""
        assert len(_col_map(MarketInvestorFlowDaily)) == 46

    def test_primary_key(self):
        pk_cols = [c.name for c in _table(MarketInvestorFlowDaily).primary_key.columns]
        assert pk_cols == ["id"]

    def test_key_columns_not_nullable(self):
        table = _table(MarketInvestorFlowDaily)
        for name in ("market", "date", "source"):
            assert table.c[name].nullable is False, f"{name} should be NOT NULL"

    def test_index_columns_exist_and_nullable(self):
        """지수 10컬럼 — KIS 7 + KRX 백필 전용 3(volume/trading_value/market_cap)."""
        table = _table(MarketInvestorFlowDaily)
        for name in (
            "index_open",
            "index_high",
            "index_low",
            "index_close",
            "index_prev_close",
            "index_change",
            "index_change_rate",
            "index_volume",
            "index_trading_value",
            "index_market_cap",
        ):
            assert name in table.c, f"missing column: {name}"
            assert table.c[name].nullable is True, f"{name} should be nullable"

    def test_index_ohlc_precision(self):
        table = _table(MarketInvestorFlowDaily)
        for name in ("index_open", "index_high", "index_low", "index_close"):
            assert table.c[name].type.precision == 15
            assert table.c[name].type.scale == 2

    def test_all_flow_columns_exist_and_nullable(self):
        """15축 × net_qty/net_amt = 30 수급 컬럼 전수 검증 (매도/매수 분해 없음)."""
        table = _table(MarketInvestorFlowDaily)
        count = 0
        for axis in AXES:
            for slot in NET_SLOTS:
                name = f"{axis}_{slot}"
                assert name in table.c, f"missing column: {name}"
                assert table.c[name].nullable is True, f"{name} should be nullable"
                count += 1
            for slot in ("sell_qty", "buy_qty", "sell_amt", "buy_amt"):
                assert f"{axis}_{slot}" not in table.c, f"{axis}_{slot} should not exist"
        assert count == 30

    def test_unique_market_date(self):
        assert "uq_market_investor_flow_daily_market_date" in _unique_constraint_names(
            MarketInvestorFlowDaily
        )


# ── TestModelsRegistered ─────────────────────────────────────────────


class TestModelsRegistered:
    """패키지 import 및 __all__ 검증 (Alembic autogenerate 인식 조건)."""

    def test_all_exports(self):
        from src.db.models import __all__

        assert "InvestorFlowDaily" in __all__
        assert "ShortInterestDaily" in __all__
        assert "MarketInvestorFlowDaily" in __all__

    def test_package_import(self):
        from src.db.models import (
            InvestorFlowDaily,
            MarketInvestorFlowDaily,
            ShortInterestDaily,
        )

        assert InvestorFlowDaily.__tablename__ == "investor_flow_daily"
        assert ShortInterestDaily.__tablename__ == "short_interest_daily"
        assert MarketInvestorFlowDaily.__tablename__ == "market_investor_flow_daily"
