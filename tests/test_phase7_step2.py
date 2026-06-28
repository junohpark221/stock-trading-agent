"""Phase 7 Step 2: DB ORM 모델 + 마이그레이션 테스트.

Tests:
- BacktestRun, BacktestTrade ORM 모델 생성/기본값
- __init__.py exports 확인
- 마이그레이션 체인 검증
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Index

from src.db.models import BacktestRun, BacktestTrade

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _get_table_args_by_type(model, cls):
    """__table_args__ 튜플에서 특정 타입의 요소만 추출."""
    return [arg for arg in model.__table_args__ if isinstance(arg, cls)]


def _get_index_names(model):
    """__table_args__에서 Index 이름 목록 추출."""
    return [idx.name for idx in _get_table_args_by_type(model, Index)]


# ---------------------------------------------------------------------------
# BacktestRun Tests
# ---------------------------------------------------------------------------


class TestBacktestRun:
    """BacktestRun ORM 모델 테스트."""

    def test_tablename(self):
        assert BacktestRun.__tablename__ == "backtest_runs"

    def test_create_with_all_fields(self):
        run_id = uuid.uuid4()
        rec = BacktestRun(
            run_id=run_id,
            strategy_type="position",
            mode="technical",
            start_date=date(2023, 1, 1),
            end_date=date(2025, 12, 31),
            initial_capital=Decimal("10000000.00"),
            slippage_bps=10,
            commission_buy_pct=Decimal("0.0150"),
            commission_sell_pct=Decimal("0.1950"),
            symbols=["005930", "000660"],
            parameters={"rsi_period": 14},
            status="completed",
            result_metrics={"sharpe_ratio": 1.5, "mdd_pct": 12.3},
            total_trades=42,
            error_message=None,
            started_at=datetime(2026, 3, 27, 10, 0, 0),
            completed_at=datetime(2026, 3, 27, 10, 5, 0),
        )
        assert rec.run_id == run_id
        assert rec.strategy_type == "position"
        assert rec.mode == "technical"
        assert rec.start_date == date(2023, 1, 1)
        assert rec.end_date == date(2025, 12, 31)
        assert rec.initial_capital == Decimal("10000000.00")
        assert rec.slippage_bps == 10
        assert rec.commission_buy_pct == Decimal("0.0150")
        assert rec.commission_sell_pct == Decimal("0.1950")
        assert rec.symbols == ["005930", "000660"]
        assert rec.parameters == {"rsi_period": 14}
        assert rec.status == "completed"
        assert rec.result_metrics == {"sharpe_ratio": 1.5, "mdd_pct": 12.3}
        assert rec.total_trades == 42
        assert rec.error_message is None
        assert rec.completed_at == datetime(2026, 3, 27, 10, 5, 0)

    def test_defaults_defined(self):
        """default 값이 컬럼에 정의되어 있는지 확인."""
        cols = BacktestRun.__table__.c
        assert cols.mode.default.arg == "technical"
        assert cols.slippage_bps.default.arg == 10
        assert cols.commission_buy_pct.default.arg == Decimal("0.015")
        assert cols.commission_sell_pct.default.arg == Decimal("0.195")
        assert cols.status.default.arg == "pending"
        assert cols.total_trades.default.arg == 0

    def test_nullable_fields_default_none(self):
        run_id = uuid.uuid4()
        rec = BacktestRun(
            run_id=run_id,
            strategy_type="swing",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            initial_capital=Decimal("5000000.00"),
            started_at=datetime(2026, 3, 27, 10, 0, 0),
        )
        assert rec.symbols is None
        assert rec.parameters is None
        assert rec.result_metrics is None
        assert rec.error_message is None
        assert rec.completed_at is None

    def test_result_metrics_jsonb(self):
        """result_metrics JSONB에 복잡한 구조 저장 가능."""
        metrics = {
            "sharpe_ratio": 1.85,
            "sortino_ratio": 2.1,
            "mdd_pct": 8.5,
            "win_rate_pct": 62.5,
            "total_return_pct": 35.2,
            "nested": {"daily_returns": [0.01, -0.02, 0.03]},
        }
        rec = BacktestRun(
            run_id=uuid.uuid4(),
            strategy_type="position",
            start_date=date(2023, 1, 1),
            end_date=date(2025, 12, 31),
            initial_capital=Decimal("10000000.00"),
            started_at=datetime(2026, 3, 27, 10, 0, 0),
            result_metrics=metrics,
        )
        assert rec.result_metrics["sharpe_ratio"] == 1.85
        assert rec.result_metrics["nested"]["daily_returns"] == [0.01, -0.02, 0.03]

    def test_symbols_jsonb_array(self):
        """symbols JSONB에 종목 리스트 저장 가능."""
        symbols = ["005930", "000660", "035420"]
        rec = BacktestRun(
            run_id=uuid.uuid4(),
            strategy_type="swing",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            initial_capital=Decimal("10000000.00"),
            started_at=datetime(2026, 3, 27, 10, 0, 0),
            symbols=symbols,
        )
        assert rec.symbols == ["005930", "000660", "035420"]
        assert len(rec.symbols) == 3

    def test_indices_defined(self):
        names = _get_index_names(BacktestRun)
        assert "ix_backtest_runs_status" in names
        assert "ix_backtest_runs_strategy_type" in names


# ---------------------------------------------------------------------------
# BacktestTrade Tests
# ---------------------------------------------------------------------------


class TestBacktestTrade:
    """BacktestTrade ORM 모델 테스트."""

    def test_tablename(self):
        assert BacktestTrade.__tablename__ == "backtest_trades"

    def test_create_with_all_fields(self):
        run_id = uuid.uuid4()
        trade = BacktestTrade(
            run_id=run_id,
            symbol="005930",
            side="buy",
            quantity=10,
            price=Decimal("71500.00"),
            commission=Decimal("107.25"),
            slippage=Decimal("71.50"),
            trade_date=date(2024, 3, 15),
            pnl=None,
            exit_reason=None,
        )
        assert trade.run_id == run_id
        assert trade.symbol == "005930"
        assert trade.side == "buy"
        assert trade.quantity == 10
        assert trade.price == Decimal("71500.00")
        assert trade.commission == Decimal("107.25")
        assert trade.slippage == Decimal("71.50")
        assert trade.trade_date == date(2024, 3, 15)
        assert trade.pnl is None
        assert trade.exit_reason is None

    def test_defaults_defined(self):
        """commission, slippage default 값 확인."""
        cols = BacktestTrade.__table__.c
        assert cols.commission.default.arg == Decimal("0")
        assert cols.slippage.default.arg == Decimal("0")

    def test_nullable_pnl(self):
        """진입 거래는 pnl=None, 청산 거래는 pnl 값 존재."""
        entry = BacktestTrade(
            run_id=uuid.uuid4(),
            symbol="005930",
            side="buy",
            quantity=10,
            price=Decimal("71500.00"),
            trade_date=date(2024, 3, 15),
        )
        assert entry.pnl is None
        assert entry.exit_reason is None

        exit_trade = BacktestTrade(
            run_id=entry.run_id,
            symbol="005930",
            side="sell",
            quantity=10,
            price=Decimal("73000.00"),
            trade_date=date(2024, 4, 10),
            pnl=Decimal("14500.00"),
            exit_reason="take_profit",
        )
        assert exit_trade.pnl == Decimal("14500.00")
        assert exit_trade.exit_reason == "take_profit"

    def test_fk_column_exists(self):
        """run_id FK 컬럼에 외래키 설정 확인."""
        col = BacktestTrade.__table__.c.run_id
        fk_refs = [fk.target_fullname for fk in col.foreign_keys]
        assert "backtest_runs.run_id" in fk_refs

    def test_indices_defined(self):
        names = _get_index_names(BacktestTrade)
        assert "ix_backtest_trades_run_id" in names
        assert "ix_backtest_trades_symbol" in names


# ---------------------------------------------------------------------------
# Model Exports Tests
# ---------------------------------------------------------------------------


class TestBacktestModelExports:
    """src.db.models 패키지 backtest export 검증."""

    def test_all_models_importable(self):
        from src.db.models import BacktestRun, BacktestTrade

        assert BacktestRun is not None
        assert BacktestTrade is not None

    def test_all_list_contains_new_models(self):
        import src.db.models as models_pkg

        assert "BacktestRun" in models_pkg.__all__
        assert "BacktestTrade" in models_pkg.__all__

    def test_total_model_count(self):
        """Phase 1~7까지 총 18개 모델 export 확인."""
        import src.db.models as models_pkg

        assert len(models_pkg.__all__) == 21


# ---------------------------------------------------------------------------
# Migration Chain Tests
# ---------------------------------------------------------------------------


class TestBacktestMigration:
    """마이그레이션 파일 체인 검증."""

    @pytest.fixture()
    def migration_module(self):
        import importlib.util
        from pathlib import Path

        migration_path = (
            Path(__file__).parent.parent
            / "alembic"
            / "versions"
            / "008_add_backtest_tables.py"
        )
        spec = importlib.util.spec_from_file_location("migration_008", migration_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_revision_id(self, migration_module):
        assert migration_module.revision == "008_backtest"
        assert migration_module.down_revision == "007_scheduler"

    def test_upgrade_downgrade_defined(self, migration_module):
        assert callable(migration_module.upgrade)
        assert callable(migration_module.downgrade)
