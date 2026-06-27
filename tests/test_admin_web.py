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
            patch("src.api.routes.admin_web.fetch_portfolio_view", AsyncMock(return_value=None)),
            patch("src.api.routes.admin_web.ReportDataFetcher") as MockFetcher,
            patch("src.api.routes.admin_web.get_session_factory"),
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
        )
        with (
            patch("src.main.get_redis", side_effect=RuntimeError),
            patch("src.main.get_scheduler", side_effect=RuntimeError),
            patch("src.api.routes.admin_web.fetch_portfolio_view", AsyncMock(return_value=None)),
            patch("src.api.routes.admin_web.ReportDataFetcher") as MockFetcher,
            patch("src.api.routes.admin_web.get_session_factory"),
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
            patch("src.api.routes.admin_web.fetch_portfolio_view", AsyncMock(return_value=None)),
            patch("src.api.routes.admin_web.ReportDataFetcher") as MockFetcher,
            patch("src.api.routes.admin_web.get_session_factory"),
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
        mock_cache = AsyncMock()
        mock_cache.get_json = AsyncMock(return_value=None)
        with (
            patch("src.api.routes.admin_web.get_session_factory"),
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
            patch("src.api.routes.admin_web.get_session_factory") as mock_factory,
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
                  AsyncMock(return_value=(executor, broker))),
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
                  AsyncMock(return_value=(executor, broker))),
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
                  AsyncMock(return_value=(executor, broker))),
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
            patch("src.api.routes.admin_web.get_session_factory",
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
