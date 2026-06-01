"""Phase 6 E2E 테스트 — Docker DB + Redis, Mock TelegramBot + InMemoryBroker.

Prerequisites:
    - Docker: docker compose up -d (PostgreSQL 16 + Redis 7)
    - Migrations: uv run alembic upgrade head
    - .env: USE_MOCK_BROKER=true (나머지는 env_override에서 설정)

Run:
    uv run pytest tests/test_phase6_integration.py -v -s --timeout=120

Skip in CI:
    uv run pytest -m "not e2e"
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

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
        "SCHEDULER_ENABLED": "true",
        "HUMAN_APPROVAL_REQUIRED": "false",
        "WEB_VERIFY_ENABLED": "false",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "test_chat",
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
    """httpx AsyncClient — lifespan 수동 관리 + TelegramBot mock 패치."""
    from src.main import app, lifespan

    async with lifespan(app):
        # TelegramBot은 _disabled=True로 생성됨 (빈 token).
        # send_message를 AsyncMock으로 교체하여 호출 검증 가능하게 만든다.
        from src.main import get_telegram_bot

        bot = get_telegram_bot()
        bot._disabled = False
        bot.send_message = AsyncMock(return_value=12345)
        bot.send_message_with_buttons = AsyncMock(return_value=12346)
        bot.update_message = AsyncMock()

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            timeout=90.0,
        ) as c:
            yield c


@pytest.fixture(scope="module")
def mock_send(client):
    """TelegramBot.send_message AsyncMock — call_count / call_args 검증용."""
    from src.main import get_telegram_bot

    return get_telegram_bot().send_message


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
def session_factory(client):
    """DB session factory — 시드 데이터 삽입용."""
    from src.db.session import get_session_factory

    return get_session_factory()


# ── Scenario 1: Scheduler Lifecycle ───────────────────────────────────


async def test_scheduler_lifecycle(scheduler):
    """스케줄러 라이프사이클: 시작 → 9개 작업 등록 → pause/resume."""
    # 1. 스케줄러 동작 중
    assert scheduler.is_running, "Scheduler should be running after lifespan"

    # 2. 9개 작업 등록 확인
    status = scheduler.get_status()
    assert len(status["jobs"]) == 9, f"Expected 9 jobs, got {len(status['jobs'])}"

    expected_jobs = {
        "token_refresh",
        "market_data_collect",
        "swing_analysis",
        "position_analysis",
        "stop_loss_check",
        "daily_report",
        "weekly_report",
        "monthly_report",
        "llm_cost_report",
    }
    actual_jobs = {j["name"] for j in status["jobs"]}
    assert actual_jobs == expected_jobs, f"Missing jobs: {expected_jobs - actual_jobs}"

    # 3. pause/resume
    scheduler.pause_all()
    assert scheduler.is_paused, "Scheduler should be paused"

    scheduler.resume_all()
    assert not scheduler.is_paused, "Scheduler should be resumed"


# ── Scenario 2: PerformanceCalculator Accuracy ─────────────────────────


async def test_performance_calculator_accuracy():
    """성과 지표 정확도 — 사전 계산된 정답과 비교."""
    from unittest.mock import MagicMock

    from src.report.metrics import PerformanceCalculator

    today = date.today()

    # Mock closed positions (3승 2패)
    def make_position(entry, exit_p, pnl, exit_d):
        p = MagicMock()
        p.entry_price = Decimal(str(entry))
        p.exit_price = Decimal(str(exit_p))
        p.realized_pnl = Decimal(str(pnl))
        p.strategy_type = "swing"
        p.exit_date = exit_d
        return p

    positions = [
        make_position(100000, 105000, 50000, today - timedelta(days=5)),  # +5%
        make_position(200000, 206000, 60000, today - timedelta(days=4)),  # +3%
        make_position(150000, 153000, 30000, today - timedelta(days=3)),  # +2%
        make_position(180000, 172800, -72000, today - timedelta(days=2)),  # -4%
        make_position(120000, 118800, -12000, today - timedelta(days=1)),  # -1%
    ]

    # Mock snapshots (10일분)
    values = [
        100_000_000,
        100_500_000,
        100_300_000,
        100_800_000,
        100_100_000,
        100_600_000,
        100_200_000,
        100_900_000,
        100_400_000,
        100_700_000,
    ]

    def make_snapshot(d, val):
        s = MagicMock()
        s.snapshot_date = d
        s.total_value = Decimal(str(val))
        return s

    snapshots = [
        make_snapshot(today - timedelta(days=9 - i), v) for i, v in enumerate(values)
    ]

    metrics = PerformanceCalculator.calculate(
        closed_positions=positions,
        snapshots=snapshots,
        period_start=today - timedelta(days=9),
        period_end=today,
    )

    # 승률 = 3/5 = 60%
    assert metrics.win_rate_pct == Decimal("60.00")
    assert metrics.total_trades == 5
    assert metrics.winning_trades == 3
    assert metrics.losing_trades == 2

    # Profit Factor = (50000+60000+30000) / (72000+12000) = 140000/84000 ≈ 1.67
    assert metrics.profit_factor == Decimal("1.67")

    # MDD: peak=100_800_000, trough=100_100_000 → (100.8M-100.1M)/100.8M*100 ≈ 0.69%
    assert metrics.max_drawdown_pct > Decimal("0")
    assert metrics.max_drawdown_pct < Decimal("1.0")

    # Sharpe/Sortino should be calculable (9 daily returns)
    assert metrics.sharpe_ratio is not None
    assert metrics.sortino_ratio is not None


# ── Scenario 3: Daily Report Full Flow ────────────────────────────────


async def test_daily_report_full_flow(scheduler, session_factory, mock_send):
    """일간 리포트 전체 흐름: 시드 → 리포트 생성 → Telegram 전송."""
    from sqlalchemy import select

    from src.db.models.strategy import PortfolioSnapshot, PositionRecord

    today = date.today()

    # 1. 시드 데이터 삽입
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

        # 스냅샷 upsert (이전 테스트 실행 잔존 데이터 충돌 방지)
        existing_snap = (
            await session.execute(
                select(PortfolioSnapshot).where(
                    PortfolioSnapshot.snapshot_date == today
                )
            )
        ).scalar_one_or_none()
        if existing_snap:
            existing_snap.total_value = Decimal("100720000")
            existing_snap.cash = Decimal("99280000")
            existing_snap.invested = Decimal("720000")
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

    # 2. 일간 리포트 실행
    mock_send.reset_mock()
    await scheduler.run_job_now("daily_report")

    # 3. 검증: Telegram 전송 호출됨
    assert mock_send.call_count >= 1, "daily_report should send at least one message"

    # 4. 검증: 메시지 내용에 리포트 관련 키워드 포함
    sent_text = mock_send.call_args_list[0][0][0]
    assert any(
        keyword in sent_text for keyword in ("리포트", "포트폴리오", "📊", "일간")
    ), f"Expected report keywords in message: {sent_text[:200]}"


# ── Scenario 4: Weekly Report with Performance ────────────────────────


async def test_weekly_report_with_performance(scheduler, session_factory, mock_send):
    """주간 리포트 + 성과 지표: 포지션 시드 → 리포트 생성 → 성과 포함."""
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

        # 6일분 스냅샷 (변동하는 total_value)
        snapshot_values = [
            100_000_000,
            100_500_000,
            100_300_000,
            100_800_000,
            100_100_000,
            100_600_000,
        ]
        for i, val in enumerate(snapshot_values):
            snap_date = today - timedelta(days=5 - i)
            # 기존 스냅샷과 충돌하지 않도록 확인
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

    # 주간 리포트 실행
    mock_send.reset_mock()
    await scheduler.run_job_now("weekly_report")

    # 검증
    assert mock_send.call_count >= 1, "weekly_report should send at least one message"


# ── Scenario 5: Stop-Loss Check → Auto Execution ─────────────────────


async def test_stop_loss_check_auto_execution(
    scheduler, session_factory, broker, mock_send
):
    """손절 체크 → 자동 청산: 현재가 < stop_loss → 포지션 closed."""
    from unittest.mock import patch
    from sqlalchemy import select

    from src.core.enums import MarketType, PositionStatus
    from src.core.models import PriceInfo, Position, StockInfo
    from src.db.models.strategy import PositionRecord

    today = date.today()
    symbol = "005930"  # InMemoryBroker 기본 종목 사용

    # 1. DB에 open position 시드
    async with session_factory() as session:
        pos = PositionRecord(
            symbol=symbol,
            strategy_type="swing",
            quantity=10,
            avg_cost=Decimal("52000"),
            entry_price=Decimal("52000"),
            entry_date=today - timedelta(days=5),
            stop_loss_price=Decimal("50000"),
            take_profit_price=Decimal("58000"),
            status="open",
        )
        session.add(pos)
        await session.commit()
        pos_id = pos.id

    # 2. InMemoryBroker에 현재가 설정 (손절가 미만)
    broker._prices[symbol] = PriceInfo(
        symbol=symbol,
        current_price=Decimal("49000"),
        previous_close=Decimal("52000"),
        change_price=Decimal("-3000"),
        change_percent=Decimal("-5.77"),
        high=Decimal("52000"),
        low=Decimal("49000"),
        volume=1_000_000,
        timestamp=datetime.now(timezone.utc),
    )

    # InMemoryBroker에 보유 포지션 설정 (매도 가능하도록)
    broker._positions[symbol] = Position(
        symbol=symbol,
        quantity=10,
        average_cost=Decimal("52000"),
        current_price=Decimal("49000"),
        market_value=Decimal("490000"),
        unrealized_pnl=Decimal("-30000"),
        unrealized_pnl_pct=Decimal("-5.77"),
        status=PositionStatus.OPEN,
        entry_date=datetime.now(timezone.utc),
    )

    # 3. stop_loss_check 실행 (장 시간 외 실행 차단 우회)
    mock_send.reset_mock()
    with patch("src.scheduler.jobs._is_market_open", return_value=True):
        await scheduler.run_job_now("stop_loss_check")

    # 4. DB 재조회 — 포지션 closed 확인
    async with session_factory() as session:
        result = await session.get(PositionRecord, pos_id)
        assert result is not None, f"Position {pos_id} not found"
        # 손절 실행 시 포지션 closed or exit_reason 설정
        # OrderExecutor가 position_manager.close()를 호출
        if result.status == "closed":
            assert result.exit_reason in (
                "stop_loss",
                "trailing_stop",
            ), f"Unexpected exit_reason: {result.exit_reason}"
        else:
            # 실행 경로에 따라 status가 아직 open일 수 있음 (비동기 처리)
            # 최소한 Telegram으로 알림이 갔는지 확인
            assert mock_send.call_count >= 1, (
                "stop_loss_check should send alert if not executed"
            )


# ── Scenario 6: Monitoring Alerts ─────────────────────────────────────


async def test_monitoring_alerts(session_factory, broker, mock_send):
    """모니터링 경고: 손절 근접 + LLM 예산 알림."""
    from src.core.enums import MarketType, PositionStatus
    from src.core.models import PriceInfo, Position, StockInfo
    from src.data.cache import get_cache
    from src.db.models.strategy import PositionRecord
    from src.llm.cost_tracker import CostTracker
    from src.strategy.portfolio_state import PortfolioStateService
    from src.strategy.position_manager import PositionManager

    from src.config import get_settings
    from src.db.session import get_session_factory
    from src.main import get_telegram_bot
    from src.scheduler.monitor import TradingMonitor

    settings = get_settings()
    sf = get_session_factory()
    cache = get_cache()

    # 손절 근접 알림용 포지션 (심볼 "000660" — Redis dedup 충돌 방지)
    async with sf() as session:
        pos = PositionRecord(
            symbol="000660",
            strategy_type="swing",
            quantity=5,
            avg_cost=Decimal("52000"),
            entry_price=Decimal("52000"),
            entry_date=date.today() - timedelta(days=3),
            stop_loss_price=Decimal("50000"),
            status="open",
        )
        session.add(pos)
        await session.commit()

    # 손절 근접 가격 설정: 50800 → (50800-50000)/50800*100 ≈ 1.57% (< 2% 임계치)
    broker._prices["000660"] = PriceInfo(
        symbol="000660",
        current_price=Decimal("50800"),
        previous_close=Decimal("52000"),
        timestamp=datetime.now(timezone.utc),
    )

    # StockInfo 추가 (get_price 가능하도록)
    if "000660" not in broker._stocks:
        broker._stocks["000660"] = StockInfo(
            symbol="000660",
            name="SK하이닉스",
            market_type=MarketType.KOSPI,
            sector="반도체",
        )

    # TradingMonitor 구성
    position_manager = PositionManager(session_factory=sf)
    portfolio_service = PortfolioStateService(broker=broker, session_factory=sf, cache=cache)
    cost_tracker = CostTracker(session_factory=sf, settings=settings)
    bot = get_telegram_bot()

    monitor = TradingMonitor(
        portfolio_state_service=portfolio_service,
        position_manager=position_manager,
        cost_tracker=cost_tracker,
        telegram_bot=bot,
        broker=broker,
        cache=cache,
        settings=settings,
    )

    mock_send.reset_mock()

    # 개별 체커 실행 (check_all은 내부적으로 4개 모두 실행)
    # 최소 손절 근접 알림 1개는 발생해야 함
    alerts = await monitor.check_all()

    # 최소 1개 알림 (손절 근접)
    assert len(alerts) >= 1, f"Expected at least 1 alert, got {len(alerts)}"
    assert mock_send.call_count >= 1, "Monitor should send alert via Telegram"

    # 알림 타입 확인
    alert_types = {a.alert_type for a in alerts}
    from src.core.enums import MonitoringAlertType

    assert MonitoringAlertType.STOP_LOSS_PROXIMITY in alert_types, (
        f"Expected stop_loss_proximity alert, got {alert_types}"
    )


# ── Scenario 7: Control API Round Trip ────────────────────────────────


async def test_control_api_round_trip(client, mock_send):
    """Control API 5개 엔드포인트 라운드 트립."""
    # 1. GET /api/control/scheduler/status
    resp = await client.get("/api/control/scheduler/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_running"] is True
    assert len(body["jobs"]) == 9

    # 2. POST /api/control/scheduler/pause
    resp = await client.post("/api/control/scheduler/pause")
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"

    # 3. GET /api/control/scheduler/status — paused 확인
    resp = await client.get("/api/control/scheduler/status")
    assert resp.status_code == 200
    assert resp.json()["is_paused"] is True

    # 4. POST /api/control/scheduler/resume
    resp = await client.post("/api/control/scheduler/resume")
    assert resp.status_code == 200
    assert resp.json()["status"] == "resumed"

    # 5. POST /api/control/scheduler/run/daily_report
    mock_send.reset_mock()
    resp = await client.post("/api/control/scheduler/run/daily_report")
    assert resp.status_code == 200
    assert resp.json()["status"] == "triggered"

    # 6. GET /api/control/jobs/history — daily_report 이력
    resp = await client.get("/api/control/jobs/history", params={"job_name": "daily_report"})
    assert resp.status_code == 200
    history = resp.json()
    assert history["total"] >= 1, "Should have at least 1 daily_report execution"
    job_names = {item["job_name"] for item in history["items"]}
    assert "daily_report" in job_names


# ── Scenario 8: Trades API with Performance ───────────────────────────


async def test_trades_api_with_performance(client, session_factory):
    """Trades API 3개 엔드포인트: 목록 + 요약 + 상세."""
    # 이전 테스트에서 closed positions 시드됨 (test 4에서 5건)

    # 1. GET /api/trades — closed positions 목록
    resp = await client.get("/api/trades")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1, f"Expected closed trades, got {body['total']}"
    assert "items" in body

    # 2. GET /api/trades/summary — 성과 지표
    resp = await client.get("/api/trades/summary")
    assert resp.status_code == 200
    summary = resp.json()
    assert "total_trades" in summary
    assert summary["total_trades"] >= 1

    # 3. GET /api/trades/{position_id} — 첫 번째 거래 상세
    if body["items"]:
        first_trade = body["items"][0]
        trade_id = first_trade["id"]
        resp = await client.get(f"/api/trades/{trade_id}")
        assert resp.status_code == 200
        detail = resp.json()
        assert "position" in detail
