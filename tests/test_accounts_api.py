"""Unit tests for Account CRUD API + existing route account_id filtering.

Uses httpx AsyncClient with FastAPI test transport.
DB session is mocked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.db.session import get_db_session


# ── Helpers ───────────────────────────────────────────────────────────


def _mock_account(**kwargs) -> MagicMock:
    """Create a mock Account ORM row."""
    row = MagicMock()
    row.id = kwargs.get("id", "test-acct")
    row.nickname = kwargs.get("nickname", "테스트계좌")
    row.kis_app_key_enc = kwargs.get("kis_app_key_enc", "enc_key_xxx")
    row.kis_app_secret_enc = kwargs.get("kis_app_secret_enc", "enc_secret_xxx")
    row.kis_account_no = kwargs.get("kis_account_no", "12345678")
    row.kis_account_prod = kwargs.get("kis_account_prod", "01")
    row.kis_is_paper = kwargs.get("kis_is_paper", True)
    row.kis_hts_id = kwargs.get("kis_hts_id", "")
    row.strategy_type = kwargs.get("strategy_type", "swing")
    row.investment_prompt = kwargs.get("investment_prompt", "")
    row.risk_overrides = kwargs.get("risk_overrides", None)
    row.is_active = kwargs.get("is_active", True)
    row.created_at = kwargs.get("created_at", datetime(2026, 3, 25, tzinfo=UTC))
    row.updated_at = kwargs.get("updated_at", datetime(2026, 3, 25, tzinfo=UTC))
    return row


def _mock_order(**kwargs) -> MagicMock:
    """Create a mock Order ORM row."""
    row = MagicMock()
    row.id = kwargs.get("id", 1)
    row.symbol = kwargs.get("symbol", "005930")
    row.side = kwargs.get("side", "buy")
    row.order_type = kwargs.get("order_type", "limit")
    row.quantity = kwargs.get("quantity", 10)
    row.price = kwargs.get("price", 78000)
    row.status = kwargs.get("status", "filled")
    row.account_id = kwargs.get("account_id", "default")
    row.filled_quantity = kwargs.get("filled_quantity", 10)
    row.filled_price = kwargs.get("filled_price", 78000)
    row.executed_at = kwargs.get("executed_at", datetime(2026, 3, 25, tzinfo=UTC))
    row.created_at = kwargs.get("created_at", datetime(2026, 3, 25, tzinfo=UTC))
    row.position_id = kwargs.get("position_id", None)
    row.session_id = kwargs.get("session_id", None)
    row.strategy_type = kwargs.get("strategy_type", "swing")
    row.approval_status = kwargs.get("approval_status", None)
    row.modified_quantity = kwargs.get("modified_quantity", None)
    row.broker_order_no = kwargs.get("broker_order_no", None)
    row.error_message = kwargs.get("error_message", None)
    return row


@pytest.fixture
def mock_session():
    """Create an async mock session."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture
def client(mock_session):
    """HTTP client with mocked DB session."""
    from src.main import app

    app.dependency_overrides[get_db_session] = lambda: mock_session
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.clear()


