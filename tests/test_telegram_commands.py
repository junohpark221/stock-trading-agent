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

    def test_lowercase_symbol_uppercased(self):
        """F-22: 소문자 영숫자 심볼은 대문자로 정규화된다."""
        got = _parse_order_args("0001a0 10")
        assert got == ("0001A0", 10, None, "")

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

    # ── F-28: /sell은 execute_exit 경로 ──────────────────────────────

    @staticmethod
    def _sell_mocks(result):
        """(broker, executor) — /sell 경로용 공용 모의."""
        from decimal import Decimal

        broker = AsyncMock()
        broker.get_price = AsyncMock(
            return_value=MagicMock(current_price=Decimal("70000")),
        )
        broker.disconnect = AsyncMock()

        executor = AsyncMock()
        executor.execute_exit = AsyncMock(return_value=result)
        executor.execute_entry = AsyncMock()
        return broker, executor

    @staticmethod
    def _sell_result(**kwargs):
        from decimal import Decimal

        from src.core.enums import ApprovalStatus, OrderSide
        from src.core.models import ExecutionResult

        return ExecutionResult(
            success=kwargs.get("success", True),
            order_id=1,
            broker_order_id=kwargs.get("broker_order_id", "KIS1"),
            symbol="005930",
            side=OrderSide.SELL,
            quantity=kwargs.get("quantity", 5),
            fill_price=kwargs.get("fill_price", Decimal("70000")),
            approval_status=ApprovalStatus.AUTO_APPROVED,
            pending=kwargs.get("pending", False),
            error=kwargs.get("error", ""),
        )

    @staticmethod
    def _position(**kwargs):
        from decimal import Decimal

        pos = MagicMock()
        pos.id = kwargs.get("id", 7)
        pos.symbol = "005930"
        pos.quantity = kwargs.get("quantity", 10)
        pos.avg_cost = Decimal("68000")
        pos.status = "open"
        pos.account_id = "acc-1"
        return pos

    @pytest.mark.asyncio
    async def test_sell_happy_path_uses_execute_exit(self, mock_message):
        """F-28: /sell은 진입 경로가 아니라 포지션 청산(execute_exit)으로 간다."""
        from src.execution.manual_sell import ManualSellPlan

        mock_message.text = "/sell 005930 5"
        pos = self._position()
        broker, executor = self._sell_mocks(self._sell_result())

        with (
            patch("src.notification.commands.resolve_account",
                  AsyncMock(return_value=("acc-1", "테스트"))),
            patch("src.api.routes.orders._build_executor",
                  AsyncMock(return_value=(executor, broker, True, AsyncMock()))),
            patch("src.api.routes.orders._resolve_account_label",
                  AsyncMock(return_value="테스트 (1234)")),
            patch("src.execution.manual_sell.resolve_manual_sell",
                  AsyncMock(return_value=ManualSellPlan(
                      position=pos, symbol="005930", quantity=5, candidates=[pos],
                  ))),
        ):
            await cmd_sell(mock_message)

        executor.execute_entry.assert_not_awaited()
        kwargs = executor.execute_exit.await_args.kwargs
        assert kwargs["position"] is pos
        assert kwargs["exit_quantity"] == 5
        assert kwargs["manual"] is True
        assert "order_type_override" not in kwargs  # 텔레그램은 LIMIT 기본
        assert "매도 체결" in mock_message.answer.call_args[0][0]
        broker.disconnect.assert_awaited()

    @pytest.mark.asyncio
    async def test_sell_no_position_rejected(self, mock_message):
        """대상 포지션이 없으면 브로커 접속·발주 없이 거부한다."""
        from src.execution.manual_sell import ManualSellRejection

        mock_message.text = "/sell 005930 5"
        broker, executor = self._sell_mocks(self._sell_result())

        with (
            patch("src.notification.commands.resolve_account",
                  AsyncMock(return_value=("acc-1", "테스트"))),
            patch("src.api.routes.orders._build_executor",
                  AsyncMock(return_value=(executor, broker, True, AsyncMock()))) as build,
            patch("src.execution.manual_sell.resolve_manual_sell",
                  AsyncMock(return_value=ManualSellRejection(
                      code="no_open_position",
                      message="open 포지션이 없습니다. 포지션 동기화 후 다시 시도하세요.",
                  ))),
        ):
            await cmd_sell(mock_message)

        text = mock_message.answer.call_args[0][0]
        assert "매도 불가" in text
        assert "포지션 동기화" in text
        executor.execute_exit.assert_not_awaited()
        build.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sell_multi_position_note(self, mock_message):
        """동일 종목 open 포지션이 여러 건이면 대상 선택을 안내한다(F-27)."""
        from src.execution.manual_sell import ManualSellPlan

        mock_message.text = "/sell 005930 5"
        older, newer = self._position(id=12), self._position(id=19)
        broker, executor = self._sell_mocks(self._sell_result())

        with (
            patch("src.notification.commands.resolve_account",
                  AsyncMock(return_value=("acc-1", "테스트"))),
            patch("src.api.routes.orders._build_executor",
                  AsyncMock(return_value=(executor, broker, True, AsyncMock()))),
            patch("src.api.routes.orders._resolve_account_label",
                  AsyncMock(return_value="테스트 (1234)")),
            patch("src.execution.manual_sell.resolve_manual_sell",
                  AsyncMock(return_value=ManualSellPlan(
                      position=older, symbol="005930", quantity=5,
                      candidates=[older, newer],
                  ))),
        ):
            await cmd_sell(mock_message)

        text = mock_message.answer.call_args[0][0]
        assert "동일 종목 open 포지션 2건" in text
        assert "#12" in text

    @pytest.mark.asyncio
    async def test_sell_pending_renders_as_accepted(self, mock_message):
        """pending은 success=True를 동반 — '체결'이 아니라 '접수'로 표시돼야 한다."""
        from src.execution.manual_sell import ManualSellPlan

        mock_message.text = "/sell 005930 5"
        pos = self._position()
        broker, executor = self._sell_mocks(
            self._sell_result(pending=True, fill_price=None)
        )

        with (
            patch("src.notification.commands.resolve_account",
                  AsyncMock(return_value=("acc-1", "테스트"))),
            patch("src.api.routes.orders._build_executor",
                  AsyncMock(return_value=(executor, broker, True, AsyncMock()))),
            patch("src.api.routes.orders._resolve_account_label",
                  AsyncMock(return_value="테스트 (1234)")),
            patch("src.execution.manual_sell.resolve_manual_sell",
                  AsyncMock(return_value=ManualSellPlan(
                      position=pos, symbol="005930", quantity=5, candidates=[pos],
                  ))),
        ):
            await cmd_sell(mock_message)

        text = mock_message.answer.call_args[0][0]
        assert "매도 접수" in text
        assert "체결 대기" in text

    @pytest.mark.asyncio
    async def test_sell_preflight_failure_message(self, mock_message):
        """F-12 매도가능수량 부족(실패)은 사유와 함께 노출된다."""
        from src.execution.manual_sell import ManualSellPlan

        mock_message.text = "/sell 005930 5"
        pos = self._position()
        broker, executor = self._sell_mocks(self._sell_result(
            success=False, fill_price=None,
            error="매도가능수량 부족 — 매도가능=0, 요청=5주",
        ))

        with (
            patch("src.notification.commands.resolve_account",
                  AsyncMock(return_value=("acc-1", "테스트"))),
            patch("src.api.routes.orders._build_executor",
                  AsyncMock(return_value=(executor, broker, True, AsyncMock()))),
            patch("src.api.routes.orders._resolve_account_label",
                  AsyncMock(return_value="테스트 (1234)")),
            patch("src.execution.manual_sell.resolve_manual_sell",
                  AsyncMock(return_value=ManualSellPlan(
                      position=pos, symbol="005930", quantity=5, candidates=[pos],
                  ))),
        ):
            await cmd_sell(mock_message)

        text = mock_message.answer.call_args[0][0]
        assert "매도 실패" in text
        assert "매도가능수량 부족" in text
