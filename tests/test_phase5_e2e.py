"""Phase 5 E2E 테스트 — 실제 인프라 (Docker DB + Redis + Telegram + LLM).

Prerequisites:
    - Docker: docker compose up -d (PostgreSQL 16 + Redis 7)
    - Migrations: uv run alembic upgrade head
    - .env: OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, USE_MOCK_BROKER=true

Run:
    uv run pytest tests/test_phase5_e2e.py -v -s --timeout=120

Skip in CI:
    uv run pytest -m "not e2e"
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from httpx import ASGITransport

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio(loop_scope="module")]


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def env_override():
    """E2E 환경변수 설정 — 모듈 시작 전에 적용."""
    overrides = {
        "USE_MOCK_BROKER": "true",
    }
    original = {}
    for key, value in overrides.items():
        original[key] = os.environ.get(key)
        os.environ[key] = value

    # settings 캐시 클리어
    from src.config import get_settings

    get_settings.cache_clear()
    yield
    # 복원
    for key, orig_val in original.items():
        if orig_val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig_val
    get_settings.cache_clear()


@pytest.fixture(scope="module")
async def client(env_override):
    """httpx AsyncClient — lifespan 수동 관리 + ASGITransport."""
    from src.main import app, lifespan

    # ASGITransport는 lifespan을 실행하지 않으므로 수동으로 관리
    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            timeout=90.0,
        ) as c:
            yield c


@pytest.fixture(scope="module")
async def seed_agent_config(client):
    """agent_model_config에 web_verifier 시드 데이터 추가.

    client fixture에 의존하여 lifespan(DB init)이 완료된 후 실행.
    """
    from sqlalchemy import select

    from src.db.models.llm import AgentModelConfigDB
    from src.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        existing = (
            await session.execute(
                select(AgentModelConfigDB).where(
                    AgentModelConfigDB.agent_type == "web_verifier"
                )
            )
        ).scalar_one_or_none()

        if not existing:
            config = AgentModelConfigDB(
                agent_type="web_verifier",
                routing_mode="fixed",
                primary_model="openai/gpt-4o",
                is_active=True,
                updated_by="e2e_seed",
            )
            session.add(config)
            await session.commit()
    yield


# ── Scenario 1: Health Check ────────────────────────────────────────────


async def test_health_check(client):
    """서버 기동 + DB/Redis 연결 확인."""
    resp = await client.get("/health")
    assert resp.status_code == 200, f"Health check failed: {resp.text}"

    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "connected"
    assert body["redis"] == "connected"


# ── Scenario 2: Telegram Bot Connectivity ────────────────────────────────


async def test_telegram_bot_connected(client):
    """Telegram 봇이 lifespan에서 정상 연결되었는지 확인."""
    from src.main import get_telegram_bot

    bot = get_telegram_bot()
    assert bot.is_running, "Telegram bot polling is not running"

    # 테스트 메시지 전송
    msg_id = await bot.send_message(
        "🧪 <b>E2E Test</b>: Bot connectivity check ✅"
    )
    assert msg_id is not None, "Failed to send Telegram message"


# ── Scenario 3: Entry Order (Auto-Approved) ──────────────────────────────


async def test_entry_order_auto_approved(client, seed_agent_config):
    """자동 승인 진입 주문 — 실제 LLM WebSearch + InMemoryBroker 체결.

    HUMAN_APPROVAL_REQUIRED를 일시적으로 False로 설정하여 자동 승인 경로 검증.
    """
    from src.config import get_settings

    settings = get_settings()
    original_approval = settings.HUMAN_APPROVAL_REQUIRED
    settings.HUMAN_APPROVAL_REQUIRED = False

    try:
        resp = await client.post(
            "/api/orders/execute",
            json={
                "symbol": "005930",
                "side": "buy",
                "order_type": "limit",
                "quantity": 10,
                "price": 72000,
                "stop_loss_price": 68000,
                "take_profit_price": 80000,
                "strategy_type": "swing",
            },
        )
        body = resp.json()

        assert resp.status_code == 200, f"Execute failed: {body}"
        assert body["success"] is True
        assert body["symbol"] == "005930"
        assert body["side"] == "buy"
        assert body["approval_status"] == "auto_approved"
        assert body["web_verify_result"] in ("safe", "warning")
        assert body["order_id"] is not None
        assert body["broker_order_id"] is not None
        assert body["position_id"] is not None

        # 주문 상세 조회로 DB 저장 확인
        detail_resp = await client.get(f"/api/orders/{body['order_id']}")
        assert detail_resp.status_code == 200

        detail = detail_resp.json()
        assert detail["order"]["status"] == "filled"
        assert detail["order"]["symbol"] == "005930"
        assert len(detail["executions"]) >= 1

    finally:
        settings.HUMAN_APPROVAL_REQUIRED = original_approval


# ── Scenario 4: Entry Order (Telegram Manual Approval) ───────────────────


async def test_entry_order_telegram_approval(client, seed_agent_config):
    """Telegram 수동 승인 — 실제 버튼 클릭 필요 (semi-automated).

    1. 주문 실행 요청 → Telegram에 승인 메시지 도착
    2. 사용자가 60초 내에 '승인 ✅' 버튼 클릭
    3. 체결 완료 확인

    NOTE: 이 테스트는 Telegram에서 실제 버튼 클릭이 필요합니다.
    """
    from src.config import get_settings

    settings = get_settings()
    original_approval = settings.HUMAN_APPROVAL_REQUIRED
    original_timeout = settings.HUMAN_APPROVAL_TIMEOUT_SEC
    settings.HUMAN_APPROVAL_REQUIRED = True
    settings.HUMAN_APPROVAL_TIMEOUT_SEC = 60

    try:
        # 안내 메시지
        from src.main import get_telegram_bot

        bot = get_telegram_bot()
        await bot.send_message(
            "🧪 <b>E2E Test</b>: 수동 승인 테스트 시작\n"
            "아래 승인 요청이 도착하면 <b>승인 ✅</b> 버튼을 눌러주세요."
        )

        print("\n" + "=" * 60)
        print("📱 Telegram에서 '승인 ✅' 버튼을 눌러주세요 (60초 제한)")
        print("=" * 60 + "\n")

        resp = await client.post(
            "/api/orders/execute",
            json={
                "symbol": "005930",
                "side": "buy",
                "order_type": "limit",
                "quantity": 5,
                "price": 72000,
                "stop_loss_price": 68000,
                "take_profit_price": 80000,
                "strategy_type": "swing",
            },
        )
        body = resp.json()

        # 승인 또는 타임아웃 — 사용자 개입에 의존
        if body.get("success"):
            assert body["approval_status"] == "approved"
            assert body["broker_order_id"] is not None
            print("✅ 수동 승인 + 체결 성공!")
        else:
            # 타임아웃인 경우도 테스트 통과 (semi-automated)
            assert body["approval_status"] in ("timeout", "rejected")
            print(f"⏰ 승인 {body['approval_status']} — 수동 테스트 미완료")

    finally:
        settings.HUMAN_APPROVAL_REQUIRED = original_approval
        settings.HUMAN_APPROVAL_TIMEOUT_SEC = original_timeout


# ── Scenario 5: Approval Timeout ────────────────────────────────────────


async def test_approval_timeout(client, seed_agent_config):
    """짧은 타임아웃 → 승인 시간 초과 → 주문 취소."""
    from src.config import get_settings

    settings = get_settings()
    original_approval = settings.HUMAN_APPROVAL_REQUIRED
    original_timeout = settings.HUMAN_APPROVAL_TIMEOUT_SEC
    settings.HUMAN_APPROVAL_REQUIRED = True
    settings.HUMAN_APPROVAL_TIMEOUT_SEC = 3  # 3초 타임아웃

    try:
        resp = await client.post(
            "/api/orders/execute",
            json={
                "symbol": "005930",
                "side": "buy",
                "order_type": "limit",
                "quantity": 2,
                "price": 72000,
                "strategy_type": "swing",
            },
        )
        body = resp.json()

        # 3초 후 타임아웃 → 실패
        assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {body}"
        assert body["success"] is False
        assert body["approval_status"] == "timeout"

    finally:
        settings.HUMAN_APPROVAL_REQUIRED = original_approval
        settings.HUMAN_APPROVAL_TIMEOUT_SEC = original_timeout


# ── Scenario 6: Order Listing & Detail ───────────────────────────────────


async def test_order_listing(client, seed_agent_config):
    """주문 목록 조회 — 이전 테스트에서 생성된 주문 확인."""
    resp = await client.get("/api/orders")
    assert resp.status_code == 200

    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert body["total"] >= 1, "이전 테스트에서 생성된 주문이 있어야 함"

    # 심볼 필터
    resp_filtered = await client.get("/api/orders", params={"symbol": "005930"})
    assert resp_filtered.status_code == 200

    filtered = resp_filtered.json()
    for item in filtered["items"]:
        assert item["symbol"] == "005930"

    # 첫 번째 주문 상세 조회
    if filtered["items"]:
        order_id = filtered["items"][0]["id"]
        detail_resp = await client.get(f"/api/orders/{order_id}")
        assert detail_resp.status_code == 200

        detail = detail_resp.json()
        assert "order" in detail
        assert "executions" in detail
        assert "approval" in detail
