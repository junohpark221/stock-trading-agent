"""Unit tests for Orders API endpoints.

Uses httpx AsyncClient with FastAPI test transport.
All external dependencies (executor, broker, DB) are mocked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import src.main as main_mod
from src.core.enums import ApprovalStatus, OrderSide, OrderStatus
from src.core.models import ExecutionResult
from src.db.session import get_db_session


# ── Helpers ───────────────────────────────────────────────────────────


def _mock_order_row(**kwargs) -> MagicMock:
    """Mock Order ORM row."""
    row = MagicMock()
    row.id = kwargs.get("id", 1)
    row.symbol = kwargs.get("symbol", "005930")
    row.side = kwargs.get("side", "buy")
    row.order_type = kwargs.get("order_type", "limit")
    row.quantity = kwargs.get("quantity", 10)
    row.price = kwargs.get("price", Decimal("78000"))
    row.status = kwargs.get("status", "pending")
    row.approval_status = kwargs.get("approval_status", "auto_approved")
    row.original_quantity = kwargs.get("original_quantity", 10)
    row.modified_quantity = kwargs.get("modified_quantity", None)
    row.session_id = kwargs.get("session_id", None)
    row.trade_decision_id = kwargs.get("trade_decision_id", None)
    row.broker_order_id = kwargs.get("broker_order_id", None)
    row.filled_quantity = kwargs.get("filled_quantity", 0)
    row.filled_price = kwargs.get("filled_price", None)
    row.commission = kwargs.get("commission", Decimal("0"))
    row.rejection_reason = kwargs.get("rejection_reason", "")
    row.web_verify_result = kwargs.get("web_verify_result", None)
    row.web_verify_summary = kwargs.get("web_verify_summary", "")
    row.created_at = kwargs.get("created_at", datetime(2026, 3, 25, 9, 0, tzinfo=UTC))
    row.executed_at = kwargs.get("executed_at", None)
    return row


def _mock_execution_row(**kwargs) -> MagicMock:
    """Mock Execution ORM row."""
    row = MagicMock()
    row.id = kwargs.get("id", 1)
    row.order_id = kwargs.get("order_id", 1)
    row.broker_order_id = kwargs.get("broker_order_id", "KIS-001")
    row.fill_price = kwargs.get("fill_price", Decimal("78000"))
    row.fill_quantity = kwargs.get("fill_quantity", 10)
    row.commission = kwargs.get("commission", Decimal("234"))
    row.executed_at = kwargs.get("executed_at", datetime(2026, 3, 25, 9, 1, tzinfo=UTC))
    return row


def _mock_approval_row(**kwargs) -> MagicMock:
    """Mock ApprovalRequestDB ORM row."""
    row = MagicMock()
    row.id = kwargs.get("id", 1)
    row.request_id = kwargs.get("request_id", uuid.uuid4())
    row.order_id = kwargs.get("order_id", 1)
    row.status = kwargs.get("status", "pending")
    row.requested_at = kwargs.get("requested_at", datetime(2026, 3, 25, 9, 0, tzinfo=UTC))
    row.responded_at = kwargs.get("responded_at", None)
    row.modified_quantity = kwargs.get("modified_quantity", None)
    row.response_reason = kwargs.get("response_reason", "")
    row.expires_at = kwargs.get("expires_at", datetime(2026, 3, 25, 9, 5, tzinfo=UTC))
    row.telegram_message_id = kwargs.get("telegram_message_id", None)
    return row


def _execution_result(success: bool = True, **kwargs) -> ExecutionResult:
    """Sample ExecutionResult."""
    return ExecutionResult(
        success=success,
        order_id=kwargs.get("order_id", 1),
        broker_order_id=kwargs.get("broker_order_id", "KIS-001"),
        symbol=kwargs.get("symbol", "005930"),
        side=kwargs.get("side", OrderSide.BUY),
        quantity=kwargs.get("quantity", 10),
        fill_price=kwargs.get("fill_price", Decimal("78000")),
        commission=kwargs.get("commission", Decimal("234")),
        approval_status=kwargs.get("approval_status", ApprovalStatus.AUTO_APPROVED),
        web_verify_result=kwargs.get("web_verify_result", None),
        position_id=kwargs.get("position_id", 1),
        decision_ids=[],
        error=kwargs.get("error", ""),
    )


# ── Mock DB helper ───────────────────────────────────────────────────


def _scalars_result(rows):
    """Build a mock result that supports .scalars().all()."""
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows
    result = MagicMock()
    result.scalars.return_value = scalars_mock
    return result


def _scalar_one_result(value):
    """Build a mock result that supports .scalar_one()."""
    result = MagicMock()
    result.scalar_one.return_value = value
    return result


def _scalar_one_or_none_result(value):
    """Build a mock result that supports .scalar_one_or_none()."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_db_session():
    """Mock AsyncSession for DI override."""
    return AsyncMock()


@pytest.fixture
def app(mock_db_session):
    """FastAPI app with dependency overrides."""
    main_mod.app.dependency_overrides[get_db_session] = lambda: mock_db_session
    yield main_mod.app
    main_mod.app.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    """httpx AsyncClient bound to the test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ═══════════════════════════════════════════════════════════════════════
# GET /api/orders
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_orders_default(client, mock_db_session):
    """GET /api/orders — 기본 조회 (필터 없음)."""
    rows = [_mock_order_row(id=1), _mock_order_row(id=2, symbol="000660")]

    mock_db_session.execute = AsyncMock(side_effect=[
        _scalar_one_result(2),   # count
        _scalars_result(rows),   # select
    ])

    resp = await client.get("/api/orders")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2


@pytest.mark.asyncio
async def test_list_orders_filter_symbol(client, mock_db_session):
    """GET /api/orders?symbol=005930 — symbol 필터."""
    rows = [_mock_order_row(id=1, symbol="005930")]
    mock_db_session.execute = AsyncMock(side_effect=[
        _scalar_one_result(1),
        _scalars_result(rows),
    ])

    resp = await client.get("/api/orders", params={"symbol": "005930"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["symbol"] == "005930"


@pytest.mark.asyncio
async def test_list_orders_filter_status(client, mock_db_session):
    """GET /api/orders?status=filled — status 필터."""
    rows = [_mock_order_row(id=1, status="filled")]
    mock_db_session.execute = AsyncMock(side_effect=[
        _scalar_one_result(1),
        _scalars_result(rows),
    ])

    resp = await client.get("/api/orders", params={"status": "filled"})
    assert resp.status_code == 200
    assert resp.json()["items"][0]["status"] == "filled"


@pytest.mark.asyncio
async def test_list_orders_filter_date_range(client, mock_db_session):
    """GET /api/orders?from_date=2026-03-20&to_date=2026-03-25 — 날짜 범위."""
    rows = [_mock_order_row(id=1)]
    mock_db_session.execute = AsyncMock(side_effect=[
        _scalar_one_result(1),
        _scalars_result(rows),
    ])

    resp = await client.get("/api/orders", params={
        "from_date": "2026-03-20", "to_date": "2026-03-25",
    })
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_list_orders_empty(client, mock_db_session):
    """GET /api/orders — 빈 결과."""
    mock_db_session.execute = AsyncMock(side_effect=[
        _scalar_one_result(0),
        _scalars_result([]),
    ])

    resp = await client.get("/api/orders")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    assert resp.json()["items"] == []


# ═══════════════════════════════════════════════════════════════════════
# GET /api/orders/{order_id}
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_order_detail_success(client, mock_db_session):
    """GET /api/orders/1 — 주문+체결+승인 전체 반환."""
    order_row = _mock_order_row(id=1, status="filled")
    exec_rows = [_mock_execution_row(order_id=1)]
    approval_row = _mock_approval_row(order_id=1)

    mock_db_session.execute = AsyncMock(side_effect=[
        _scalar_one_or_none_result(order_row),    # Order
        _scalars_result(exec_rows),               # Executions
        _scalar_one_or_none_result(approval_row), # ApprovalRequestDB
    ])

    resp = await client.get("/api/orders/1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["order"]["id"] == 1
    assert len(data["executions"]) == 1
    assert data["approval"] is not None
    assert data["approval"]["order_id"] == 1


@pytest.mark.asyncio
async def test_order_detail_not_found(client, mock_db_session):
    """GET /api/orders/999 — 404."""
    mock_db_session.execute = AsyncMock(return_value=_scalar_one_or_none_result(None))

    resp = await client.get("/api/orders/999")
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
# POST /api/orders/execute
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@patch("src.api.routes.orders._build_executor")
async def test_execute_order_success(mock_build, client):
    """POST /api/orders/execute — 정상 실행."""
    mock_executor = MagicMock()
    mock_executor.execute_entry = AsyncMock(return_value=_execution_result(success=True))
    mock_broker = AsyncMock()
    mock_build.return_value = (mock_executor, mock_broker)

    resp = await client.post("/api/orders/execute", json={
        "symbol": "005930",
        "side": "buy",
        "quantity": 10,
        "price": "78000",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["symbol"] == "005930"
    mock_broker.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@patch("src.api.routes.orders._build_executor")
async def test_execute_order_failure(mock_build, client):
    """POST /api/orders/execute — executor 실패."""
    mock_executor = MagicMock()
    mock_executor.execute_entry = AsyncMock(
        return_value=_execution_result(success=False, error="Web 검증 차단")
    )
    mock_broker = AsyncMock()
    mock_build.return_value = (mock_executor, mock_broker)

    resp = await client.post("/api/orders/execute", json={
        "symbol": "005930",
        "side": "buy",
        "quantity": 10,
        "price": "78000",
    })
    assert resp.status_code == 422
    data = resp.json()
    assert data["success"] is False
    assert "차단" in data["error"]


@pytest.mark.asyncio
async def test_execute_order_validation_error(client):
    """POST /api/orders/execute — quantity=0 → 422 validation."""
    resp = await client.post("/api/orders/execute", json={
        "symbol": "005930",
        "side": "buy",
        "quantity": 0,
        "price": "78000",
    })
    assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════
# GET /api/orders/approvals/pending
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_pending_approvals_list(client, mock_db_session):
    """GET /api/orders/approvals/pending — pending 목록 반환."""
    approval_row = _mock_approval_row(order_id=1)
    order_row = _mock_order_row(id=1)

    mock_db_session.execute = AsyncMock(side_effect=[
        _scalars_result([approval_row]),             # ApprovalRequestDB
        _scalar_one_or_none_result(order_row),       # Order join
    ])

    resp = await client.get("/api/orders/approvals/pending")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["order_id"] == 1
    assert data["items"][0]["symbol"] == "005930"


@pytest.mark.asyncio
async def test_pending_approvals_empty(client, mock_db_session):
    """GET /api/orders/approvals/pending — 빈 목록."""
    mock_db_session.execute = AsyncMock(return_value=_scalars_result([]))

    resp = await client.get("/api/orders/approvals/pending")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    assert resp.json()["items"] == []


# ═══════════════════════════════════════════════════════════════════════
# POST /api/orders/approvals/{request_id}/respond
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_approval_respond_approve(client, mock_db_session):
    """POST /api/orders/approvals/{id}/respond — 승인."""
    request_id = uuid.uuid4()
    approval_row = _mock_approval_row(request_id=request_id, status="pending")
    mock_db_session.execute = AsyncMock(side_effect=[
        _scalar_one_or_none_result(approval_row),  # lookup
        MagicMock(),  # update ApprovalRequestDB
        MagicMock(),  # update Order
    ])
    mock_db_session.commit = AsyncMock()

    resp = await client.post(
        f"/api/orders/approvals/{request_id}/respond",
        json={"action": "approve"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["status"] == "approved"


@pytest.mark.asyncio
async def test_approval_respond_reject(client, mock_db_session):
    """POST /api/orders/approvals/{id}/respond — 거부."""
    request_id = uuid.uuid4()
    approval_row = _mock_approval_row(request_id=request_id, status="pending")
    mock_db_session.execute = AsyncMock(side_effect=[
        _scalar_one_or_none_result(approval_row),
        MagicMock(),
        MagicMock(),
    ])
    mock_db_session.commit = AsyncMock()

    resp = await client.post(
        f"/api/orders/approvals/{request_id}/respond",
        json={"action": "reject"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_approval_respond_modify(client, mock_db_session):
    """POST /api/orders/approvals/{id}/respond — 수량 변경."""
    request_id = uuid.uuid4()
    approval_row = _mock_approval_row(request_id=request_id, status="pending")
    mock_db_session.execute = AsyncMock(side_effect=[
        _scalar_one_or_none_result(approval_row),
        MagicMock(),
        MagicMock(),
    ])
    mock_db_session.commit = AsyncMock()

    resp = await client.post(
        f"/api/orders/approvals/{request_id}/respond",
        json={"action": "modify", "modified_quantity": 5},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["status"] == "approved"


@pytest.mark.asyncio
async def test_approval_respond_not_found(client, mock_db_session):
    """POST /api/orders/approvals/{id}/respond — 404."""
    request_id = uuid.uuid4()
    mock_db_session.execute = AsyncMock(return_value=_scalar_one_or_none_result(None))

    resp = await client.post(
        f"/api/orders/approvals/{request_id}/respond",
        json={"action": "approve"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_approval_respond_already_responded(client, mock_db_session):
    """POST /api/orders/approvals/{id}/respond — 이미 응답됨 → 400."""
    request_id = uuid.uuid4()
    approval_row = _mock_approval_row(request_id=request_id, status="approved")
    mock_db_session.execute = AsyncMock(
        return_value=_scalar_one_or_none_result(approval_row)
    )

    resp = await client.post(
        f"/api/orders/approvals/{request_id}/respond",
        json={"action": "approve"},
    )
    assert resp.status_code == 400
    assert "already responded" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_approval_respond_modify_no_quantity(client, mock_db_session):
    """POST /api/orders/approvals/{id}/respond — modify without quantity → 400."""
    request_id = uuid.uuid4()
    approval_row = _mock_approval_row(request_id=request_id, status="pending")
    mock_db_session.execute = AsyncMock(
        return_value=_scalar_one_or_none_result(approval_row)
    )

    resp = await client.post(
        f"/api/orders/approvals/{request_id}/respond",
        json={"action": "modify"},
    )
    assert resp.status_code == 400
    assert "modified_quantity" in resp.json()["detail"]
