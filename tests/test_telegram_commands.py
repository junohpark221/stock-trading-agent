"""Unit tests for Telegram command handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.notification.commands import (
    ChatIdFilterMiddleware,
    _extract_args,
    _parse_history_args,
    _parse_order_args,
    cmd_buy,
    cmd_help,
    cmd_history,
    cmd_performance,
    cmd_portfolio,
    cmd_positions,
    cmd_sell,
    cmd_start,
    cmd_status,
    resolve_account,
)


# ── Helpers ──────────────────────────────────────────────────────────


class TestExtractArgs:
    def test_with_args(self):
        assert _extract_args("/portfolio 모던투자", "portfolio") == "모던투자"

    def test_no_args(self):
        assert _extract_args("/portfolio", "portfolio") == ""

    def test_none_text(self):
        assert _extract_args(None, "portfolio") == ""


class TestParseHistoryArgs:
    def test_no_args(self):
        assert _parse_history_args("/history") == ("", 5)

    def test_number_only(self):
        assert _parse_history_args("/history 10") == ("", 10)

    def test_account_and_number(self):
        assert _parse_history_args("/history 모던투자 3") == ("모던투자", 3)

    def test_account_only(self):
        assert _parse_history_args("/history 모던투자") == ("모던투자", 5)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_message():
    """Mock aiogram Message."""
    msg = AsyncMock()
    msg.chat = MagicMock()
    msg.chat.id = 123456
    msg.text = ""
    msg.answer = AsyncMock()
    return msg


@pytest.fixture
def mock_session():
    """Mock AsyncSession for session_factory context."""
    return AsyncMock()


@pytest.fixture
def mock_session_factory(mock_session):
    """Mock async_sessionmaker → yields mock_session."""
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    return factory


@pytest.fixture(autouse=True)
def inject_deps(mock_session_factory):
    """_deps에 mock 의존성 주입."""
    from src.notification.commands import _deps

    _deps["session_factory"] = mock_session_factory
    _deps["cache"] = AsyncMock()
    _deps["settings"] = MagicMock()
    yield
    _deps.clear()


# ── Static Commands ──────────────────────────────────────────────────


class TestStaticCommands:
    @pytest.mark.asyncio
    async def test_cmd_start(self, mock_message):
        await cmd_start(mock_message)
        text = mock_message.answer.call_args[0][0]
        assert "Trading Agent Bot" in text

    @pytest.mark.asyncio
    async def test_cmd_help(self, mock_message):
        await cmd_help(mock_message)
        text = mock_message.answer.call_args[0][0]
        assert "커맨드 레퍼런스" in text


# ── ChatIdFilterMiddleware ───────────────────────────────────────────


class TestChatIdFilter:
    @pytest.mark.asyncio
    async def test_allowed_chat_passes(self):
        mw = ChatIdFilterMiddleware("123456")
        handler = AsyncMock(return_value="ok")
        event = MagicMock()
        event.chat.id = 123456
        result = await mw(handler, event, {})
        handler.assert_awaited_once()
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_blocked_chat_rejected(self):
        mw = ChatIdFilterMiddleware("123456")
        handler = AsyncMock()
        event = MagicMock()
        event.chat.id = 999999
        result = await mw(handler, event, {})
        handler.assert_not_awaited()
        assert result is None


# ── resolve_account ──────────────────────────────────────────────────


class TestResolveAccount:
    @pytest.mark.asyncio
    async def test_resolve_default(self, mock_session_factory, mock_session):
        account = MagicMock()
        account.id = "acc-1"
        account.nickname = "테스트계좌"
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = account
        mock_session.execute.return_value = result_mock

        result = await resolve_account("", mock_session_factory)
        assert result == ("acc-1", "테스트계좌")

    @pytest.mark.asyncio
    async def test_resolve_not_found(self, mock_session_factory, mock_session):
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = result_mock

        result = await resolve_account("없는계좌", mock_session_factory)
        assert result is None


# ── Command Handlers ─────────────────────────────────────────────────


class TestCmdPortfolio:
    @pytest.mark.asyncio
    async def test_no_account(self, mock_message):
        mock_message.text = "/portfolio 없는계좌"
        with patch("src.notification.commands.resolve_account", return_value=None):
            await cmd_portfolio(mock_message)
        text = mock_message.answer.call_args[0][0]
        assert "찾을 수 없습니다" in text

    @pytest.mark.asyncio
    async def test_success(self, mock_message, mock_session_factory):
        mock_message.text = "/portfolio"
        view = MagicMock()
        with (
            patch("src.notification.commands.resolve_account", return_value=("acc-1", "테스트")),
            patch("src.api.portfolio_live.fetch_portfolio_view", AsyncMock(return_value=view)),
            patch("src.report.data_fetcher.ReportDataFetcher") as MockFetcher,
            patch("src.notification.templates.MessageTemplates") as MockTemplates,
        ):
            fetcher = MockFetcher.return_value
            fetcher.get_open_positions = AsyncMock(return_value=[MagicMock()])
            MockTemplates.portfolio_summary_command.return_value = "포트폴리오 요약"
            await cmd_portfolio(mock_message)
        mock_message.answer.assert_awaited_once()


class TestCmdPositions:
    @pytest.mark.asyncio
    async def test_success(self, mock_message):
        mock_message.text = "/positions"
        positions = [MagicMock(), MagicMock()]
        with (
            patch("src.notification.commands.resolve_account", return_value=("acc-1", "테스트")),
            patch("src.report.data_fetcher.ReportDataFetcher") as MockFetcher,
            patch("src.notification.templates.MessageTemplates") as MockTemplates,
        ):
            fetcher = MockFetcher.return_value
            fetcher.get_open_positions = AsyncMock(return_value=positions)
            MockTemplates.positions_list_command.return_value = "포지션 목록"
            await cmd_positions(mock_message)
        mock_message.answer.assert_awaited_once()


class TestCmdHistory:
    @pytest.mark.asyncio
    async def test_success(self, mock_message):
        mock_message.text = "/history 3"
        closed = [MagicMock(), MagicMock(), MagicMock()]
        with (
            patch("src.notification.commands.resolve_account", return_value=("acc-1", "테스트")),
            patch("src.report.data_fetcher.ReportDataFetcher") as MockFetcher,
            patch("src.notification.templates.MessageTemplates") as MockTemplates,
        ):
            fetcher = MockFetcher.return_value
            fetcher.get_closed_positions = AsyncMock(return_value=closed)
            MockTemplates.trade_history_command.return_value = "매매 이력"
            await cmd_history(mock_message)
        mock_message.answer.assert_awaited_once()


class TestCmdPerformance:
    @pytest.mark.asyncio
    async def test_success(self, mock_message):
        mock_message.text = "/performance"
        metrics = MagicMock()
        with (
            patch("src.notification.commands.resolve_account", return_value=("acc-1", "테스트")),
            patch("src.report.data_fetcher.ReportDataFetcher") as MockFetcher,
            patch("src.report.metrics.PerformanceCalculator") as MockCalc,
            patch("src.notification.templates.MessageTemplates") as MockTemplates,
        ):
            fetcher = MockFetcher.return_value
            fetcher.get_closed_positions = AsyncMock(return_value=[MagicMock()])
            fetcher.get_portfolio_snapshots = AsyncMock(return_value=[MagicMock()])
            MockCalc.calculate.return_value = metrics
            MockTemplates.performance_summary_command.return_value = "성과 요약"
            await cmd_performance(mock_message)
        mock_message.answer.assert_awaited_once()


class TestCmdStatus:
    @pytest.mark.asyncio
    async def test_all_ok(self, mock_message, mock_session):
        mock_message.text = "/status"
        mock_session.execute = AsyncMock()  # DB OK

        with (
            patch("src.main.get_redis") as mock_redis,
            patch("src.main.get_scheduler") as mock_sched,
        ):
            mock_redis.return_value.ping = AsyncMock()
            mock_sched.return_value.get_status.return_value = {
                "is_running": True,
                "is_paused": False,
                "jobs": [{"name": "test_job", "next_run_time": "2026-03-29T10:00:00"}],
            }
            await cmd_status(mock_message)

        text = mock_message.answer.call_args[0][0]
        assert "Database: OK" in text
        assert "Redis: OK" in text


# ── /buy, /sell 수동 주문 ────────────────────────────────────────────


class TestParseOrderArgs:
    def test_symbol_qty_only(self):
        from decimal import Decimal

        got = _parse_order_args("005930 10")
        assert got == ("005930", 10, None, "")

    def test_with_price(self):
        from decimal import Decimal

        got = _parse_order_args("005930 10 70000")
        assert got == ("005930", 10, Decimal("70000"), "")

    def test_with_price_and_account(self):
        from decimal import Decimal

        got = _parse_order_args("005930 10 70000 모던투자")
        assert got == ("005930", 10, Decimal("70000"), "모던투자")

    def test_account_without_price(self):
        got = _parse_order_args("005930 10 모던투자")
        assert got == ("005930", 10, None, "모던투자")

    def test_multi_word_account(self):
        got = _parse_order_args("005930 10 long term acct")
        assert got == ("005930", 10, None, "long term acct")

    def test_missing_qty(self):
        assert _parse_order_args("005930") is None

    def test_invalid_qty(self):
        assert _parse_order_args("005930 abc") is None

    def test_zero_qty(self):
        assert _parse_order_args("005930 0") is None

    def test_negative_qty(self):
        assert _parse_order_args("005930 -5") is None

    def test_empty(self):
        assert _parse_order_args("") is None


class TestCmdBuySell:
    @pytest.mark.asyncio
    async def test_buy_missing_args_shows_usage(self, mock_message):
        mock_message.text = "/buy"
        await cmd_buy(mock_message)
        text = mock_message.answer.call_args[0][0]
        assert "사용법" in text

    @pytest.mark.asyncio
    async def test_buy_bad_parse_shows_usage(self, mock_message):
        mock_message.text = "/buy bogus"
        await cmd_buy(mock_message)
        text = mock_message.answer.call_args[0][0]
        assert "사용법" in text or "파싱" in text

    @pytest.mark.asyncio
    async def test_buy_account_not_found(self, mock_message):
        mock_message.text = "/buy 005930 10 없는계좌123"
        with patch("src.notification.commands.resolve_account", AsyncMock(return_value=None)):
            await cmd_buy(mock_message)
        text = mock_message.answer.call_args[0][0]
        assert "찾을 수 없습니다" in text

    @pytest.mark.asyncio
    async def test_buy_happy_path(self, mock_message):
        """현재가 자동조회 + executor 호출 → 성공 메시지."""
        from decimal import Decimal

        from src.core.enums import OrderSide, WebVerifyResult
        from src.core.models import ExecutionResult

        mock_message.text = "/buy 005930 10"

        broker = AsyncMock()
        broker.get_price = AsyncMock(
            return_value=MagicMock(current_price=Decimal("70000")),
        )
        broker.disconnect = AsyncMock()

        executor = AsyncMock()
        executor.execute_entry = AsyncMock(return_value=ExecutionResult(
            success=True,
            order_id=1,
            broker_order_id="KIS1",
            symbol="005930",
            side=OrderSide.BUY,
            quantity=10,
            fill_price=Decimal("70000"),
            approval_status=__import__("src.core.enums", fromlist=["ApprovalStatus"]).ApprovalStatus.AUTO_APPROVED,
            web_verify_result=WebVerifyResult.SAFE,
        ))

        with (
            patch("src.notification.commands.resolve_account",
                  AsyncMock(return_value=("acc-1", "테스트"))),
            patch("src.api.routes.orders._build_executor",
                  AsyncMock(return_value=(executor, broker, True, AsyncMock()))),
            patch("src.api.routes.orders._resolve_account_label",
                  AsyncMock(return_value="테스트 (1234)")),
        ):
            await cmd_buy(mock_message)

        executor.execute_entry.assert_awaited_once()
        # manual=True로 호출되었는지 검증
        kwargs = executor.execute_entry.await_args.kwargs
        assert kwargs["manual"] is True
        assert kwargs["account_id"] == "acc-1"

        text = mock_message.answer.call_args[0][0]
        assert "매수 체결" in text
        broker.disconnect.assert_awaited()

    @pytest.mark.asyncio
    async def test_sell_delegates_to_run(self, mock_message):
        """cmd_sell이 side=sell로 공용 핸들러를 호출하는지 간접 검증."""
        mock_message.text = "/sell"
        await cmd_sell(mock_message)
        text = mock_message.answer.call_args[0][0]
        # usage 메시지 형태
        assert "/sell" in text or "사용법" in text
