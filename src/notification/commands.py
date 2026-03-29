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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_args(text: str | None, command: str) -> str:
    """메시지 텍스트에서 커맨드를 제거하고 인자만 반환.

    예: _extract_args("/portfolio 모던투자", "portfolio") → "모던투자"
    """
    if not text:
        return ""
    # /command 또는 /command@botname 제거
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


# ---------------------------------------------------------------------------
# /portfolio
# ---------------------------------------------------------------------------


@command_router.message(Command("portfolio"))
async def cmd_portfolio(message: Message) -> None:
    """포트폴리오 요약 조회."""
    from src.notification.templates import MessageTemplates
    from src.report.data_fetcher import ReportDataFetcher

    session_factory = _deps["session_factory"]
    args = _extract_args(message.text, "portfolio")

    account = await resolve_account(args, session_factory)
    if account is None:
        await message.answer("⚠️ 계좌를 찾을 수 없습니다.", parse_mode="HTML")
        return

    account_id, account_label = account
    fetcher = ReportDataFetcher(session_factory)

    snapshot = await fetcher.get_latest_snapshot(account_id=account_id)
    if snapshot is None:
        await message.answer(
            f"📭 <b>{account_label}</b> 포트폴리오 데이터가 없습니다.",
            parse_mode="HTML",
        )
        return

    positions = await fetcher.get_open_positions(account_id=account_id)
    text = MessageTemplates.portfolio_summary_command(
        snapshot, len(positions), account_label=account_label,
    )
    await message.answer(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /positions
# ---------------------------------------------------------------------------


@command_router.message(Command("positions"))
async def cmd_positions(message: Message) -> None:
    """보유 종목 목록 조회."""
    from src.notification.templates import MessageTemplates
    from src.report.data_fetcher import ReportDataFetcher

    session_factory = _deps["session_factory"]
    args = _extract_args(message.text, "positions")

    account = await resolve_account(args, session_factory)
    if account is None:
        await message.answer("⚠️ 계좌를 찾을 수 없습니다.", parse_mode="HTML")
        return

    account_id, account_label = account
    fetcher = ReportDataFetcher(session_factory)

    positions = await fetcher.get_open_positions(account_id=account_id)
    if not positions:
        await message.answer(
            f"📭 <b>{account_label}</b> 보유 종목이 없습니다.",
            parse_mode="HTML",
        )
        return

    text = MessageTemplates.positions_list_command(
        positions, account_label=account_label,
    )
    await message.answer(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /history
# ---------------------------------------------------------------------------

_DEFAULT_HISTORY_LIMIT = 5
_HISTORY_LOOKBACK_DAYS = 90


def _parse_history_args(text: str) -> tuple[str, int]:
    """'/history [계좌명] [N]' 인자 파싱.

    Returns:
        (account_text, limit) — account_text는 resolve_account()에 전달.
    """
    args = _extract_args(text, "history")
    if not args:
        return "", _DEFAULT_HISTORY_LIMIT

    parts = args.split()
    # 숫자 하나만 → limit
    if len(parts) == 1 and parts[0].isdigit():
        return "", int(parts[0])
    # 마지막이 숫자 → 계좌명 + limit
    if len(parts) >= 2 and parts[-1].isdigit():
        return " ".join(parts[:-1]), int(parts[-1])
    # 전부 계좌명
    return args, _DEFAULT_HISTORY_LIMIT


@command_router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    """최근 매매 이력 조회."""
    from datetime import date, timedelta

    from src.notification.templates import MessageTemplates
    from src.report.data_fetcher import ReportDataFetcher

    session_factory = _deps["session_factory"]
    account_text, limit = _parse_history_args(message.text)

    account = await resolve_account(account_text, session_factory)
    if account is None:
        await message.answer("⚠️ 계좌를 찾을 수 없습니다.", parse_mode="HTML")
        return

    account_id, account_label = account
    fetcher = ReportDataFetcher(session_factory)

    end_date = date.today()
    start_date = end_date - timedelta(days=_HISTORY_LOOKBACK_DAYS)

    closed = await fetcher.get_closed_positions(
        start_date=start_date, end_date=end_date, account_id=account_id,
    )
    if not closed:
        await message.answer(
            f"📭 <b>{account_label}</b> 매매 이력이 없습니다.",
            parse_mode="HTML",
        )
        return

    # exit_date ASC → 최신순으로 뒤집어서 limit 적용
    recent = list(reversed(closed))[:limit]
    text = MessageTemplates.trade_history_command(
        recent, len(recent), account_label=account_label,
    )
    await message.answer(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /performance
# ---------------------------------------------------------------------------

_PERFORMANCE_DAYS = 30


@command_router.message(Command("performance"))
async def cmd_performance(message: Message) -> None:
    """성과 지표 조회."""
    from datetime import date, timedelta

    from src.notification.templates import MessageTemplates
    from src.report.data_fetcher import ReportDataFetcher
    from src.report.metrics import PerformanceCalculator

    session_factory = _deps["session_factory"]
    args = _extract_args(message.text, "performance")

    account = await resolve_account(args, session_factory)
    if account is None:
        await message.answer("⚠️ 계좌를 찾을 수 없습니다.", parse_mode="HTML")
        return

    account_id, account_label = account
    fetcher = ReportDataFetcher(session_factory)

    end_date = date.today()
    start_date = end_date - timedelta(days=_PERFORMANCE_DAYS)

    closed = await fetcher.get_closed_positions(
        start_date=start_date, end_date=end_date, account_id=account_id,
    )
    snapshots = await fetcher.get_portfolio_snapshots(
        start_date=start_date, end_date=end_date, account_id=account_id,
    )

    if not closed and not snapshots:
        await message.answer(
            f"📭 <b>{account_label}</b> 성과 데이터가 없습니다.",
            parse_mode="HTML",
        )
        return

    metrics = PerformanceCalculator.calculate(
        closed_positions=closed,
        snapshots=snapshots,
        period_start=start_date,
        period_end=end_date,
    )
    text = MessageTemplates.performance_summary_command(
        metrics, account_label=account_label,
    )
    await message.answer(text, parse_mode="HTML")
