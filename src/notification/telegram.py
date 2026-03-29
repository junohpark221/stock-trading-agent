"""aiogram 3.x 기반 텔레그램 봇 래퍼.

FastAPI lifespan에서 start/stop 호출하여 봇 라이프사이클을 관리.
콜백 핸들러를 등록하여 승인/거부/수정 버튼 응답 처리.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import structlog
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logger = structlog.get_logger(__name__)


class TelegramBot:
    """aiogram 3.x 기반 텔레그램 봇.

    FastAPI lifespan에서 start/stop 호출하여 봇 라이프사이클을 관리.
    콜백 핸들러를 등록하여 승인/거부/수정 버튼 응답 처리.

    사용 패턴:
        1. lifespan startup에서 bot.start() 호출
        2. ApprovalManager가 bot.send_approval_request() 호출
        3. 사용자 버튼 클릭 → 콜백 핸들러 → ApprovalManager.handle_response()
        4. lifespan shutdown에서 bot.stop() 호출
    """

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        session_factory: Any | None = None,
        cache: Any | None = None,
        settings: Any | None = None,
    ) -> None:
        """aiogram Bot + Dispatcher 초기화.

        bot_token이 빈 문자열이면 경고만 로깅하고, 모든 메서드는 None 반환.
        session_factory/cache/settings는 커맨드 핸들러 DI에 사용.
        """
        self._chat_id = chat_id
        self._session_factory = session_factory
        self._cache = cache
        self._settings = settings
        self._polling_task: asyncio.Task[None] | None = None
        self._callback_handler: Callable[[str, UUID], Awaitable[None]] | None = None
        self._pending_modify: dict[str, UUID] = {}

        if not bot_token:
            logger.warning("telegram_bot_disabled", reason="empty bot_token")
            self._bot: Bot | None = None
            self._dp: Dispatcher | None = None
            self._disabled = True
            return

        self._disabled = False
        self._bot = Bot(token=bot_token)
        self._dp = Dispatcher()

    async def start(self) -> None:
        """봇 polling 시작. asyncio.Task로 백그라운드 실행.

        1. aiogram Dispatcher에 콜백/메시지 핸들러 등록
        2. getMe()로 봇 연결 확인 (실패 시 disabled 전환)
        3. dp.start_polling()을 Task로 시작
        """
        if self._disabled:
            logger.warning("telegram_bot_start_skipped", reason="disabled")
            return

        assert self._bot is not None
        assert self._dp is not None

        # 커맨드 라우터 등록 (catch-all 핸들러보다 먼저 등록해야 우선 매칭)
        if self._session_factory is not None:
            from src.notification.commands import register_commands

            register_commands(
                self._dp, self._session_factory, self._cache, self._settings
            )

        # catch-all 핸들러 등록 (커맨드에 매칭되지 않은 메시지만 받음)
        self._dp.callback_query.register(self._on_callback_query)
        self._dp.message.register(self._on_text_message)

        # 연결 확인
        try:
            me = await self._bot.get_me()
            logger.info("telegram_bot_connected", username=me.username)
        except TelegramAPIError as e:
            logger.error("telegram_bot_connection_failed", error=str(e))
            self._disabled = True
            return

        # 백그라운드 polling 시작
        self._polling_task = asyncio.create_task(
            self._dp.start_polling(self._bot),
            name="telegram_polling",
        )

    async def stop(self) -> None:
        """봇 polling 중지 및 세션 정리."""
        if self._disabled:
            return

        if self._dp:
            await self._dp.stop_polling()

        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        self._polling_task = None

        if self._bot:
            await self._bot.session.close()

        logger.info("telegram_bot_stopped")

    async def send_message(self, text: str, *, parse_mode: str = "HTML") -> int | None:
        """일반 텍스트 메시지 전송. 텔레그램 message_id 반환."""
        if self._disabled:
            return None
        assert self._bot is not None
        try:
            msg = await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=parse_mode,
            )
            return msg.message_id
        except TelegramAPIError as e:
            logger.error("telegram_send_failed", error=str(e))
            return None

    async def send_approval_request(self, text: str, request_id: UUID) -> int | None:
        """승인 요청 메시지 + 인라인 버튼 전송.

        인라인 키보드: [승인 ✅] [거부 ❌] [수정(수량) 📝]
        콜백 데이터: approve:{request_id}, reject:{request_id}, modify:{request_id}
        """
        if self._disabled:
            return None
        assert self._bot is not None

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="승인 ✅",
                        callback_data=f"approve:{request_id}",
                    ),
                    InlineKeyboardButton(
                        text="거부 ❌",
                        callback_data=f"reject:{request_id}",
                    ),
                    InlineKeyboardButton(
                        text="수정(수량) 📝",
                        callback_data=f"modify:{request_id}",
                    ),
                ]
            ]
        )

        try:
            msg = await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return msg.message_id
        except TelegramAPIError as e:
            logger.error(
                "telegram_approval_send_failed",
                request_id=str(request_id),
                error=str(e),
            )
            return None

    async def send_quantity_prompt(
        self, request_id: UUID, original_quantity: int
    ) -> int | None:
        """수량 수정 프롬프트 전송.

        _pending_modify에 상태 저장 후, 사용자가 숫자를 입력하면
        _on_text_message에서 처리.
        """
        if self._disabled:
            return None

        self._pending_modify[self._chat_id] = request_id
        text = f"📝 현재 수량: {original_quantity}주\n새 수량을 숫자로 입력해주세요."
        return await self.send_message(text)

    async def update_message(
        self, message_id: int, text: str, *, remove_buttons: bool = True
    ) -> None:
        """기존 메시지 내용/버튼 업데이트. 처리 결과 표시 후 버튼 제거."""
        if self._disabled:
            return
        assert self._bot is not None
        try:
            kwargs: dict = {
                "text": text,
                "chat_id": self._chat_id,
                "message_id": message_id,
                "parse_mode": "HTML",
            }
            if remove_buttons:
                kwargs["reply_markup"] = None
            await self._bot.edit_message_text(**kwargs)
        except TelegramAPIError as e:
            logger.error(
                "telegram_update_failed", message_id=message_id, error=str(e)
            )

    def register_callback_handler(
        self, handler: Callable[[str, UUID], Awaitable[None]]
    ) -> None:
        """외부 콜백 핸들러 등록.

        handler 시그니처: (action: str, request_id: UUID) -> None
        action: "approve", "reject", "modify:{quantity}"
        """
        self._callback_handler = handler
        logger.info("telegram_callback_handler_registered")

    @property
    def is_running(self) -> bool:
        """봇 polling 실행 여부."""
        if self._disabled:
            return False
        return self._polling_task is not None and not self._polling_task.done()

    # ── Internal Handlers ────────────────────────────────────────────

    async def _on_callback_query(self, callback_query: CallbackQuery) -> None:
        """인라인 버튼 클릭 처리."""
        await callback_query.answer()

        data = callback_query.data
        if not data or ":" not in data:
            logger.warning("telegram_invalid_callback_data", data=data)
            return

        action, _, request_id_str = data.partition(":")
        try:
            request_id = UUID(request_id_str)
        except ValueError:
            logger.warning("telegram_invalid_callback_uuid", data=data)
            return

        if self._callback_handler:
            await self._callback_handler(action, request_id)
        else:
            logger.warning("telegram_no_callback_handler", action=action)

    async def _on_text_message(self, message: Message) -> None:
        """텍스트 메시지 처리 — 수량 수정 대기 중일 때만 처리."""
        chat_id = str(message.chat.id)

        if chat_id not in self._pending_modify:
            return

        request_id = self._pending_modify[chat_id]
        text = (message.text or "").strip()

        try:
            quantity = int(text)
            if quantity <= 0:
                raise ValueError("non-positive")
        except ValueError:
            await self.send_message("⚠️ 올바른 양의 정수를 입력해주세요.")
            return

        del self._pending_modify[chat_id]

        if self._callback_handler:
            await self._callback_handler(f"modify:{quantity}", request_id)
