"""Unit tests for backoffice web UI routes (/admin/*)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import src.main as main_mod
from src.api.auth import require_admin
from src.db.session import get_db_session


# ── Helpers ──────────────────────────────────────────────────────────


def _make_execute_results(*results):
    """side_effect list: int→scalar_one, list→scalars().all()."""
    mocks = []
    for r in results:
        m = MagicMock()
        if isinstance(r, int):
            m.scalar_one = MagicMock(return_value=r)
            m.scalars = MagicMock()
            m.scalars.return_value.all.return_value = []
        elif isinstance(r, list):
            m.scalars = MagicMock()
            m.scalars.return_value.all.return_value = r
        else:
            m = r
        mocks.append(m)
    return mocks


def _mock_account(**kwargs):
    """Account ORM mock."""
    acct = MagicMock()
    acct.id = kwargs.get("id", "acc-1")
    acct.nickname = kwargs.get("nickname", "테스트계좌")
    acct.strategy_type = kwargs.get("strategy_type", "position")
    acct.kis_is_paper = kwargs.get("kis_is_paper", True)
    acct.is_active = True
    acct.created_at = MagicMock()
    return acct


def _portfolio_view(*, positions_count=0, is_live=True):
    """PortfolioView 인스턴스 (account_detail 잔고 요약용)."""
    from datetime import date
    from decimal import Decimal

    from src.api.portfolio_live import PortfolioView

    return PortfolioView(
        total_value=Decimal("1000000"),
        cash=Decimal("500000"),
        invested=Decimal("500000"),
        unrealized_pnl=Decimal("0"),
        realized_pnl_daily=Decimal("0"),
        drawdown_pct=Decimal("0"),
        positions_count=positions_count,
        trade_count_daily=0,
        snapshot_date=date(2026, 6, 28),
        is_live=is_live,
    )


def _mock_position(**kwargs):
    """PositionRecord ORM mock (account_detail 포지션 표 렌더링용)."""
    from datetime import date
    from decimal import Decimal

    pos = MagicMock()
    pos.id = kwargs.get("id", "pos-1")
    pos.symbol = kwargs.get("symbol", "005930")
    pos.strategy_type = kwargs.get("strategy_type", "position")
    pos.quantity = kwargs.get("quantity", 10)
    pos.avg_cost = kwargs.get("avg_cost", Decimal("70000"))
    pos.entry_price = kwargs.get("entry_price", Decimal("70000"))
    pos.stop_loss_price = kwargs.get("stop_loss_price", Decimal("63000"))
    pos.take_profit_price = kwargs.get("take_profit_price", None)
    pos.entry_date = kwargs.get("entry_date", date(2026, 6, 27))
    return pos


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_session():
    s = AsyncMock()
    # 기본 execute 결과: 빈 all()/scalars().all()/scalar_one=0.
    # symbol_names(종목명 병기)처럼 결과를 소비하는 쿼리도 안전하게 동작.
    # 특정 결과가 필요한 테스트는 execute.side_effect를 지정해 덮어쓴다.
    _default = MagicMock()
    _default.all.return_value = []
    _default.scalars.return_value.all.return_value = []
    _default.scalar_one.return_value = 0
    s.execute = AsyncMock(return_value=_default)
    s.get = AsyncMock(return_value=None)
    s.add = MagicMock()
    s.commit = AsyncMock()
    return s


@pytest.fixture(autouse=True)
def _override_deps(mock_session):
    """인증 우회 + DB 세션 mock 주입."""
    async def _noop():
        return None

    async def _session():
        yield mock_session

    main_mod.app.dependency_overrides[require_admin] = _noop
    main_mod.app.dependency_overrides[get_db_session] = _session
    yield
    main_mod.app.dependency_overrides.clear()


def _client():
    return AsyncClient(
        transport=ASGITransport(app=main_mod.app),
        base_url="http://test",
    )


# ── Login / Logout ───────────────────────────────────────────────────


class TestLoginLogout:
    @pytest.mark.asyncio
    async def test_login_page_200(self):
        async with _client() as c:
            r = await c.get("/admin/login")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_login_post_redirect(self):
        with patch("src.api.auth.get_settings") as mock_settings:
            mock_settings.return_value.ADMIN_PASSWORD = "test-pw"
            mock_settings.return_value.SESSION_SECRET_KEY = "secret"
            mock_settings.return_value.ACCOUNT_ENCRYPTION_KEY = ""
            async with _client() as c:
                r = await c.post(
                    "/admin/login",
                    data={"password": "wrong"},
                    follow_redirects=False,
                )
        assert r.status_code == 303

    @pytest.mark.asyncio
    async def test_logout_redirect(self):
        async with _client() as c:
            r = await c.get("/admin/logout", follow_redirects=False)
        assert r.status_code == 303
        assert "/admin/login" in r.headers.get("location", "")


# ── Dashboard ────────────────────────────────────────────────────────


class TestDashboard:
    @pytest.mark.asyncio
    async def test_dashboard_success(self, mock_session):
        mock_session.execute.side_effect = _make_execute_results(
            MagicMock(),  # SELECT 1
            [_mock_account()],  # accounts
            0,  # disapproved order count (F-06)
            0,  # decision queue pending count
        )
        with (
            patch("src.main.get_redis") as mock_redis,
            patch("src.main.get_scheduler") as mock_sched,
            patch("src.api.routes.admin_web.dashboard.fetch_portfolio_view", AsyncMock(return_value=None)),
            patch("src.api.routes.admin_web.dashboard.ReportDataFetcher") as MockFetcher,
            patch("src.api.routes.admin_web.dashboard.get_session_factory"),
        ):
            mock_redis.return_value.ping = AsyncMock()
            mock_sched.return_value.get_status.return_value = {
                "is_running": True, "is_paused": False, "jobs": [],
            }
            fetcher = MockFetcher.return_value
            fetcher.get_todays_orders = AsyncMock(return_value=[])

            async with _client() as c:
                r = await c.get("/admin/")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_dashboard_services_down(self, mock_session):
        mock_session.execute.side_effect = _make_execute_results(
            MagicMock(),  # SELECT 1
            [],  # accounts
            0,  # disapproved order count (F-06)
            0,  # decision queue pending count
        )
        with (
            patch("src.main.get_redis", side_effect=RuntimeError),
            patch("src.main.get_scheduler", side_effect=RuntimeError),
            patch("src.api.routes.admin_web.dashboard.fetch_portfolio_view", AsyncMock(return_value=None)),
            patch("src.api.routes.admin_web.dashboard.ReportDataFetcher") as MockFetcher,
            patch("src.api.routes.admin_web.dashboard.get_session_factory"),
        ):
            fetcher = MockFetcher.return_value
            fetcher.get_todays_orders = AsyncMock(return_value=[])

            async with _client() as c:
                r = await c.get("/admin/")
        assert r.status_code == 200


# ── Account Detail ───────────────────────────────────────────────────


class TestAccountDetail:
    @pytest.mark.asyncio
    async def test_found(self, mock_session):
        mock_session.get.return_value = _mock_account()
        with (
            patch("src.api.routes.admin_web.accounts.fetch_portfolio_view", AsyncMock(return_value=None)),
            patch("src.api.routes.admin_web.accounts.ReportDataFetcher") as MockFetcher,
            patch("src.api.routes.admin_web.accounts.get_session_factory"),
        ):
            fetcher = MockFetcher.return_value
            fetcher.get_open_positions = AsyncMock(return_value=[])
            fetcher.get_pending_orders = AsyncMock(return_value=[])

            async with _client() as c:
                r = await c.get("/admin/accounts/acc-1")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_not_found(self, mock_session):
        mock_session.get.return_value = None
        async with _client() as c:
            r = await c.get("/admin/accounts/no-such")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_live_count_mismatch_shows_warning(self, mock_session):
        """B-04: 라이브 잔고 카운트 ≠ DB 포지션 행수 → 불일치 경고 표시."""
        mock_session.get.return_value = _mock_account()
        view = _portfolio_view(positions_count=2, is_live=True)
        with (
            patch("src.api.routes.admin_web.accounts.fetch_portfolio_view", AsyncMock(return_value=view)),
            patch("src.api.routes.admin_web.accounts.ReportDataFetcher") as MockFetcher,
            patch("src.api.routes.admin_web.accounts.get_session_factory"),
        ):
            fetcher = MockFetcher.return_value
            fetcher.get_open_positions = AsyncMock(return_value=[_mock_position()])
            fetcher.get_pending_orders = AsyncMock(return_value=[])

            async with _client() as c:
                r = await c.get("/admin/accounts/acc-1")
        assert r.status_code == 200
        assert "포지션 수 불일치" in r.text

    @pytest.mark.asyncio
    async def test_live_count_match_no_warning(self, mock_session):
        """B-04: 라이브 카운트 == DB 행수면 경고 미표시."""
        mock_session.get.return_value = _mock_account()
        view = _portfolio_view(positions_count=1, is_live=True)
        with (
            patch("src.api.routes.admin_web.accounts.fetch_portfolio_view", AsyncMock(return_value=view)),
            patch("src.api.routes.admin_web.accounts.ReportDataFetcher") as MockFetcher,
            patch("src.api.routes.admin_web.accounts.get_session_factory"),
        ):
            fetcher = MockFetcher.return_value
            fetcher.get_open_positions = AsyncMock(return_value=[_mock_position()])
            fetcher.get_pending_orders = AsyncMock(return_value=[])

            async with _client() as c:
                r = await c.get("/admin/accounts/acc-1")
        assert r.status_code == 200
        assert "포지션 수 불일치" not in r.text

    @pytest.mark.asyncio
    async def test_db_fallback_no_warning(self, mock_session):
        """B-04: DB 폴백(is_live=False)이면 카운트가 달라도 경고 미표시."""
        mock_session.get.return_value = _mock_account()
        view = _portfolio_view(positions_count=2, is_live=False)
        with (
            patch("src.api.routes.admin_web.accounts.fetch_portfolio_view", AsyncMock(return_value=view)),
            patch("src.api.routes.admin_web.accounts.ReportDataFetcher") as MockFetcher,
            patch("src.api.routes.admin_web.accounts.get_session_factory"),
        ):
            fetcher = MockFetcher.return_value
            fetcher.get_open_positions = AsyncMock(return_value=[_mock_position()])
            fetcher.get_pending_orders = AsyncMock(return_value=[])

            async with _client() as c:
                r = await c.get("/admin/accounts/acc-1")
        assert r.status_code == 200
        assert "포지션 수 불일치" not in r.text


class TestAccountEditReload:
    """B-10: 계좌 편집 저장 직후 reload_account로 실행 중 스케줄러에 즉시 반영."""

    @pytest.mark.asyncio
    async def test_edit_triggers_reload(self, mock_session):
        mock_session.get.return_value = _mock_account(id="acc-1")
        runtime = MagicMock()
        runtime.reload_account = AsyncMock(return_value=True)
        original = main_mod._scheduler_runtime
        try:
            main_mod._scheduler_runtime = runtime
            async with _client() as c:
                r = await c.post(
                    "/admin/accounts/acc-1/edit",
                    data={
                        "nickname": "새이름",
                        "strategy_type": "position",
                        "risk_overrides": '{"DAILY_LOSS_LIMIT_KRW": 1000000}',
                    },
                )
        finally:
            main_mod._scheduler_runtime = original
        assert r.status_code == 200
        runtime.reload_account.assert_awaited_once_with("acc-1")

    @pytest.mark.asyncio
    async def test_edit_graceful_when_scheduler_unavailable(self, mock_session):
        """스케줄러 미가동(runtime None) → RuntimeError 흡수, 저장은 유지·안내 표시."""
        mock_session.get.return_value = _mock_account(id="acc-1")
        original = main_mod._scheduler_runtime
        try:
            main_mod._scheduler_runtime = None
            async with _client() as c:
                r = await c.post(
                    "/admin/accounts/acc-1/edit",
                    data={"strategy_type": "position"},
                )
        finally:
            main_mod._scheduler_runtime = original
        assert r.status_code == 200
        assert "스케줄러 미가동" in r.text


# ── Trades ───────────────────────────────────────────────────────────


class TestTrades:
    @pytest.mark.asyncio
    async def test_trades_redirects_to_performance_tab(self, mock_session):
        """매매이력은 성과·거래 화면의 거래 탭으로 병합 — /admin/trades 리다이렉트."""
        async with _client() as c:
            r = await c.get("/admin/trades?account_id=acc-1", follow_redirects=False)
        assert r.status_code == 303
        loc = r.headers.get("location", "")
        assert "/admin/performance?tab=trades" in loc
        assert "account_id=acc-1" in loc

    @pytest.mark.asyncio
    async def test_performance_trades_tab(self, mock_session):
        """성과·거래 거래 탭: accounts → count → rows 순으로 조회."""
        mock_session.execute.side_effect = _make_execute_results(
            [],  # accounts (active_accounts)
            0,   # count
            [],  # trades rows
        )
        async with _client() as c:
            r = await c.get("/admin/performance?tab=trades")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_performance_trades_tab_htmx_partial(self, mock_session):
        mock_session.execute.side_effect = _make_execute_results(
            [],  # accounts
            0,   # count
            [],  # trades rows
        )
        async with _client() as c:
            r = await c.get(
                "/admin/performance?tab=trades", headers={"HX-Request": "true"}
            )
        assert r.status_code == 200


# ── Performance ──────────────────────────────────────────────────────


def _mock_metrics():
    """PerformanceCalculator.calculate 반환값 mock (템플릿 비교 연산 호환)."""
    m = MagicMock()
    m.total_return_pct = 0.0
    m.annualized_return_pct = 0.0
    m.total_pnl = 0
    m.win_rate_pct = 0.0
    m.total_trades = 0
    m.winning_trades = 0
    m.losing_trades = 0
    m.sharpe_ratio = 0.0
    m.sortino_ratio = 0.0
    m.max_drawdown_pct = 0.0
    m.profit_factor = 0.0
    m.avg_win_pct = 0.0
    m.avg_loss_pct = 0.0
    return m


class TestPerformance:
    @pytest.mark.asyncio
    async def test_performance_page(self, mock_session):
        mock_session.execute.side_effect = _make_execute_results(
            [],  # accounts
        )
        with (
            patch("src.api.routes.admin_web.performance.ReportDataFetcher") as MockFetcher,
            patch("src.api.routes.admin_web.performance.PerformanceCalculator") as MockCalc,
            patch("src.api.routes.admin_web.performance.get_session_factory"),
        ):
            fetcher = MockFetcher.return_value
            fetcher.get_closed_positions = AsyncMock(return_value=[])
            fetcher.get_portfolio_snapshots = AsyncMock(return_value=[])
            MockCalc.calculate.return_value = _mock_metrics()
            MockCalc.breakdown_by_strategy.return_value = {}
            MockCalc.breakdown_by_month.return_value = {}

            async with _client() as c:
                r = await c.get("/admin/performance")
        assert r.status_code == 200


# ── Backtest ─────────────────────────────────────────────────────────


class TestBacktest:
    @pytest.mark.asyncio
    async def test_list_page(self, mock_session):
        mock_session.execute.side_effect = _make_execute_results(
            0,  # count
            [],  # runs
        )
        async with _client() as c:
            r = await c.get("/admin/backtest")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_run_post(self, mock_session):
        with patch("src.api.routes.admin_web.backtest._execute_backtest", new_callable=AsyncMock):
            async with _client() as c:
                r = await c.post(
                    "/admin/backtest/run",
                    data={
                        "strategy_type": "position",
                        "mode": "technical",
                        "start_date": "2026-01-01",
                        "end_date": "2026-03-01",
                        "initial_capital": "10000000",
                        "symbols": "005930",
                    },
                    follow_redirects=False,
                )
        assert r.status_code == 303
        mock_session.add.assert_called_once()


# ── Scheduler ────────────────────────────────────────────────────────


class TestScheduler:
    @pytest.mark.asyncio
    async def test_scheduler_page(self, mock_session):
        mock_session.execute.side_effect = _make_execute_results(
            0,  # count
            [],  # history
        )
        with patch("src.main.get_scheduler") as mock_sched:
            mock_sched.return_value.get_status.return_value = {
                "is_running": True, "is_paused": False, "jobs": [],
            }
            async with _client() as c:
                r = await c.get("/admin/scheduler")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_scheduler_pause(self, mock_session):
        mock_session.execute.side_effect = _make_execute_results(
            0,  # count
            [],  # history
        )
        with patch("src.main.get_scheduler") as mock_sched:
            mock_sched.return_value.get_status.return_value = {
                "is_running": True, "is_paused": True, "jobs": [],
            }
            async with _client() as c:
                r = await c.post("/admin/scheduler/pause")
        assert r.status_code == 200
        mock_sched.return_value.pause_all.assert_called_once()


# ── Stock Master ────────────────────────────────────────────────────


def _mock_stock(**kwargs):
    """StockMaster ORM mock."""
    s = MagicMock()
    s.symbol = kwargs.get("symbol", "005930")
    s.name = kwargs.get("name", "삼성전자")
    s.market_type = kwargs.get("market_type", "kospi")
    s.is_active = kwargs.get("is_active", True)
    s.updated_at = MagicMock()
    s.updated_at.strftime = MagicMock(return_value="2026-03-30 10:00")
    return s


class TestStockMaster:
    @pytest.mark.asyncio
    async def test_stock_master_page(self, mock_session):
        mock_session.execute.side_effect = _make_execute_results(
            100,   # total
            50,    # kospi
            50,    # kosdaq
            0,     # inactive
            MagicMock(scalar_one=MagicMock(return_value=None)),  # last_updated
            [_mock_stock()],  # stocks top 10
        )
        async with _client() as c:
            r = await c.get("/admin/stock-master")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_stock_master_page_empty(self, mock_session):
        mock_session.execute.side_effect = _make_execute_results(
            0,     # total
            0,     # kospi
            0,     # kosdaq
            0,     # inactive
            MagicMock(scalar_one=MagicMock(return_value=None)),  # last_updated
            [],    # stocks
        )
        async with _client() as c:
            r = await c.get("/admin/stock-master")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_stock_master_sync_trigger(self, mock_session):
        mock_cache = AsyncMock()
        mock_cache.get_json = AsyncMock(return_value=None)
        with (
            patch("src.api.routes.admin_web.stock_master.get_session_factory"),
            patch("src.data.cache.get_cache", return_value=mock_cache),
        ):
            async with _client() as c:
                r = await c.post("/admin/stock-master/sync")
        assert r.status_code == 200
        assert "동기화 진행 중" in r.text

    @pytest.mark.asyncio
    async def test_stock_master_sync_duplicate_blocked(self, mock_session):
        mock_cache = AsyncMock()
        mock_cache.get_json = AsyncMock(return_value={"status": "running"})
        with (
            patch("src.api.routes.admin_web.stock_master.get_session_factory") as mock_factory,
            patch("src.data.cache.get_cache", return_value=mock_cache),
        ):
            async with _client() as c:
                r = await c.post("/admin/stock-master/sync")
        assert r.status_code == 200
        assert "동기화 진행 중" in r.text

    @pytest.mark.asyncio
    async def test_stock_master_sync_status_running(self, mock_session):
        mock_cache = AsyncMock()
        mock_cache.get_json = AsyncMock(return_value={"status": "running"})
        with patch("src.data.cache.get_cache", return_value=mock_cache):
            async with _client() as c:
                r = await c.get("/admin/stock-master/sync/status")
        assert r.status_code == 200
        assert "동기화 진행 중" in r.text

    @pytest.mark.asyncio
    async def test_stock_master_sync_status_completed(self, mock_session):
        mock_cache = AsyncMock()
        mock_cache.get_json = AsyncMock(return_value={"status": "completed", "count": 2500})
        with patch("src.data.cache.get_cache", return_value=mock_cache):
            async with _client() as c:
                r = await c.get("/admin/stock-master/sync/status")
        assert r.status_code == 200
        assert "2500" in r.text
        assert "동기화 완료" in r.text

    @pytest.mark.asyncio
    async def test_stock_master_sync_status_failed(self, mock_session):
        mock_cache = AsyncMock()
        mock_cache.get_json = AsyncMock(return_value={"status": "failed", "error": "API timeout"})
        with patch("src.data.cache.get_cache", return_value=mock_cache):
            async with _client() as c:
                r = await c.get("/admin/stock-master/sync/status")
        assert r.status_code == 200
        assert "동기화 실패" in r.text
        assert "API timeout" in r.text

    @pytest.mark.asyncio
    async def test_stock_master_sync_status_idle(self, mock_session):
        mock_cache = AsyncMock()
        mock_cache.get_json = AsyncMock(return_value=None)
        with patch("src.data.cache.get_cache", return_value=mock_cache):
            async with _client() as c:
                r = await c.get("/admin/stock-master/sync/status")
        assert r.status_code == 200
        assert "종목 마스터 동기화" in r.text


# ── Manual Order Handler ─────────────────────────────────────────────


class TestManualOrderHandler:
    """POST /admin/accounts/{account_id}/orders."""

    @pytest.mark.asyncio
    async def test_account_not_found(self, mock_session):
        mock_session.get.return_value = None
        async with _client() as c:
            r = await c.post(
                "/admin/accounts/missing/orders",
                data={"symbol": "005930", "quantity": "1", "side": "buy"},
                follow_redirects=False,
            )
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_invalid_side_redirects_with_error(self, mock_session):
        mock_session.get.return_value = _mock_account()
        async with _client() as c:
            r = await c.post(
                "/admin/accounts/acc-1/orders",
                data={"symbol": "005930", "quantity": "1", "side": "sideways"},
                follow_redirects=False,
            )
        assert r.status_code == 303
        assert "order_error" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_missing_symbol_redirects_with_error(self, mock_session):
        mock_session.get.return_value = _mock_account()
        async with _client() as c:
            r = await c.post(
                "/admin/accounts/acc-1/orders",
                data={"symbol": "", "quantity": "1", "side": "buy"},
                follow_redirects=False,
            )
        assert r.status_code == 303
        assert "order_error" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_zero_quantity_redirects_with_error(self, mock_session):
        mock_session.get.return_value = _mock_account()
        async with _client() as c:
            r = await c.post(
                "/admin/accounts/acc-1/orders",
                data={"symbol": "005930", "quantity": "0", "side": "buy"},
                follow_redirects=False,
            )
        assert r.status_code == 303
        assert "order_error" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_happy_path_with_explicit_price(self, mock_session):
        """가격 지정 + executor 성공 → order_success 쿼리로 redirect."""
        from decimal import Decimal

        from src.core.enums import ApprovalStatus, OrderSide, WebVerifyResult
        from src.core.models import ExecutionResult

        mock_session.get.return_value = _mock_account()

        broker = AsyncMock()
        broker.disconnect = AsyncMock()

        executor = AsyncMock()
        executor.execute_entry = AsyncMock(return_value=ExecutionResult(
            success=True,
            order_id=42,
            broker_order_id="KIS42",
            symbol="005930",
            side=OrderSide.BUY,
            quantity=10,
            fill_price=Decimal("70000"),
            approval_status=ApprovalStatus.AUTO_APPROVED,
            web_verify_result=WebVerifyResult.SAFE,
        ))

        with (
            patch("src.api.routes.orders._build_executor",
                  AsyncMock(return_value=(executor, broker, True, AsyncMock()))),
            patch("src.api.routes.orders._resolve_account_label",
                  AsyncMock(return_value="테스트")),
        ):
            async with _client() as c:
                r = await c.post(
                    "/admin/accounts/acc-1/orders",
                    data={
                        "symbol": "005930",
                        "quantity": "10",
                        "price": "70000",
                        "side": "buy",
                    },
                    follow_redirects=False,
                )

        assert r.status_code == 303
        assert "order_success" in r.headers.get("location", "")
        executor.execute_entry.assert_awaited_once()
        kwargs = executor.execute_entry.await_args.kwargs
        assert kwargs["manual"] is True
        assert kwargs["account_id"] == "acc-1"
        broker.disconnect.assert_awaited()

    @pytest.mark.asyncio
    async def test_auto_fetches_price_when_empty(self, mock_session):
        """price 미입력 시 broker.get_price로 현재가를 채움."""
        from decimal import Decimal

        from src.core.enums import ApprovalStatus, OrderSide, WebVerifyResult
        from src.core.models import ExecutionResult

        mock_session.get.return_value = _mock_account()

        broker = AsyncMock()
        broker.get_price = AsyncMock(
            return_value=MagicMock(current_price=Decimal("65000")),
        )
        broker.disconnect = AsyncMock()

        executor = AsyncMock()
        executor.execute_entry = AsyncMock(return_value=ExecutionResult(
            success=True,
            order_id=43,
            broker_order_id="KIS43",
            symbol="005930",
            side=OrderSide.BUY,
            quantity=5,
            fill_price=Decimal("65000"),
            approval_status=ApprovalStatus.AUTO_APPROVED,
            web_verify_result=WebVerifyResult.SAFE,
        ))

        with (
            patch("src.api.routes.orders._build_executor",
                  AsyncMock(return_value=(executor, broker, True, AsyncMock()))),
            patch("src.api.routes.orders._resolve_account_label",
                  AsyncMock(return_value="테스트")),
        ):
            async with _client() as c:
                r = await c.post(
                    "/admin/accounts/acc-1/orders",
                    data={
                        "symbol": "005930",
                        "quantity": "5",
                        "side": "buy",
                    },
                    follow_redirects=False,
                )

        assert r.status_code == 303
        assert "order_success" in r.headers.get("location", "")
        broker.get_price.assert_awaited_once_with("005930")
        kwargs = executor.execute_entry.await_args.kwargs
        assert kwargs["trade_decision"].price == Decimal("65000")

    @pytest.mark.asyncio
    async def test_executor_failure_redirects_with_error(self, mock_session):
        from decimal import Decimal

        from src.core.enums import ApprovalStatus, OrderSide, WebVerifyResult
        from src.core.models import ExecutionResult

        mock_session.get.return_value = _mock_account()

        broker = AsyncMock()
        broker.disconnect = AsyncMock()

        executor = AsyncMock()
        executor.execute_entry = AsyncMock(return_value=ExecutionResult(
            success=False,
            order_id=99,
            symbol="005930",
            side=OrderSide.BUY,
            quantity=10,
            approval_status=ApprovalStatus.AUTO_APPROVED,
            web_verify_result=WebVerifyResult.SAFE,
            error="Cash gate rejected",
        ))

        with (
            patch("src.api.routes.orders._build_executor",
                  AsyncMock(return_value=(executor, broker, True, AsyncMock()))),
            patch("src.api.routes.orders._resolve_account_label",
                  AsyncMock(return_value="테스트")),
        ):
            async with _client() as c:
                r = await c.post(
                    "/admin/accounts/acc-1/orders",
                    data={
                        "symbol": "005930",
                        "quantity": "10",
                        "price": "70000",
                        "side": "buy",
                    },
                    follow_redirects=False,
                )

        assert r.status_code == 303
        assert "order_error" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_market_order_type_passed_through(self, mock_session):
        """B-08: order_type=market 폼값이 TradeDecision.order_type=MARKET로 전달."""
        from decimal import Decimal

        from src.core.enums import (
            ApprovalStatus,
            OrderSide,
            OrderType,
            WebVerifyResult,
        )
        from src.core.models import ExecutionResult

        mock_session.get.return_value = _mock_account()

        broker = AsyncMock()
        broker.get_price = AsyncMock(
            return_value=MagicMock(current_price=Decimal("70000")),
        )
        broker.disconnect = AsyncMock()

        executor = AsyncMock()
        executor.execute_entry = AsyncMock(return_value=ExecutionResult(
            success=True, order_id=50, broker_order_id="KIS50", symbol="005930",
            side=OrderSide.BUY, quantity=10, fill_price=Decimal("70000"),
            approval_status=ApprovalStatus.AUTO_APPROVED,
            web_verify_result=WebVerifyResult.SAFE,
        ))

        with (
            patch("src.api.routes.orders._build_executor",
                  AsyncMock(return_value=(executor, broker, True, AsyncMock()))),
            patch("src.api.routes.orders._resolve_account_label",
                  AsyncMock(return_value="테스트")),
        ):
            async with _client() as c:
                r = await c.post(
                    "/admin/accounts/acc-1/orders",
                    data={
                        "symbol": "005930",
                        "quantity": "10",
                        "order_type": "market",
                        "side": "buy",
                    },
                    follow_redirects=False,
                )

        assert r.status_code == 303
        kwargs = executor.execute_entry.await_args.kwargs
        assert kwargs["trade_decision"].order_type == OrderType.MARKET

    @pytest.mark.asyncio
    async def test_lowercase_symbol_uppercased(self, mock_session):
        """F-22: 소문자 영숫자 심볼 입력이 대문자로 정규화되어 전달된다."""
        from decimal import Decimal

        from src.core.enums import (
            ApprovalStatus,
            OrderSide,
            WebVerifyResult,
        )
        from src.core.models import ExecutionResult

        mock_session.get.return_value = _mock_account()

        broker = AsyncMock()
        broker.get_price = AsyncMock(
            return_value=MagicMock(current_price=Decimal("70000")),
        )
        broker.disconnect = AsyncMock()

        executor = AsyncMock()
        executor.execute_entry = AsyncMock(return_value=ExecutionResult(
            success=True, order_id=52, broker_order_id="KIS52", symbol="0001A0",
            side=OrderSide.BUY, quantity=10, fill_price=Decimal("70000"),
            approval_status=ApprovalStatus.AUTO_APPROVED,
            web_verify_result=WebVerifyResult.SAFE,
        ))

        with (
            patch("src.api.routes.orders._build_executor",
                  AsyncMock(return_value=(executor, broker, True, AsyncMock()))),
            patch("src.api.routes.orders._resolve_account_label",
                  AsyncMock(return_value="테스트")),
        ):
            async with _client() as c:
                r = await c.post(
                    "/admin/accounts/acc-1/orders",
                    data={
                        "symbol": "0001a0",
                        "quantity": "10",
                        "order_type": "market",
                        "side": "buy",
                    },
                    follow_redirects=False,
                )

        assert r.status_code == 303
        kwargs = executor.execute_entry.await_args.kwargs
        assert kwargs["trade_decision"].symbol == "0001A0"

    @pytest.mark.asyncio
    async def test_default_order_type_is_limit(self, mock_session):
        """B-08: order_type 미지정 → 기존대로 LIMIT."""
        from decimal import Decimal

        from src.core.enums import (
            ApprovalStatus,
            OrderSide,
            OrderType,
            WebVerifyResult,
        )
        from src.core.models import ExecutionResult

        mock_session.get.return_value = _mock_account()

        broker = AsyncMock()
        broker.disconnect = AsyncMock()
        executor = AsyncMock()
        executor.execute_entry = AsyncMock(return_value=ExecutionResult(
            success=True, order_id=51, broker_order_id="KIS51", symbol="005930",
            side=OrderSide.BUY, quantity=10, fill_price=Decimal("70000"),
            approval_status=ApprovalStatus.AUTO_APPROVED,
            web_verify_result=WebVerifyResult.SAFE,
        ))

        with (
            patch("src.api.routes.orders._build_executor",
                  AsyncMock(return_value=(executor, broker, True, AsyncMock()))),
            patch("src.api.routes.orders._resolve_account_label",
                  AsyncMock(return_value="테스트")),
        ):
            async with _client() as c:
                r = await c.post(
                    "/admin/accounts/acc-1/orders",
                    data={
                        "symbol": "005930", "quantity": "10",
                        "price": "70000", "side": "buy",
                    },
                    follow_redirects=False,
                )

        assert r.status_code == 303
        kwargs = executor.execute_entry.await_args.kwargs
        assert kwargs["trade_decision"].order_type == OrderType.LIMIT

    @pytest.mark.asyncio
    async def test_pending_order_confirmed_filled_shows_filled(self, mock_session):
        """B-08: 접수분이 동기 확인에서 FILLED → '체결' 안내."""
        from datetime import UTC, datetime
        from decimal import Decimal
        from urllib.parse import unquote

        from src.core.enums import (
            ApprovalStatus,
            OrderSide,
            OrderStatus,
            OrderType,
            WebVerifyResult,
        )
        from src.core.models import ExecutionResult, OrderResult

        mock_session.get.return_value = _mock_account()

        broker = AsyncMock()
        broker.disconnect = AsyncMock()
        executor = AsyncMock()
        executor.execute_entry = AsyncMock(return_value=ExecutionResult(
            success=True, pending=True, order_id=60, broker_order_id="KIS60",
            symbol="005930", side=OrderSide.BUY, quantity=10,
            approval_status=ApprovalStatus.AUTO_APPROVED,
            web_verify_result=WebVerifyResult.SAFE,
        ))
        filled = OrderResult(
            order_id="KIS60", symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=10, price=Decimal("70000"),
            status=OrderStatus.FILLED, filled_quantity=10,
            filled_price=Decimal("71000"), timestamp=datetime.now(UTC),
        )

        with (
            patch("src.api.routes.orders._build_executor",
                  AsyncMock(return_value=(executor, broker, True, AsyncMock()))),
            patch("src.api.routes.orders._resolve_account_label",
                  AsyncMock(return_value="테스트")),
            patch("src.api.routes.orders._confirm_fill",
                  AsyncMock(return_value=filled)),
        ):
            async with _client() as c:
                r = await c.post(
                    "/admin/accounts/acc-1/orders",
                    data={
                        "symbol": "005930", "quantity": "10",
                        "price": "70000", "side": "buy",
                    },
                    follow_redirects=False,
                )

        assert r.status_code == 303
        loc = unquote(r.headers.get("location", ""))
        assert "order_success" in loc
        assert "체결" in loc
        assert "대기" not in loc

    @pytest.mark.asyncio
    async def test_pending_order_confirm_timeout_shows_waiting(self, mock_session):
        """B-08: 접수분이 타임아웃(미확정) → '체결 대기' 폴백."""
        from decimal import Decimal
        from urllib.parse import unquote

        from src.core.enums import ApprovalStatus, OrderSide, WebVerifyResult
        from src.core.models import ExecutionResult

        mock_session.get.return_value = _mock_account()

        broker = AsyncMock()
        broker.disconnect = AsyncMock()
        executor = AsyncMock()
        executor.execute_entry = AsyncMock(return_value=ExecutionResult(
            success=True, pending=True, order_id=61, broker_order_id="KIS61",
            symbol="005930", side=OrderSide.BUY, quantity=10,
            approval_status=ApprovalStatus.AUTO_APPROVED,
            web_verify_result=WebVerifyResult.SAFE,
        ))

        with (
            patch("src.api.routes.orders._build_executor",
                  AsyncMock(return_value=(executor, broker, True, AsyncMock()))),
            patch("src.api.routes.orders._resolve_account_label",
                  AsyncMock(return_value="테스트")),
            patch("src.api.routes.orders._confirm_fill",
                  AsyncMock(return_value=None)),
        ):
            async with _client() as c:
                r = await c.post(
                    "/admin/accounts/acc-1/orders",
                    data={
                        "symbol": "005930", "quantity": "10",
                        "price": "70000", "side": "buy",
                    },
                    follow_redirects=False,
                )

        assert r.status_code == 303
        loc = unquote(r.headers.get("location", ""))
        assert "order_success" in loc
        assert "체결 대기" in loc

    # ── 매도 경로 (F-28: 공용 resolve_manual_sell 위임) ─────────────

    @pytest.mark.asyncio
    async def test_sell_without_position_id_rejected(self, mock_session):
        """어드민 매도는 대상 포지션 선택이 필수."""
        from urllib.parse import unquote

        mock_session.get.return_value = _mock_account()
        async with _client() as c:
            r = await c.post(
                "/admin/accounts/acc-1/orders",
                data={"symbol": "005930", "quantity": "1", "side": "sell"},
                follow_redirects=False,
            )
        assert r.status_code == 303
        assert "대상 포지션" in unquote(r.headers.get("location", ""))

    @pytest.mark.asyncio
    async def test_sell_closed_position_rejected(self, mock_session):
        """closed 포지션은 공용 해석기가 거부 — 브로커 접속 전에 차단."""
        from urllib.parse import unquote

        pos = _mock_position(id=42)
        pos.status = "closed"
        pos.account_id = "acc-1"
        mock_session.get = AsyncMock(side_effect=[_mock_account(), pos])

        async with _client() as c:
            r = await c.post(
                "/admin/accounts/acc-1/orders",
                data={
                    "symbol": "005930", "quantity": "1",
                    "side": "sell", "position_id": "42",
                },
                follow_redirects=False,
            )
        assert r.status_code == 303
        assert "open 상태가 아닙니다" in unquote(r.headers.get("location", ""))

    @pytest.mark.asyncio
    async def test_sell_foreign_account_position_rejected(self, mock_session):
        """다른 계좌의 포지션도 거부."""
        from urllib.parse import unquote

        pos = _mock_position(id=42)
        pos.status = "open"
        pos.account_id = "acc-9"
        mock_session.get = AsyncMock(side_effect=[_mock_account(), pos])

        async with _client() as c:
            r = await c.post(
                "/admin/accounts/acc-1/orders",
                data={
                    "symbol": "005930", "quantity": "1",
                    "side": "sell", "position_id": "42",
                },
                follow_redirects=False,
            )
        assert r.status_code == 303
        assert "이 계좌" in unquote(r.headers.get("location", ""))

    @pytest.mark.asyncio
    async def test_sell_happy_path_uses_execute_exit(self, mock_session):
        """유효 포지션 매도 → execute_exit(MANUAL) 호출."""
        from decimal import Decimal
        from urllib.parse import unquote

        from src.core.enums import ApprovalStatus, ExitReason, OrderSide
        from src.core.models import ExecutionResult

        pos = _mock_position(id=42, quantity=10)
        pos.status = "open"
        pos.account_id = "acc-1"
        mock_session.get = AsyncMock(side_effect=[_mock_account(), pos])

        broker = AsyncMock()
        broker.disconnect = AsyncMock()
        executor = AsyncMock()
        executor.execute_entry = AsyncMock()
        executor.execute_exit = AsyncMock(return_value=ExecutionResult(
            success=True, order_id=42, broker_order_id="KIS42", symbol="005930",
            side=OrderSide.SELL, quantity=4, fill_price=Decimal("72000"),
            approval_status=ApprovalStatus.AUTO_APPROVED,
        ))

        with (
            patch("src.api.routes.orders._build_executor",
                  AsyncMock(return_value=(executor, broker, True, AsyncMock()))),
            patch("src.api.routes.orders._resolve_account_label",
                  AsyncMock(return_value="테스트")),
        ):
            async with _client() as c:
                r = await c.post(
                    "/admin/accounts/acc-1/orders",
                    data={
                        "symbol": "005930", "quantity": "4",
                        "price": "72000", "side": "sell", "position_id": "42",
                    },
                    follow_redirects=False,
                )

        assert r.status_code == 303
        assert "order_success" in unquote(r.headers.get("location", ""))
        executor.execute_entry.assert_not_awaited()
        kwargs = executor.execute_exit.await_args.kwargs
        assert kwargs["position"] is pos
        assert kwargs["exit_quantity"] == 4
        assert kwargs["exit_signal"].reason == ExitReason.MANUAL


class TestSyncOrders:
    """POST /admin/accounts/{id}/sync-orders — 미체결 정리 (B-05 브로커 취소)."""

    @staticmethod
    def _order(*, broker_order_id, days_ago=0, status="submitted", oid=1):
        from datetime import UTC, datetime, timedelta

        o = MagicMock()
        o.id = oid
        o.broker_order_id = broker_order_id
        o.created_at = datetime.now(UTC) - timedelta(days=days_ago)
        o.status = status
        return o

    @staticmethod
    def _patches(orders, broker):
        """sync_orders 내부 의존성 패치 컨텍스트 리스트."""
        from conftest import AsyncContextManagerMock

        session = AsyncMock()
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = orders
        session.execute = AsyncMock(return_value=exec_result)
        session.commit = AsyncMock()
        factory = MagicMock(return_value=AsyncContextManagerMock(session))

        registry = MagicMock()
        registry.get = MagicMock(return_value=broker)

        reconciler = MagicMock()
        reconciler.run = AsyncMock(return_value=0)

        return [
            patch("src.api.routes.admin_web.accounts.get_session_factory",
                  MagicMock(return_value=factory)),
            patch("src.main.get_broker_registry", MagicMock(return_value=registry)),
            patch("src.main.get_telegram_bot", MagicMock(return_value=None)),
            patch("src.execution.fill_finalizer.FillFinalizer", MagicMock()),
            patch("src.execution.reconciler.OrderReconciler",
                  MagicMock(return_value=reconciler)),
            patch("src.strategy.position_manager.PositionManager", MagicMock()),
        ]

    @pytest.mark.asyncio
    async def test_today_order_cancelled_via_broker(self):
        order = self._order(broker_order_id="BRK123", days_ago=0)
        broker = MagicMock()
        broker.cancel_order = AsyncMock(return_value=True)

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in self._patches([order], broker):
                stack.enter_context(p)
            async with _client() as c:
                r = await c.post(
                    "/admin/accounts/acc-1/sync-orders", follow_redirects=False
                )

        assert r.status_code == 303
        broker.cancel_order.assert_awaited_once_with("BRK123")
        assert order.status == "cancelled"

    @pytest.mark.asyncio
    async def test_today_order_left_when_broker_cancel_fails(self):
        order = self._order(broker_order_id="BRK123", days_ago=0)
        broker = MagicMock()
        broker.cancel_order = AsyncMock(return_value=False)

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in self._patches([order], broker):
                stack.enter_context(p)
            async with _client() as c:
                r = await c.post(
                    "/admin/accounts/acc-1/sync-orders", follow_redirects=False
                )

        assert r.status_code == 303
        broker.cancel_order.assert_awaited_once_with("BRK123")
        # 오취소 방지 — 상태 유지(체결 확정은 §2 reconciler에 위임)
        assert order.status == "submitted"

    @pytest.mark.asyncio
    async def test_prior_day_order_db_only_no_broker_call(self):
        order = self._order(broker_order_id="BRK999", days_ago=2)
        broker = MagicMock()
        broker.cancel_order = AsyncMock(return_value=True)

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in self._patches([order], broker):
                stack.enter_context(p)
            async with _client() as c:
                r = await c.post(
                    "/admin/accounts/acc-1/sync-orders", follow_redirects=False
                )

        assert r.status_code == 303
        # 전일 주문은 DB-only cancelled — 브로커 취소 미호출
        broker.cancel_order.assert_not_awaited()
        assert order.status == "cancelled"

    @pytest.mark.asyncio
    async def test_kst_boundary_order_not_treated_as_prior_day(self):
        """B-07: UTC 23:30(전일)=KST 당일 08:30 주문은 '전일'이 아니다.

        과거 UTC 기준 판정에서는 UTC 날짜가 하루 빨라 prior-day로 오분류되어
        브로커 취소 없이 DB-only cancelled 처리됐다. KST 기준으로 바로잡혀야 한다.
        """
        from datetime import UTC, datetime

        order = MagicMock()
        order.id = 1
        order.broker_order_id = "BRK777"
        # UTC 2026-06-27 23:30 → KST 2026-06-28 08:30
        order.created_at = datetime(2026, 6, 27, 23, 30, tzinfo=UTC)
        order.status = "submitted"

        broker = MagicMock()
        broker.cancel_order = AsyncMock(return_value=True)

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in self._patches([order], broker):
                stack.enter_context(p)
            # "오늘"을 KST 2026-06-28로 고정
            stack.enter_context(patch(
                "src.api.routes.admin_web.accounts.today_kst",
                MagicMock(return_value=__import__("datetime").date(2026, 6, 28)),
            ))
            async with _client() as c:
                r = await c.post(
                    "/admin/accounts/acc-1/sync-orders", follow_redirects=False
                )

        assert r.status_code == 303
        # KST 당일로 판정 → 전일 DB-only가 아니라 실제 브로커 취소 경로
        broker.cancel_order.assert_awaited_once_with("BRK777")
        assert order.status == "cancelled"


class TestPendingOrdersScope:
    """B-06: 표시(get_pending_orders)와 정리(sync-orders) 대상 status 일치."""

    @pytest.mark.asyncio
    async def test_get_pending_orders_includes_pending_and_submitted(self):
        from conftest import AsyncContextManagerMock

        from src.report.data_fetcher import ReportDataFetcher

        captured: dict = {}
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session = AsyncMock()

        async def _exec(stmt):
            captured["stmt"] = stmt
            return result

        session.execute = AsyncMock(side_effect=_exec)
        factory = MagicMock(return_value=AsyncContextManagerMock(session))

        await ReportDataFetcher(factory).get_pending_orders()

        sql = str(captured["stmt"].compile(compile_kwargs={"literal_binds": True}))
        assert "'pending'" in sql
        assert "'submitted'" in sql


# ── New screens (F-05~F-09) ──────────────────────────────────────────


class TestDecisionQueue:
    @pytest.mark.asyncio
    async def test_decision_queue_page(self, mock_session):
        # accounts, 4 status counters, paginate count, rows
        mock_session.execute.side_effect = _make_execute_results(
            [],  # accounts
            0, 0, 0, 0,  # pending/executed/expired/rejected counters
            0,   # paginate count
            [],  # rows
        )
        async with _client() as c:
            r = await c.get("/admin/decision-queue")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_decision_queue_detail_not_found(self, mock_session):
        mock_session.get.return_value = None
        async with _client() as c:
            r = await c.get("/admin/decision-queue/999")
        assert r.status_code == 404


class TestEntrySnapshots:
    @pytest.mark.asyncio
    async def test_entry_snapshots_page(self, mock_session):
        mock_session.execute.side_effect = _make_execute_results(
            [],  # accounts
            0,   # count
            [],  # rows
        )
        async with _client() as c:
            r = await c.get("/admin/entry-snapshots")
        assert r.status_code == 200


class TestMemoryViewer:
    @pytest.mark.asyncio
    async def test_memory_page(self):
        with (
            patch("src.api.routes.admin_web.memory.AgentMemoryManager") as MockMgr,
            patch("src.api.routes.admin_web.memory.get_session_factory"),
        ):
            mgr = MockMgr.return_value
            mgr.list_memories = AsyncMock(return_value=([], 0))
            async with _client() as c:
                r = await c.get("/admin/memory")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_memory_cleanup_redirect(self):
        with (
            patch("src.api.routes.admin_web.memory.AgentMemoryManager") as MockMgr,
            patch("src.api.routes.admin_web.memory.get_session_factory"),
        ):
            mgr = MockMgr.return_value
            mgr.cleanup_expired = AsyncMock(return_value=3)
            async with _client() as c:
                r = await c.post("/admin/memory/cleanup", follow_redirects=False)
        assert r.status_code == 303
        assert "/admin/memory" in r.headers.get("location", "")


class TestExecMonitor:
    @pytest.mark.asyncio
    async def test_exec_monitor_ws_disabled(self, mock_session):
        """WS 미가동(get_stoploss_stream=None) + 거부주문 0건 → 정상 렌더."""
        mock_session.execute.side_effect = _make_execute_results([])  # disapproved orders
        with patch("src.main.get_stoploss_stream", return_value=None):
            async with _client() as c:
                r = await c.get("/admin/exec-monitor")
        assert r.status_code == 200
        assert "비활성" in r.text

    @pytest.mark.asyncio
    async def test_exec_monitor_status_partial_with_ws(self, mock_session):
        """WS 가동 시 get_status() 요약 표시 (HTMX 폴링 partial)."""
        mock_session.execute.side_effect = _make_execute_results([])
        stream = MagicMock()
        stream.get_status = AsyncMock(return_value={
            "enabled": True, "stream_connected": True,
            "registered_accounts": ["acc-1"], "watched_symbols": ["005930"],
            "watched_count": 1, "positions_tracked": 1, "inflight": [],
        })
        with patch("src.main.get_stoploss_stream", return_value=stream):
            async with _client() as c:
                r = await c.get("/admin/exec-monitor/status")
        assert r.status_code == 200
        assert "005930" in r.text


# ── LLM Config ───────────────────────────────────────────────────────


def _mock_llm_config(**kwargs):
    """AgentModelConfigDB ORM mock."""
    from decimal import Decimal

    row = MagicMock()
    row.agent_type = kwargs.get("agent_type", "trader")
    row.routing_mode = kwargs.get("routing_mode", "fixed")
    row.primary_model = kwargs.get("primary_model", "openai/gpt-5.2-2025-12-11")
    row.escalation_model = kwargs.get("escalation_model", None)
    threshold = kwargs.get("confidence_threshold", None)
    row.confidence_threshold = Decimal(threshold) if threshold else None
    row.is_active = kwargs.get("is_active", True)
    row.updated_by = kwargs.get("updated_by", "system")
    return row


class TestLLMConfigView:
    @pytest.mark.asyncio
    async def test_llm_config_page_200(self, mock_session):
        """DB 행이 YAML 기본값과 일치 → 드리프트 경고 없음."""
        mock_session.execute.side_effect = _make_execute_results([
            _mock_llm_config(agent_type="trader"),
        ])
        async with _client() as c:
            r = await c.get("/admin/llm-config")
        assert r.status_code == 200
        assert "trader" in r.text
        assert "openai/gpt-5.2-2025-12-11" in r.text
        assert "덮어씁니다" not in r.text  # 드리프트 배너 없음
        # DB 미시드 에이전트도 YAML 기본값으로 표에 노출
        assert "web_verifier" in r.text

    @pytest.mark.asyncio
    async def test_llm_config_page_drift_marker(self, mock_session):
        """DB 값 ≠ YAML 기본값 → ⚠ + 드리프트 배너 렌더."""
        mock_session.execute.side_effect = _make_execute_results([
            _mock_llm_config(
                agent_type="trader",
                routing_mode="escalation",
                primary_model="google/gemini-3.1-pro-preview",
                escalation_model="openai/gpt-5.2-2025-12-11",
                confidence_threshold="0.60",
            ),
        ])
        async with _client() as c:
            r = await c.get("/admin/llm-config")
        assert r.status_code == 200
        assert "⚠" in r.text
        assert "덮어씁니다" in r.text
        assert "google/gemini-3.1-pro-preview" in r.text

    @pytest.mark.asyncio
    async def test_llm_config_reset_redirects(self, mock_session):
        """POST reset → YAML 재시드 후 303 + msg 쿼리."""
        with patch("src.api.admin.llm_config.get_cache") as mock_cache:
            mock_cache.return_value.clear_namespace = AsyncMock()
            async with _client() as c:
                r = await c.post("/admin/llm-config/reset", follow_redirects=False)
        assert r.status_code == 303
        assert "/admin/llm-config" in r.headers.get("location", "")
        assert "msg=" in r.headers.get("location", "")
        assert mock_session.commit.await_count >= 1

    @pytest.mark.asyncio
    async def test_llm_api_requires_admin(self):
        """/api/admin/llm/* 무인증 호출 → 로그인 리다이렉트(303)."""
        main_mod.app.dependency_overrides.pop(require_admin)
        async with _client() as c:
            r = await c.get("/api/admin/llm/config", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers.get("location") == "/admin/login"
