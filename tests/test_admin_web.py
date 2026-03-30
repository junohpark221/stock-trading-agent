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


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_session():
    s = AsyncMock()
    s.execute = AsyncMock()
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
        )
        with (
            patch("src.main.get_redis") as mock_redis,
            patch("src.main.get_scheduler") as mock_sched,
            patch("src.api.routes.admin_web.ReportDataFetcher") as MockFetcher,
            patch("src.api.routes.admin_web.get_session_factory"),
        ):
            mock_redis.return_value.ping = AsyncMock()
            mock_sched.return_value.get_status.return_value = {
                "is_running": True, "is_paused": False, "jobs": [],
            }
            fetcher = MockFetcher.return_value
            fetcher.get_latest_snapshot = AsyncMock(return_value=None)
            fetcher.get_todays_orders = AsyncMock(return_value=[])

            async with _client() as c:
                r = await c.get("/admin/")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_dashboard_services_down(self, mock_session):
        mock_session.execute.side_effect = _make_execute_results(
            MagicMock(),  # SELECT 1
            [],  # accounts
        )
        with (
            patch("src.main.get_redis", side_effect=RuntimeError),
            patch("src.main.get_scheduler", side_effect=RuntimeError),
            patch("src.api.routes.admin_web.ReportDataFetcher") as MockFetcher,
            patch("src.api.routes.admin_web.get_session_factory"),
        ):
            fetcher = MockFetcher.return_value
            fetcher.get_latest_snapshot = AsyncMock(return_value=None)
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
            patch("src.api.routes.admin_web.ReportDataFetcher") as MockFetcher,
            patch("src.api.routes.admin_web.get_session_factory"),
        ):
            fetcher = MockFetcher.return_value
            fetcher.get_latest_snapshot = AsyncMock(return_value=None)
            fetcher.get_open_positions = AsyncMock(return_value=[])

            async with _client() as c:
                r = await c.get("/admin/accounts/acc-1")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_not_found(self, mock_session):
        mock_session.get.return_value = None
        async with _client() as c:
            r = await c.get("/admin/accounts/no-such")
        assert r.status_code == 404


# ── Trades ───────────────────────────────────────────────────────────


class TestTrades:
    @pytest.mark.asyncio
    async def test_trades_page(self, mock_session):
        mock_session.execute.side_effect = _make_execute_results(
            0,  # count
            [],  # trades
            [],  # accounts
        )
        async with _client() as c:
            r = await c.get("/admin/trades")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_trades_htmx_partial(self, mock_session):
        mock_session.execute.side_effect = _make_execute_results(
            0,  # count
            [],  # trades
            [],  # accounts
        )
        async with _client() as c:
            r = await c.get("/admin/trades", headers={"HX-Request": "true"})
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
            patch("src.api.routes.admin_web.ReportDataFetcher") as MockFetcher,
            patch("src.api.routes.admin_web.PerformanceCalculator") as MockCalc,
            patch("src.api.routes.admin_web.get_session_factory"),
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
        with patch("src.api.routes.admin_web._execute_backtest", new_callable=AsyncMock):
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
        with patch("src.api.routes.admin_web.get_session_factory"):
            async with _client() as c:
                r = await c.post("/admin/stock-master/sync")
        assert r.status_code == 200