# ── Account CRUD Tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_accounts(client, mock_session):
    """GET /api/accounts returns account list."""
    acct1 = _mock_account(id="acct-1", nickname="공격형")
    acct2 = _mock_account(id="acct-2", nickname="안정형", is_active=False)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [acct1, acct2]
    mock_session.execute.return_value = mock_result

    resp = await client.get("/api/accounts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["items"][0]["id"] == "acct-1"
    assert data["items"][1]["is_active"] is False


@pytest.mark.asyncio
async def test_list_accounts_filter_active(client, mock_session):
    """GET /api/accounts?is_active=true filters correctly."""
    acct = _mock_account(id="acct-1", is_active=True)
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [acct]
    mock_session.execute.return_value = mock_result

    resp = await client.get("/api/accounts?is_active=true")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_create_account_success(client, mock_session):
    """POST /api/accounts creates account with encrypted KIS credentials."""
    # No existing account
    mock_no_existing = MagicMock()
    mock_no_existing.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_no_existing

    # After refresh, return mock account
    created = _mock_account(id="test", nickname="Test", kis_account_no="99998888")
    mock_session.refresh.side_effect = lambda a: None

    with (
        patch("src.api.routes.accounts.get_settings") as mock_settings,
        patch("src.api.routes.accounts.AccountCrypto") as mock_crypto,
    ):
        mock_settings.return_value.ACCOUNT_ENCRYPTION_KEY = "test-key-32-bytes-for-fernet!=="
        mock_crypto.encrypt.return_value = "encrypted_value"

        resp = await client.post("/api/accounts", json={
            "nickname": "Test",
            "kis_app_key": "my_key",
            "kis_app_secret": "my_secret",
            "kis_account_no": "99998888",
            "strategy_type": "swing",
        })

    assert resp.status_code == 201
    # Verify encrypt was called for both key and secret
    assert mock_crypto.encrypt.call_count == 2


@pytest.mark.asyncio
async def test_create_account_encryption_key_missing(client, mock_session):
    """POST /api/accounts fails with 422 when ACCOUNT_ENCRYPTION_KEY is empty."""
    with patch("src.api.routes.accounts.get_settings") as mock_settings:
        mock_settings.return_value.ACCOUNT_ENCRYPTION_KEY = ""

        resp = await client.post("/api/accounts", json={
            "nickname": "Test",
            "kis_app_key": "my_key",
            "kis_app_secret": "my_secret",
            "kis_account_no": "99998888",
        })

    assert resp.status_code == 422
    assert "ACCOUNT_ENCRYPTION_KEY" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_account_duplicate(client, mock_session):
    """POST /api/accounts returns 409 for duplicate ID."""
    mock_existing = MagicMock()
    mock_existing.scalar_one_or_none.return_value = _mock_account(id="dup")
    mock_session.execute.return_value = mock_existing

    with patch("src.api.routes.accounts.get_settings") as mock_settings:
        mock_settings.return_value.ACCOUNT_ENCRYPTION_KEY = "test-key-32-bytes-for-fernet!=="

        resp = await client.post("/api/accounts", json={
            "id": "dup",
            "nickname": "Dup",
            "kis_app_key": "k",
            "kis_app_secret": "s",
            "kis_account_no": "12345678",
        })

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_get_account_detail(client, mock_session):
    """GET /api/accounts/{id} returns detail without KIS secrets."""
    acct = _mock_account(
        id="acct-1",
        nickname="공격형",
        kis_account_no="12345678",
        investment_prompt="가치투자 중심",
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = acct
    mock_session.execute.return_value = mock_result

    resp = await client.get("/api/accounts/acct-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "acct-1"
    assert data["investment_prompt"] == "가치투자 중심"
    # KIS secrets must NOT be exposed
    assert "kis_app_key_enc" not in data
    assert "kis_app_secret_enc" not in data
    assert "kis_app_key" not in data
    assert "kis_app_secret" not in data


@pytest.mark.asyncio
async def test_get_account_not_found(client, mock_session):
    """GET /api/accounts/{nonexistent} returns 404."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result

    resp = await client.get("/api/accounts/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_account(client, mock_session):
    """PUT /api/accounts/{id} partial update."""
    acct = _mock_account(id="acct-1", nickname="Old Name")
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = acct
    mock_session.execute.return_value = mock_result

    resp = await client.put("/api/accounts/acct-1", json={"nickname": "New Name"})
    assert resp.status_code == 200
    assert acct.nickname == "New Name"


@pytest.mark.asyncio
async def test_update_prompt(client, mock_session):
    """PUT /api/accounts/{id}/prompt updates investment_prompt."""
    acct = _mock_account(id="acct-1", investment_prompt="")
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = acct
    mock_session.execute.return_value = mock_result

    resp = await client.put(
        "/api/accounts/acct-1/prompt",
        json={"investment_prompt": "모멘텀 투자"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["investment_prompt"] == "모멘텀 투자"
    assert acct.investment_prompt == "모멘텀 투자"


@pytest.mark.asyncio
async def test_delete_account_soft(client, mock_session):
    """DELETE /api/accounts/{id} sets is_active=False (soft delete)."""
    acct = _mock_account(id="acct-1", is_active=True)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = acct
    mock_session.execute.return_value = mock_result

    resp = await client.delete("/api/accounts/acct-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["is_active"] is False
    assert acct.is_active is False


@pytest.mark.asyncio
async def test_account_no_masked():
    """Account number masking: '12345678' → '****5678'."""
    from src.api.routes.accounts import _mask_account_no

    assert _mask_account_no("12345678") == "****5678"
    assert _mask_account_no("1234") == "1234"
    assert _mask_account_no("123") == "123"
    assert _mask_account_no("5550012345") == "******2345"


# ── Existing API account_id Filter Tests ─────────────────────────────


@pytest.mark.asyncio
async def test_orders_account_id_filter(client, mock_session):
    """GET /api/orders?account_id=acct-1 passes account_id filter."""
    # Setup: total count + rows
    mock_count = MagicMock()
    mock_count.scalar_one.return_value = 0
    mock_rows = MagicMock()
    mock_rows.scalars.return_value.all.return_value = []
    mock_session.execute.side_effect = [mock_count, mock_rows]

    resp = await client.get("/api/orders?account_id=acct-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_decisions_account_id_filter(client, mock_session):
    """GET /api/decisions?account_id=acct-1 passes account_id filter."""
    # Setup: count + rows
    mock_count = MagicMock()
    mock_count.scalar_one.return_value = 0
    mock_rows = MagicMock()
    mock_rows.scalars.return_value.all.return_value = []
    mock_session.execute.side_effect = [mock_count, mock_rows]

    resp = await client.get("/api/decisions?account_id=acct-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []
