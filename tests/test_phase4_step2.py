"""Phase 4 Step 2: DB ORM 모델 + 마이그레이션 테스트.

Tests:
- PositionRecord, PortfolioSnapshot, AgentMemory ORM 모델 생성/기본값
- __init__.py exports 확인
- 마이그레이션 체인 검증
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Index, UniqueConstraint

from src.db.models import AgentMemory, PortfolioSnapshot, PositionRecord

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
# PositionRecord Tests
# ---------------------------------------------------------------------------


class TestPositionRecord:
    """PositionRecord ORM 모델 테스트."""

    def test_tablename(self):
        assert PositionRecord.__tablename__ == "positions"

    def test_create_with_all_fields(self):
        session_id = uuid.uuid4()
        rec = PositionRecord(
            symbol="005930",
            strategy_type="position",
            quantity=10,
            avg_cost=Decimal("70000.00"),
            entry_price=Decimal("70000.00"),
            entry_date=date(2026, 3, 22),
            stop_loss_price=Decimal("66500.00"),
            take_profit_price=Decimal("77000.00"),
            trailing_stop_pct=Decimal("5.0000"),
            max_holding_days=30,
            status="open",
            entry_session_id=session_id,
        )
        assert rec.symbol == "005930"
        assert rec.strategy_type == "position"
        assert rec.quantity == 10
        assert rec.avg_cost == Decimal("70000.00")
        assert rec.entry_price == Decimal("70000.00")
        assert rec.entry_date == date(2026, 3, 22)
        assert rec.stop_loss_price == Decimal("66500.00")
        assert rec.take_profit_price == Decimal("77000.00")
        assert rec.trailing_stop_pct == Decimal("5.0000")
        assert rec.max_holding_days == 30
        assert rec.entry_session_id == session_id

    def test_default_status_defined(self):
        """status 컬럼에 default='open'이 정의되어 있는지 확인 (DB 세션 필요)."""
        col = PositionRecord.__table__.c.status
        assert col.default.arg == "open"

    def test_nullable_fields_default_none(self):
        rec = PositionRecord(
            symbol="005930",
            strategy_type="position",
            quantity=10,
            avg_cost=Decimal("70000.00"),
            entry_price=Decimal("70000.00"),
            entry_date=date(2026, 3, 22),
            stop_loss_price=Decimal("66500.00"),
        )
        assert rec.take_profit_price is None
        assert rec.trailing_stop_pct is None
        assert rec.max_holding_days is None
        assert rec.exit_price is None
        assert rec.exit_date is None
        assert rec.exit_reason is None
        assert rec.realized_pnl is None
        assert rec.entry_session_id is None
        assert rec.exit_session_id is None

    def test_closed_position(self):
        rec = PositionRecord(
            symbol="005930",
            strategy_type="position",
            quantity=10,
            avg_cost=Decimal("70000.00"),
            entry_price=Decimal("70000.00"),
            entry_date=date(2026, 3, 1),
            stop_loss_price=Decimal("66500.00"),
            status="closed",
            exit_price=Decimal("77000.00"),
            exit_date=date(2026, 3, 22),
            exit_reason="take_profit",
            realized_pnl=Decimal("70000.00"),
        )
        assert rec.status == "closed"
        assert rec.exit_price == Decimal("77000.00")
        assert rec.exit_reason == "take_profit"
        assert rec.realized_pnl == Decimal("70000.00")

    def test_indices_defined(self):
        names = _get_index_names(PositionRecord)
        assert "ix_positions_symbol_status" in names
        assert "ix_positions_strategy_status" in names
        assert "ix_positions_entry_date" in names


# ---------------------------------------------------------------------------
# PortfolioSnapshot Tests
# ---------------------------------------------------------------------------


class TestPortfolioSnapshot:
    """PortfolioSnapshot ORM 모델 테스트."""

    def test_tablename(self):
        assert PortfolioSnapshot.__tablename__ == "portfolio_snapshots"

    def test_create_with_all_fields(self):
        sector_data = {"전기전자": 30.5, "바이오": 15.2, "금융": 10.0}
        snap = PortfolioSnapshot(
            snapshot_date=date(2026, 3, 22),
            total_value=Decimal("10000000.00"),
            cash=Decimal("3000000.00"),
            invested=Decimal("7000000.00"),
            unrealized_pnl=Decimal("500000.00"),
            realized_pnl_daily=Decimal("100000.00"),
            peak_value=Decimal("10500000.00"),
            drawdown_pct=Decimal("4.7619"),
            positions_count=5,
            sector_allocations=sector_data,
            trade_count_daily=3,
        )
        assert snap.snapshot_date == date(2026, 3, 22)
        assert snap.total_value == Decimal("10000000.00")
        assert snap.cash == Decimal("3000000.00")
        assert snap.invested == Decimal("7000000.00")
        assert snap.unrealized_pnl == Decimal("500000.00")
        assert snap.realized_pnl_daily == Decimal("100000.00")
        assert snap.peak_value == Decimal("10500000.00")
        assert snap.drawdown_pct == Decimal("4.7619")
        assert snap.positions_count == 5
        assert snap.sector_allocations == sector_data
        assert snap.trade_count_daily == 3

    def test_defaults_defined(self):
        """default 값이 컬럼에 정의되어 있는지 확인."""
        cols = PortfolioSnapshot.__table__.c
        assert cols.realized_pnl_daily.default.arg == Decimal("0")
        assert cols.trade_count_daily.default.arg == 0

    def test_nullable_sector_allocations(self):
        snap = PortfolioSnapshot(
            snapshot_date=date(2026, 3, 22),
            total_value=Decimal("10000000.00"),
            cash=Decimal("10000000.00"),
            invested=Decimal("0.00"),
            unrealized_pnl=Decimal("0.00"),
            peak_value=Decimal("10000000.00"),
            drawdown_pct=Decimal("0.0000"),
            positions_count=0,
        )
        assert snap.sector_allocations is None

    def test_unique_constraint_defined(self):
        constraints = _get_table_args_by_type(PortfolioSnapshot, UniqueConstraint)
        names = [c.name for c in constraints]
        assert "uq_portfolio_snapshots_account_date" in names

    def test_index_defined(self):
        names = _get_index_names(PortfolioSnapshot)
        assert "ix_portfolio_snapshots_date" in names


# ---------------------------------------------------------------------------
# AgentMemory Tests
# ---------------------------------------------------------------------------


class TestAgentMemory:
    """AgentMemory ORM 모델 테스트."""

    def test_tablename(self):
        assert AgentMemory.__tablename__ == "agent_memory"

    def test_create_with_all_fields(self):
        session_id = uuid.uuid4()
        expires = datetime(2026, 6, 22, tzinfo=UTC)
        context_data = {"price": 70000, "volume": 1000000, "rsi": 65.3}
        mem = AgentMemory(
            agent_type="stock_analyst",
            memory_type="pattern",
            symbol="005930",
            content="삼성전자 실적 발표 전 RSI 65 이상에서 매수 시 단기 수익률 양호",
            context=context_data,
            source_session_id=session_id,
            relevance_score=Decimal("0.8500"),
            expires_at=expires,
            is_active=True,
        )
        assert mem.agent_type == "stock_analyst"
        assert mem.memory_type == "pattern"
        assert mem.symbol == "005930"
        assert mem.content.startswith("삼성전자")
        assert mem.context == context_data
        assert mem.source_session_id == session_id
        assert mem.relevance_score == Decimal("0.8500")
        assert mem.expires_at == expires
        assert mem.is_active is True

    def test_defaults_defined(self):
        """default 값이 컬럼에 정의되어 있는지 확인."""
        cols = AgentMemory.__table__.c
        assert cols.relevance_score.default.arg == Decimal("1.0")
        assert cols.is_active.default.arg is True

    def test_nullable_fields_default_none(self):
        mem = AgentMemory(
            agent_type="market_analyst",
            memory_type="lesson",
            content="금리 인상 사이클에서는 성장주보다 가치주 선호",
        )
        assert mem.symbol is None
        assert mem.context is None
        assert mem.source_session_id is None
        assert mem.expires_at is None

    def test_universal_memory_no_symbol(self):
        mem = AgentMemory(
            agent_type="risk_manager",
            memory_type="warning",
            content="연말 결산 시즌에는 대형주 변동성 증가 패턴",
        )
        assert mem.symbol is None

    def test_indices_defined(self):
        names = _get_index_names(AgentMemory)
        assert "ix_agent_memory_agent_active" in names
        assert "ix_agent_memory_symbol_active" in names
        assert "ix_agent_memory_expires" in names


# ---------------------------------------------------------------------------
# Model Exports Tests
# ---------------------------------------------------------------------------


class TestModelExports:
    """src.db.models 패키지 export 검증."""

    def test_all_models_importable(self):
        from src.db.models import AgentMemory, PortfolioSnapshot, PositionRecord

        assert AgentMemory is not None
        assert PortfolioSnapshot is not None
        assert PositionRecord is not None

    def test_all_list_contains_new_models(self):
        import src.db.models as models_pkg

        assert "AgentMemory" in models_pkg.__all__
        assert "PortfolioSnapshot" in models_pkg.__all__
        assert "PositionRecord" in models_pkg.__all__

    def test_total_model_count(self):
        """Phase 1~7까지 총 18개 모델 export 확인."""
        import src.db.models as models_pkg

        assert len(models_pkg.__all__) == 21


# ---------------------------------------------------------------------------
# Migration Chain Tests
# ---------------------------------------------------------------------------


class TestMigrationChain:
    """마이그레이션 파일 체인 검증."""

    @pytest.fixture()
    def migration_module(self):
        import importlib.util
        from pathlib import Path

        migration_path = (
            Path(__file__).parent.parent / "alembic" / "versions" / "005_add_strategy_tables.py"
        )
        spec = importlib.util.spec_from_file_location("migration_005", migration_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_revision_id(self, migration_module):
        assert migration_module.revision == "005_strategy"
        assert migration_module.down_revision == "004_llm_agent"

    def test_upgrade_downgrade_defined(self, migration_module):
        assert callable(migration_module.upgrade)
        assert callable(migration_module.downgrade)
