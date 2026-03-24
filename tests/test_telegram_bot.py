"""TelegramBot 단위 테스트 — aiogram mock 기반."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.notification.telegram import TelegramBot


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_bot_instance():
    """Mock aiogram Bot instance."""
    bot = AsyncMock()
    msg = MagicMock()
    msg.message_id = 42
    bot.send_message = AsyncMock(return_value=msg)
    bot.edit_message_text = AsyncMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="test_bot"))
    bot.session = MagicMock()
    bot.session.close = AsyncMock()
    return bot


@pytest.fixture
def mock_dp_instance():
    """Mock aiogram Dispatcher instance."""
    dp = MagicMock()
    dp.callback_query = MagicMock()
    dp.callback_query.register = MagicMock()
    dp.message = MagicMock()
    dp.message.register = MagicMock()
    dp.start_polling = AsyncMock()
    dp.stop_polling = AsyncMock()
    return dp


@pytest.fixture
def telegram_bot(mock_bot_instance, mock_dp_instance):
    """TelegramBot with mocked aiogram internals."""
    with (
        patch("src.notification.telegram.Bot", return_value=mock_bot_instance),
        patch("src.notification.telegram.Dispatcher", return_value=mock_dp_instance),
    ):
        bot = TelegramBot(bot_token="fake:token", chat_id="123456")
    # 직접 주입 (패치 범위 밖에서도 사용 가능하도록)
    bot._bot = mock_bot_instance
    bot._dp = mock_dp_instance
    return bot


# ── Creation Tests ───────────────────────────────────────────────────


class TestTelegramBotCreation:
    def test_creation_with_valid_token(self, telegram_bot):
        assert telegram_bot._disabled is False
        assert telegram_bot._bot is not None
        assert telegram_bot._dp is not None

    def test_creation_with_empty_token(self):
        bot = TelegramBot(bot_token="", chat_id="123456")
        assert bot._disabled is True
        assert bot._bot is None
        assert bot._dp is None

    def test_chat_id_stored(self, telegram_bot):
        assert telegram_bot._chat_id == "123456"


# ── send_message Tests ───────────────────────────────────────────────


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_returns_message_id(self, telegram_bot, mock_bot_instance):
        result = await telegram_bot.send_message("hello")
        assert result == 42
        mock_bot_instance.send_message.assert_awaited_once_with(
            chat_id="123456", text="hello", parse_mode="HTML"
        )

    @pytest.mark.asyncio
    async def test_custom_parse_mode(self, telegram_bot, mock_bot_instance):
        await telegram_bot.send_message("hello", parse_mode="Markdown")
        mock_bot_instance.send_message.assert_awaited_once_with(
            chat_id="123456", text="hello", parse_mode="Markdown"
        )

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self):
        bot = TelegramBot(bot_token="", chat_id="123")
        result = await bot.send_message("hello")
        assert result is None

    @pytest.mark.asyncio
    async def test_api_error_returns_none(self, telegram_bot, mock_bot_instance):
        from aiogram.exceptions import TelegramAPIError

        mock_bot_instance.send_message = AsyncMock(
            side_effect=TelegramAPIError(method=MagicMock(), message="error")
        )
        result = await telegram_bot.send_message("hello")
        assert result is None


# ── send_approval_request Tests ──────────────────────────────────────


class TestSendApprovalRequest:
    @pytest.mark.asyncio
    async def test_inline_keyboard_structure(self, telegram_bot, mock_bot_instance):
        rid = uuid4()
        await telegram_bot.send_approval_request("테스트", rid)

        call_kwargs = mock_bot_instance.send_message.call_args.kwargs
        keyboard = call_kwargs["reply_markup"]
        buttons = keyboard.inline_keyboard[0]

        assert len(buttons) == 3
        assert buttons[0].text == "승인 ✅"
        assert buttons[1].text == "거부 ❌"
        assert buttons[2].text == "수정(수량) 📝"

    @pytest.mark.asyncio
    async def test_callback_data_format(self, telegram_bot, mock_bot_instance):
        rid = uuid4()
        await telegram_bot.send_approval_request("테스트", rid)

        call_kwargs = mock_bot_instance.send_message.call_args.kwargs
        buttons = call_kwargs["reply_markup"].inline_keyboard[0]

        assert buttons[0].callback_data == f"approve:{rid}"
        assert buttons[1].callback_data == f"reject:{rid}"
        assert buttons[2].callback_data == f"modify:{rid}"

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self):
        bot = TelegramBot(bot_token="", chat_id="123")
        result = await bot.send_approval_request("test", uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_message_id(self, telegram_bot):
        result = await telegram_bot.send_approval_request("test", uuid4())
        assert result == 42


# ── Callback Handler Tests ───────────────────────────────────────────


class TestCallbackHandler:
    @pytest.mark.asyncio
    async def test_approve_handler_called(self, telegram_bot):
        handler = AsyncMock()
        telegram_bot.register_callback_handler(handler)

        rid = uuid4()
        cq = AsyncMock()
        cq.data = f"approve:{rid}"
        cq.answer = AsyncMock()

        await telegram_bot._on_callback_query(cq)

        cq.answer.assert_awaited_once()
        handler.assert_awaited_once_with("approve", rid)

    @pytest.mark.asyncio
    async def test_reject_handler_called(self, telegram_bot):
        handler = AsyncMock()
        telegram_bot.register_callback_handler(handler)

        rid = uuid4()
        cq = AsyncMock()
        cq.data = f"reject:{rid}"
        cq.answer = AsyncMock()

        await telegram_bot._on_callback_query(cq)
        handler.assert_awaited_once_with("reject", rid)

    @pytest.mark.asyncio
    async def test_invalid_data_ignored(self, telegram_bot):
        handler = AsyncMock()
        telegram_bot.register_callback_handler(handler)

        cq = AsyncMock()
        cq.data = "invalid-no-colon"
        cq.answer = AsyncMock()

        # 콜론 없는 데이터 → 무시
        await telegram_bot._on_callback_query(cq)
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_handler_registered(self, telegram_bot):
        """핸들러 미등록 시 에러 없이 무시."""
        rid = uuid4()
        cq = AsyncMock()
        cq.data = f"approve:{rid}"
        cq.answer = AsyncMock()

        # 에러 없이 완료되어야 함
        await telegram_bot._on_callback_query(cq)

    @pytest.mark.asyncio
    async def test_invalid_uuid_ignored(self, telegram_bot):
        handler = AsyncMock()
        telegram_bot.register_callback_handler(handler)

        cq = AsyncMock()
        cq.data = "approve:not-a-uuid"
        cq.answer = AsyncMock()

        await telegram_bot._on_callback_query(cq)
        handler.assert_not_awaited()


# ── Lifecycle Tests ──────────────────────────────────────────────────


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_polling_task(self, telegram_bot, mock_dp_instance):
        await telegram_bot.start()

        assert telegram_bot.is_running is True
        mock_dp_instance.callback_query.register.assert_called_once()
        mock_dp_instance.message.register.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, telegram_bot, mock_dp_instance):
        await telegram_bot.start()
        assert telegram_bot.is_running is True

        await telegram_bot.stop()
        assert telegram_bot.is_running is False

    @pytest.mark.asyncio
    async def test_start_disabled_noop(self):
        bot = TelegramBot(bot_token="", chat_id="123")
        await bot.start()
        assert bot.is_running is False

    @pytest.mark.asyncio
    async def test_stop_disabled_noop(self):
        bot = TelegramBot(bot_token="", chat_id="123")
        await bot.stop()  # 에러 없이 완료

    @pytest.mark.asyncio
    async def test_start_connection_failure_disables(
        self, telegram_bot, mock_bot_instance
    ):
        from aiogram.exceptions import TelegramAPIError

        mock_bot_instance.get_me = AsyncMock(
            side_effect=TelegramAPIError(method=MagicMock(), message="conn error")
        )
        await telegram_bot.start()

        assert telegram_bot._disabled is True
        assert telegram_bot.is_running is False


# ── update_message Tests ─────────────────────────────────────────────


class TestUpdateMessage:
    @pytest.mark.asyncio
    async def test_calls_edit(self, telegram_bot, mock_bot_instance):
        await telegram_bot.update_message(42, "updated text")
        mock_bot_instance.edit_message_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remove_buttons(self, telegram_bot, mock_bot_instance):
        await telegram_bot.update_message(42, "done", remove_buttons=True)

        call_kwargs = mock_bot_instance.edit_message_text.call_args.kwargs
        assert call_kwargs["reply_markup"] is None

    @pytest.mark.asyncio
    async def test_keep_buttons(self, telegram_bot, mock_bot_instance):
        await telegram_bot.update_message(42, "still active", remove_buttons=False)

        call_kwargs = mock_bot_instance.edit_message_text.call_args.kwargs
        assert "reply_markup" not in call_kwargs


# ── Quantity Modify Flow Tests ───────────────────────────────────────


class TestQuantityModifyFlow:
    @pytest.mark.asyncio
    async def test_prompt_content(self, telegram_bot, mock_bot_instance):
        rid = uuid4()
        await telegram_bot.send_quantity_prompt(rid, 100)

        call_kwargs = mock_bot_instance.send_message.call_args.kwargs
        assert "100주" in call_kwargs["text"]
        assert "숫자로 입력" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_sets_pending_state(self, telegram_bot):
        rid = uuid4()
        await telegram_bot.send_quantity_prompt(rid, 50)
        assert telegram_bot._pending_modify["123456"] == rid

    @pytest.mark.asyncio
    async def test_text_message_resolves_modify(self, telegram_bot):
        handler = AsyncMock()
        telegram_bot.register_callback_handler(handler)

        rid = uuid4()
        await telegram_bot.send_quantity_prompt(rid, 100)

        # 사용자가 "50" 입력
        msg = MagicMock()
        msg.chat = MagicMock()
        msg.chat.id = 123456
        msg.text = "50"

        await telegram_bot._on_text_message(msg)

        handler.assert_awaited_once_with("modify:50", rid)
        assert "123456" not in telegram_bot._pending_modify

    @pytest.mark.asyncio
    async def test_invalid_quantity_keeps_pending(self, telegram_bot, mock_bot_instance):
        handler = AsyncMock()
        telegram_bot.register_callback_handler(handler)

        rid = uuid4()
        await telegram_bot.send_quantity_prompt(rid, 100)

        # 비숫자 입력
        msg = MagicMock()
        msg.chat = MagicMock()
        msg.chat.id = 123456
        msg.text = "abc"

        await telegram_bot._on_text_message(msg)

        handler.assert_not_awaited()
        assert telegram_bot._pending_modify["123456"] == rid

    @pytest.mark.asyncio
    async def test_zero_quantity_rejected(self, telegram_bot):
        handler = AsyncMock()
        telegram_bot.register_callback_handler(handler)

        rid = uuid4()
        await telegram_bot.send_quantity_prompt(rid, 100)

        msg = MagicMock()
        msg.chat = MagicMock()
        msg.chat.id = 123456
        msg.text = "0"

        await telegram_bot._on_text_message(msg)

        handler.assert_not_awaited()
        assert "123456" in telegram_bot._pending_modify

    @pytest.mark.asyncio
    async def test_unrelated_message_ignored(self, telegram_bot):
        """pending 없는 chat에서 메시지 → 무시."""
        handler = AsyncMock()
        telegram_bot.register_callback_handler(handler)

        msg = MagicMock()
        msg.chat = MagicMock()
        msg.chat.id = 999999
        msg.text = "50"

        await telegram_bot._on_text_message(msg)
        handler.assert_not_awaited()
