"""Phase 5 Step 2: DB ORM 모델 + 마이그레이션 테스트.

Tests:
- Order, Execution, ApprovalRequestDB ORM 모델 생성/기본값
- __init__.py exports 확인
- 마이그레이션 체인 검증
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Index, UniqueConstraint

from src.db.models import ApprovalRequestDB, Execution, Order

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
# Order Tests
# ---------------------------------------------------------------------------


class TestOrder:
    """Order ORM 모델 테스트."""

    def test_tablename(self):
        assert Order.__tablename__ == "orders"

    def test_create_with_all_fields(self):
        session_id = uuid.uuid4()
        decision_id = uuid.uuid4()
        executed = datetime(2026, 3, 24, 10, 30, 0, tzinfo=UTC)
        order = Order(
            symbol="005930",
            side="buy",
            order_type="limit",
            quantity=100,
            price=Decimal("70000.00"),
            status="filled",
            approval_status="approved",
            original_quantity=100,
            modified_quantity=None,
            session_id=session_id,
            trade_decision_id=decision_id,
            position_id=42,
            broker_order_id="KIS-001",
            filled_quantity=100,
            filled_price=Decimal("69900.00"),
            commission=Decimal("69.90"),
            executed_at=executed,
            rejection_reason="",
            web_verify_result="safe",
            web_verify_summary="특이사항 없음",
        )
        assert order.symbol == "005930"
        assert order.side == "buy"
        assert order.order_type == "limit"
        assert order.quantity == 100
        assert order.price == Decimal("70000.00")
        assert order.status == "filled"
        assert order.approval_status == "approved"
        assert order.original_quantity == 100
        assert order.modified_quantity is None
        assert order.session_id == session_id
        assert order.trade_decision_id == decision_id
        assert order.position_id == 42
        assert order.broker_order_id == "KIS-001"
        assert order.filled_quantity == 100
        assert order.filled_price == Decimal("69900.00")
        assert order.commission == Decimal("69.90")
        assert order.executed_at == executed
        assert order.web_verify_result == "safe"
        assert order.web_verify_summary == "특이사항 없음"

    def test_default_status(self):
        col = Order.__table__.c.status
        assert col.default.arg == "pending"

    def test_default_approval_status(self):
        col = Order.__table__.c.approval_status
        assert col.default.arg == "auto_approved"

    def test_default_filled_quantity(self):
        col = Order.__table__.c.filled_quantity
        assert col.default.arg == 0

    def test_default_commission(self):
        col = Order.__table__.c.commission
        assert col.default.arg == Decimal("0")

    def test_nullable_fields_default_none(self):
        order = Order(
            symbol="005930",
            side="buy",
            order_type="limit",
            quantity=100,
            price=Decimal("70000.00"),
            original_quantity=100,
        )
        assert order.modified_quantity is None
        assert order.session_id is None
        assert order.trade_decision_id is None
        assert order.position_id is None
        assert order.broker_order_id is None
        assert order.filled_price is None
        assert order.executed_at is None
        assert order.web_verify_result is None

    def test_indices_defined(self):
        names = _get_index_names(Order)
        assert "ix_orders_symbol_status" in names
        assert "ix_orders_session_id" in names
        assert "ix_orders_created_at" in names


# ---------------------------------------------------------------------------
# Execution Tests
# ---------------------------------------------------------------------------


class TestExecution:
    """Execution ORM 모델 테스트."""

    def test_tablename(self):
        assert Execution.__tablename__ == "executions"

    def test_create_with_all_fields(self):
        executed = datetime(2026, 3, 24, 10, 30, 0, tzinfo=UTC)
        exe = Execution(
            order_id=1,
            broker_order_id="KIS-001",
            fill_price=Decimal("69900.00"),
            fill_quantity=100,
            commission=Decimal("69.90"),
            executed_at=executed,
        )
        assert exe.order_id == 1
        assert exe.broker_order_id == "KIS-001"
        assert exe.fill_price == Decimal("69900.00")
        assert exe.fill_quantity == 100
        assert exe.commission == Decimal("69.90")
        assert exe.executed_at == executed

    def test_default_commission(self):
        col = Execution.__table__.c.commission
        assert col.default.arg == Decimal("0")

    def test_indices_defined(self):
        names = _get_index_names(Execution)
        assert "ix_executions_order_id" in names
        assert "ix_executions_executed_at" in names


# ---------------------------------------------------------------------------
# ApprovalRequestDB Tests
# ---------------------------------------------------------------------------


class TestApprovalRequestDB:
    """ApprovalRequestDB ORM 모델 테스트."""

    def test_tablename(self):
        assert ApprovalRequestDB.__tablename__ == "approval_requests"

    def test_create_with_all_fields(self):
        req_id = uuid.uuid4()
        requested = datetime(2026, 3, 24, 10, 0, 0, tzinfo=UTC)
        responded = datetime(2026, 3, 24, 10, 3, 0, tzinfo=UTC)
        expires = datetime(2026, 3, 24, 10, 5, 0, tzinfo=UTC)
        req = ApprovalRequestDB(
            request_id=req_id,
            order_id=1,
            status="approved",
            telegram_message_id=12345,
            requested_at=requested,
            responded_at=responded,
            modified_quantity=50,
            response_reason="수량 50으로 축소 승인",
            expires_at=expires,
        )
        assert req.request_id == req_id
        assert req.order_id == 1
        assert req.status == "approved"
        assert req.telegram_message_id == 12345
        assert req.requested_at == requested
        assert req.responded_at == responded
        assert req.modified_quantity == 50
        assert req.response_reason == "수량 50으로 축소 승인"
        assert req.expires_at == expires

    def test_default_status(self):
        col = ApprovalRequestDB.__table__.c.status
        assert col.default.arg == "pending"

    def test_default_response_reason(self):
        col = ApprovalRequestDB.__table__.c.response_reason
        assert col.default.arg == ""

    def test_request_id_unique_constraint(self):
        col = ApprovalRequestDB.__table__.c.request_id
        assert col.unique is True

    def test_indices_defined(self):
        names = _get_index_names(ApprovalRequestDB)
        assert "ix_approval_requests_order_id" in names
        assert "ix_approval_requests_status" in names
        assert "ix_approval_requests_request_id" in names


# ---------------------------------------------------------------------------
# Model Exports Tests
# ---------------------------------------------------------------------------


class TestModelExports:
    """src.db.models 패키지 export 검증."""

    def test_all_models_importable(self):
        from src.db.models import ApprovalRequestDB, Execution, Order

        assert ApprovalRequestDB is not None
        assert Execution is not None
        assert Order is not None

    def test_all_list_contains_new_models(self):
        import src.db.models as models_pkg

        assert "ApprovalRequestDB" in models_pkg.__all__
        assert "Execution" in models_pkg.__all__
        assert "Order" in models_pkg.__all__

    def test_total_model_count(self):
        """Phase 1~7까지 총 18개 모델 export 확인."""
        import src.db.models as models_pkg

        assert len(models_pkg.__all__) == 18


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
            Path(__file__).parent.parent / "alembic" / "versions" / "006_add_execution_tables.py"
        )
        spec = importlib.util.spec_from_file_location("migration_006", migration_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_revision_id(self, migration_module):
        assert migration_module.revision == "006_execution"
        assert migration_module.down_revision == "005_strategy"

    def test_upgrade_downgrade_defined(self, migration_module):
        assert callable(migration_module.upgrade)
        assert callable(migration_module.downgrade)
