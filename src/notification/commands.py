"""Telegram command handlers for the trading agent bot.

Step 10: /start, /help + command infrastructure (router, DI, chat ID filter).
Step 11-13: /portfolio, /positions, /history, /performance, /status (후속 구현).
"""

from __future__ import annotations

import html
from typing import Any, Callable

import structlog
from aiogram import BaseMiddleware, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.models.account import Account

logger = structlog.get_logger()

command_router = Router(name="commands")

# 모듈 레벨 의존성 컨테이너 — register_commands()에서 주입
_deps: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Chat ID filter middleware — 허용된 chat_id만 통과
# ---------------------------------------------------------------------------


class ChatIdFilterMiddleware(BaseMiddleware):
    """허용된 TELEGRAM_CHAT_ID 이외의 메시지를 조용히 무시한다."""

    def __init__(self, allowed_chat_id: str) -> None:
        super().__init__()
        self._allowed_chat_id = allowed_chat_id

    async def __call__(
        self,
        handler: Callable,
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if str(event.chat.id) != self._allowed_chat_id:
            logger.debug(
                "telegram_command_blocked",
                chat_id=event.chat.id,
                allowed=self._allowed_chat_id,
            )
            return None
        return await handler(event, data)


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------


def register_commands(
    dp: Dispatcher,
    session_factory: async_sessionmaker[AsyncSession],
    cache: Any,
    settings: Any,
) -> None:
    """커맨드 라우터에 의존성 주입 후 디스패처에 등록.

    반드시 dp.message.register(catch_all) 보다 먼저 호출해야
    커맨드 필터가 우선 매칭된다.
    """
    # 모듈 레벨 컨테이너에 의존성 저장
    _deps["session_factory"] = session_factory
    _deps["cache"] = cache
    _deps["settings"] = settings

    # 보안: 허용된 chat_id만 커맨드 처리
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "")
    if chat_id:
        command_router.message.middleware(ChatIdFilterMiddleware(chat_id))

    dp.include_router(command_router)
    logger.info("telegram_commands_registered")


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

_START_MESSAGE = """\
<b>Trading Agent Bot</b>

사용 가능한 커맨드:

/portfolio [계좌명] — 포트폴리오 요약
/positions [계좌명] — 보유 종목 목록
/history [계좌명] [N] — 최근 매매 이력
/performance [계좌명] — 성과 지표
/status — 시스템 상태
/help — 전체 커맨드 도움말
"""


@command_router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """환영 메시지 + 사용 가능 커맨드 목록."""
    await message.answer(_START_MESSAGE, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

_HELP_MESSAGE = """\
<b>커맨드 레퍼런스</b>

<b>/portfolio</b> [계좌명]
  포트폴리오 요약 (총 자산, 현금, 투자금, 미실현 손익)
  예: <code>/portfolio</code> 또는 <code>/portfolio 모던투자</code>

<b>/positions</b> [계좌명]
  보유 종목 상세 (종목, 수량, 평균가, 손익, 손절/익절가)
  예: <code>/positions</code>

<b>/history</b> [계좌명] [N]
  최근 청산 매매 이력 (기본 5건)
  예: <code>/history 10</code> 또는 <code>/history 모던투자 5</code>

<b>/performance</b> [계좌명]
  성과 지표 (수익률, Sharpe, MDD, 승률, Profit Factor)
  예: <code>/performance</code>

<b>/status</b>
  시스템 상태 (DB, Redis, 스케줄러, 봇)

<b>계좌명 생략 시</b> 첫 번째 활성 계좌가 자동 선택됩니다.
계좌 ID 또는 닉네임으로 지정할 수 있습니다.
"""


@command_router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """전체 커맨드 레퍼런스."""
    await message.answer(_HELP_MESSAGE, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Account resolution utility
# ---------------------------------------------------------------------------


async def resolve_account(
    text: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, str] | None:
    """커맨드 인자에서 계좌명 → (account_id, display_label) 매핑.

    Args:
        text: 사용자 입력 텍스트 (빈 문자열이면 첫 번째 활성 계좌).
        session_factory: DB 세션 팩토리.

    Returns:
        (account_id, display_label) 또는 매칭 실패 시 None.
    """
    text = text.strip()

    async with session_factory() as session:
        if not text:
            # 첫 번째 활성 계좌
            stmt = (
                select(Account)
                .where(Account.is_active.is_(True))
                .order_by(Account.id)
                .limit(1)
            )
            result = await session.execute(stmt)
            account = result.scalar_one_or_none()
        else:
            # ID 정확 매칭 시도
            stmt = (
                select(Account)
                .where(Account.id == text, Account.is_active.is_(True))
            )
            result = await session.execute(stmt)
            account = result.scalar_one_or_none()

            if account is None:
                # 닉네임 ILIKE 매칭
                stmt = (
                    select(Account)
                    .where(
                        Account.nickname.ilike(f"%{text}%"),
                        Account.is_active.is_(True),
                    )
                    .order_by(Account.id)
                    .limit(1)
                )
                result = await session.execute(stmt)
                account = result.scalar_one_or_none()

    if account is None:
        return None

    label = account.nickname or account.id
    return account.id, html.escape(label)
