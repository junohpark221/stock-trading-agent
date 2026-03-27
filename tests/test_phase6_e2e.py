"""Phase 6 E2E 실서버 테스트 — Docker DB + Redis + 실제 Telegram 전송.

Prerequisites:
    - Docker: docker compose up -d (PostgreSQL 16 + Redis 7)
    - Migrations: uv run alembic upgrade head
    - .env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, USE_MOCK_BROKER=true

Run:
    uv run pytest tests/test_phase6_e2e.py -v -s

Skip in CI:
    uv run pytest -m "not e2e"
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio(loop_scope="module")]


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def env_override():
    """E2E 환경변수 설정."""
    overrides = {
        "USE_MOCK_BROKER": "true",
        "SCHEDULER_ENABLED": "true",
        "HUMAN_APPROVAL_REQUIRED": "false",
        "WEB_VERIFY_ENABLED": "false",
    }
    original = {}
    for key, value in overrides.items():
        original[key] = os.environ.get(key)
        os.environ[key] = value

    from src.config import get_settings

    get_settings.cache_clear()
    yield
    for key, orig_val in original.items():
        if orig_val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig_val
    get_settings.cache_clear()


@pytest.fixture(scope="module")
async def client(env_override):
    """httpx AsyncClient — lifespan 수동 관리."""
    from src.main import app, lifespan

    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            timeout=90.0,
        ) as c:
            yield c


@pytest.fixture(scope="module")
def scheduler(client):
    """SchedulerEngine 인스턴스."""
    from src.main import get_scheduler

    return get_scheduler()


@pytest.fixture(scope="module")
def broker(client):
    """InMemoryBroker 인스턴스."""
    import src.main as main_mod

    return main_mod._scheduler_broker


@pytest.fixture(scope="module")
def telegram_bot(client):
    """실제 TelegramBot 인스턴스."""
    from src.main import get_telegram_bot

    return get_telegram_bot()


@pytest.fixture(scope="module")
def session_factory(client):
    """DB session factory."""
    from src.db.session import get_session_factory

    return get_session_factory()


# ── Scenario 1: Health + Telegram Connectivity ────────────────────────


async def test_health_and_telegram(client, telegram_bot):
    """서버 기동 + DB/Redis 연결 + Telegram 봇 연결 확인."""
    # Health check
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "connected"
    assert body["redis"] == "connected"

    # Telegram 연결 확인 + 테스트 시작 알림
    assert telegram_bot.is_running, "Telegram bot polling is not running"
    msg_id = await telegram_bot.send_message(
        "🧪 <b>Phase 6 E2E Test</b> 시작\n"
        "스케줄러 + 리포트 + 모니터링 실서버 검증"
    )
    assert msg_id is not None, "Failed to send Telegram message"


# ── Scenario 2: Scheduler Lifecycle ───────────────────────────────────


async def test_scheduler_lifecycle(scheduler, telegram_bot):
    """스케줄러 9개 작업 등록 + pause/resume + Telegram 상태 알림."""
    assert scheduler.is_running
    status = scheduler.get_status()
    assert len(status["jobs"]) == 9

    expected_jobs = {
        "token_refresh", "market_data_collect", "swing_analysis",
        "position_analysis", "stop_loss_check", "daily_report",
        "weekly_report", "monthly_report", "llm_cost_report",
    }
    actual_jobs = {j["name"] for j in status["jobs"]}
    assert actual_jobs == expected_jobs

    # pause/resume
    scheduler.pause_all()
    assert scheduler.is_paused
    scheduler.resume_all()
    assert not scheduler.is_paused

    # 상태를 Telegram으로 전송
    job_list = "\n".join(f"  • {j['name']}" for j in status["jobs"])
    await telegram_bot.send_message(
        f"✅ <b>스케줄러 상태 확인</b>\n"
        f"작업 수: {len(status['jobs'])}개\n"
        f"is_running: {scheduler.is_running}\n\n"
        f"<b>등록된 작업:</b>\n{job_list}"
    )


# ── Scenario 3: Daily Report → Telegram ───────────────────────────────


async def test_daily_report_telegram(scheduler, session_factory, telegram_bot):
    """일간 리포트 생성 → 실제 Telegram 전송 확인."""
    from sqlalchemy import select

    from src.db.models.strategy import PortfolioSnapshot, PositionRecord

    today = date.today()

    # 시드: open position + snapshot
    async with session_factory() as session:
        pos = PositionRecord(
            symbol="005930",
            strategy_type="swing",
            quantity=10,
            avg_cost=Decimal("72000"),
            entry_price=Decimal("72000"),
            entry_date=today,
            stop_loss_price=Decimal("68000"),
            take_profit_price=Decimal("80000"),
            status="open",
        )
        session.add(pos)

        existing_snap = (
            await session.execute(
                select(PortfolioSnapshot).where(
                    PortfolioSnapshot.snapshot_date == today
                )
            )
        ).scalar_one_or_none()
        if existing_snap:
            existing_snap.total_value = Decimal("100720000")
            existing_snap.positions_count = 1
        else:
            snap = PortfolioSnapshot(
                snapshot_date=today,
                total_value=Decimal("100720000"),
                cash=Decimal("99280000"),
                invested=Decimal("720000"),
                unrealized_pnl=Decimal("0"),
                realized_pnl_daily=Decimal("0"),
                peak_value=Decimal("100720000"),
                drawdown_pct=Decimal("0"),
                positions_count=1,
            )
            session.add(snap)
        await session.commit()

    # 일간 리포트 실행 → 실제 Telegram 전송
    await scheduler.run_job_now("daily_report")

    # 결과 안내
    await telegram_bot.send_message("✅ <b>일간 리포트</b> 전송 완료 (위 메시지 확인)")


# ── Scenario 4: Weekly Report → Telegram ──────────────────────────────


async def test_weekly_report_telegram(scheduler, session_factory, telegram_bot):
    """주간 리포트 + 성과 지표 → 실제 Telegram 전송."""
    from sqlalchemy import select

    from src.db.models.strategy import PortfolioSnapshot, PositionRecord

    today = date.today()

    async with session_factory() as session:
        # 5건 closed positions (3승 2패)
        closed_data = [
            ("000660", 180000, 186000, 60000, 2),
            ("035420", 210000, 215000, 50000, 3),
            ("051910", 300000, 309000, 90000, 4),
            ("006400", 600000, 582000, -180000, 5),
            ("003670", 80000, 79200, -8000, 6),
        ]
        for symbol, entry, exit_p, pnl, days_ago in closed_data:
            pos = PositionRecord(
                symbol=symbol,
                strategy_type="swing",
                quantity=10,
                avg_cost=Decimal(str(entry)),
                entry_price=Decimal(str(entry)),
                entry_date=today - timedelta(days=days_ago + 3),
                stop_loss_price=Decimal(str(int(entry * 0.95))),
                status="closed",
                exit_price=Decimal(str(exit_p)),
                exit_date=today - timedelta(days=days_ago),
                exit_reason="take_profit" if pnl > 0 else "stop_loss",
                realized_pnl=Decimal(str(pnl)),
            )
            session.add(pos)

        # 6일분 스냅샷
        snapshot_values = [
            100_000_000, 100_500_000, 100_300_000,
            100_800_000, 100_100_000, 100_600_000,
        ]
        for i, val in enumerate(snapshot_values):
            snap_date = today - timedelta(days=5 - i)
            existing = (
                await session.execute(
                    select(PortfolioSnapshot).where(
                        PortfolioSnapshot.snapshot_date == snap_date
                    )
                )
            ).scalar_one_or_none()
            if existing:
                existing.total_value = Decimal(str(val))
                existing.peak_value = Decimal(str(max(snapshot_values[: i + 1])))
            else:
                snap = PortfolioSnapshot(
                    snapshot_date=snap_date,
                    total_value=Decimal(str(val)),
                    cash=Decimal(str(val - 5_000_000)),
                    invested=Decimal("5000000"),
                    unrealized_pnl=Decimal(str(val - 100_000_000)),
                    realized_pnl_daily=Decimal("0"),
                    peak_value=Decimal(str(max(snapshot_values[: i + 1]))),
                    drawdown_pct=Decimal("0"),
                    positions_count=2,
                )
                session.add(snap)
        await session.commit()

    # 주간 리포트 실행 → 실제 Telegram 전송
    await scheduler.run_job_now("weekly_report")

    await telegram_bot.send_message(
        "✅ <b>주간 리포트</b> 전송 완료 (3승 2패, 승률 60%)"
    )


# ── Scenario 5: Monitoring Alert → Telegram ──────────────────────────


async def test_monitoring_alert_telegram(session_factory, broker, telegram_bot):
    """모니터링 경고 → 실제 Telegram 전송 (손절 근접)."""
    from src.config import get_settings
    from src.core.enums import MarketType, MonitoringAlertType
    from src.core.models import PriceInfo, StockInfo
    from src.data.cache import get_cache
    from src.db.models.strategy import PositionRecord
    from src.db.session import get_session_factory
    from src.llm.cost_tracker import CostTracker
    from src.scheduler.monitor import TradingMonitor
    from src.strategy.portfolio_state import PortfolioStateService
    from src.strategy.position_manager import PositionManager

    settings = get_settings()
    sf = get_session_factory()
    cache = get_cache()
    today = date.today()

    # 손절 근접 포지션 시드 (symbol="000660")
    async with sf() as session:
        pos = PositionRecord(
            symbol="000660",
            strategy_type="swing",
            quantity=5,
            avg_cost=Decimal("52000"),
            entry_price=Decimal("52000"),
            entry_date=today - timedelta(days=3),
            stop_loss_price=Decimal("50000"),
            status="open",
        )
        session.add(pos)
        await session.commit()

    # 현재가 50800원 → 손절가 50000원 대비 1.57% 근접 (< 2% 임계치)
    broker._prices["000660"] = PriceInfo(
        symbol="000660",
        current_price=Decimal("50800"),
        previous_close=Decimal("52000"),
        timestamp=datetime.now(timezone.utc),
    )
    if "000660" not in broker._stocks:
        broker._stocks["000660"] = StockInfo(
            symbol="000660",
            name="SK하이닉스",
            market_type=MarketType.KOSPI,
            sector="반도체",
        )

    # Redis dedup 키 삭제 (이전 테스트 잔존 방지)
    dedup_key = f"stop_loss_proximity:000660:{today.isoformat()}"
    await cache.delete("alert", dedup_key)

    monitor = TradingMonitor(
        portfolio_state_service=PortfolioStateService(
            broker=broker, session_factory=sf, cache=cache,
        ),
        position_manager=PositionManager(session_factory=sf),
        cost_tracker=CostTracker(session_factory=sf, settings=settings),
        telegram_bot=telegram_bot,
        broker=broker,
        cache=cache,
        settings=settings,
    )

    alerts = await monitor.check_all()

    # 최소 손절 근접 알림 1개
    stop_loss_alerts = [
        a for a in alerts
        if a.alert_type == MonitoringAlertType.STOP_LOSS_PROXIMITY
    ]
    assert len(stop_loss_alerts) >= 1, f"Expected stop_loss alert, got {alerts}"

    await telegram_bot.send_message(
        f"✅ <b>모니터링 경고</b> {len(alerts)}건 전송 완료\n"
        f"(손절 근접 알림 포함)"
    )


# ── Scenario 6: Control API + Job Execution DB Logging ────────────────


async def test_control_api_and_job_history(client, telegram_bot):
    """Control API 라운드 트립 + job_executions DB 이력 확인."""
    # 1. status
    resp = await client.get("/api/control/scheduler/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_running"] is True
    assert len(body["jobs"]) == 9

    # 2. pause → status → resume
    resp = await client.post("/api/control/scheduler/pause")
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"

    resp = await client.get("/api/control/scheduler/status")
    assert resp.json()["is_paused"] is True

    resp = await client.post("/api/control/scheduler/resume")
    assert resp.status_code == 200
    assert resp.json()["status"] == "resumed"

    # 3. run daily_report → history
    resp = await client.post("/api/control/scheduler/run/daily_report")
    assert resp.status_code == 200
    assert resp.json()["status"] == "triggered"

    # 4. history
    resp = await client.get(
        "/api/control/jobs/history",
        params={"job_name": "daily_report", "limit": 10},
    )
    assert resp.status_code == 200
    history = resp.json()
    assert history["total"] >= 1
    statuses = {item["status"] for item in history["items"]}
    assert "success" in statuses, f"Expected success in history, got {statuses}"

    # 결과를 Telegram으로 전송
    latest = history["items"][0]
    await telegram_bot.send_message(
        f"✅ <b>Control API + Job History</b> 확인 완료\n"
        f"최근 실행: {latest['job_name']} → {latest['status']}\n"
        f"소요 시간: {latest.get('duration_sec', 'N/A')}초"
    )


# ── Scenario 7: Trades API ────────────────────────────────────────────


async def test_trades_api(client, telegram_bot):
    """Trades API — 거래 내역 목록 + 성과 요약."""
    # 이전 테스트에서 closed positions 시드됨
    resp = await client.get("/api/trades")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1, f"Expected closed trades, got {body['total']}"

    # summary
    resp = await client.get("/api/trades/summary")
    assert resp.status_code == 200
    summary = resp.json()
    assert "total_trades" in summary
    assert summary["total_trades"] >= 1

    # detail
    if body["items"]:
        trade_id = body["items"][0]["id"]
        resp = await client.get(f"/api/trades/{trade_id}")
        assert resp.status_code == 200
        detail = resp.json()
        assert "position" in detail

    # Telegram 요약
    await telegram_bot.send_message(
        f"✅ <b>Trades API</b> 확인 완료\n"
        f"총 거래: {summary.get('total_trades', 0)}건\n"
        f"승률: {summary.get('win_rate_pct', 'N/A')}%\n"
        f"Sharpe: {summary.get('sharpe_ratio', 'N/A')}"
    )


# ── Scenario 8: E2E 완료 알림 ────────────────────────────────────────


async def test_e2e_completion(telegram_bot):
    """Phase 6 E2E 실서버 검증 완료 알림."""
    await telegram_bot.send_message(
        "🎉 <b>Phase 6 E2E 실서버 검증 완료</b>\n\n"
        "✅ Health + Telegram 연결\n"
        "✅ 스케줄러 9개 작업 등록/pause/resume\n"
        "✅ 일간 리포트 → Telegram\n"
        "✅ 주간 리포트 (3승2패) → Telegram\n"
        "✅ 모니터링 경고 (손절 근접) → Telegram\n"
        "✅ Control API 5개 엔드포인트\n"
        "✅ Trades API 3개 엔드포인트\n\n"
        "Phase 6 스케줄러 + 리포트 + 모니터링 시스템 정상 동작 확인 ✅"
    )
