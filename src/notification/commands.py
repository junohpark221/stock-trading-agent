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
/buy SYMBOL QTY [PRICE] [계좌명] — 수동 매수 (승인 생략)
/sell SYMBOL QTY [PRICE] [계좌명] — 수동 매도 (승인 생략)
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

<b>/buy</b> SYMBOL QTY [PRICE] [계좌명]
  수동 매수 — 승인 플로우 생략, 즉시 지정가로 접수
  PRICE 생략 시 현재가 사용. 예: <code>/buy 005930 10</code>
  계좌 지정: <code>/buy 005930 10 70000 모던투자</code>

<b>/sell</b> SYMBOL QTY [PRICE] [계좌명]
  수동 매도 — 승인 플로우 생략, 즉시 지정가로 접수
  예: <code>/sell 005930 5</code>

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
    """포트폴리오 요약 조회 (라이브 브로커 잔고 + DB 포지션)."""
    from src.api.portfolio_live import fetch_portfolio_view
    from src.notification.templates import MessageTemplates
    from src.report.data_fetcher import ReportDataFetcher

    session_factory = _deps["session_factory"]
    args = _extract_args(message.text, "portfolio")

    account = await resolve_account(args, session_factory)
    if account is None:
        await message.answer("⚠️ 계좌를 찾을 수 없습니다.", parse_mode="HTML")
        return

    account_id, account_label = account

    view = await fetch_portfolio_view(account_id)
    if view is None:
        await message.answer(
            f"📭 <b>{account_label}</b> 포트폴리오 데이터가 없습니다.",
            parse_mode="HTML",
        )
        return

    fetcher = ReportDataFetcher(session_factory)
    positions = await fetcher.get_open_positions(account_id=account_id)
    text = MessageTemplates.portfolio_summary_command(
        view, len(positions), account_label=account_label,
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


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

_DIVIDER = "━━━━━━━━━━━━━━━━"


@command_router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """시스템 상태 조회."""
    from datetime import datetime

    from sqlalchemy import text

    session_factory = _deps["session_factory"]

    lines = ["🤖 <b>Trading Agent Status</b>", _DIVIDER]

    # Database
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        lines.append("🟢 Database: OK")
    except Exception:
        lines.append("🔴 Database: Error")

    # Redis
    try:
        from src.main import get_redis

        await get_redis().ping()
        lines.append("🟢 Redis: OK")
    except Exception:
        lines.append("🔴 Redis: Error")

    # Scheduler
    try:
        from src.main import get_scheduler

        status = get_scheduler().get_status()
        if status["is_running"]:
            job_count = len(status["jobs"])
            state = f"Running ({job_count} jobs)"
            if status["is_paused"]:
                state = f"Paused ({job_count} jobs)"
            lines.append(f"🟢 Scheduler: {state}")
        else:
            lines.append("🟡 Scheduler: Stopped")
    except RuntimeError:
        lines.append("🟡 Scheduler: Disabled")

    # Telegram Bot — 커맨드가 도착했으므로 항상 정상
    lines.append("🟢 Telegram Bot: Active")

    lines.append(_DIVIDER)

    # 다음 작업
    try:
        from src.main import get_scheduler

        status = get_scheduler().get_status()
        next_jobs = [
            j for j in status["jobs"] if j.get("next_run_time")
        ]
        if next_jobs:
            next_job = min(next_jobs, key=lambda j: j["next_run_time"])
            next_time = next_job["next_run_time"][:16].replace("T", " ")
            lines.append(f"📅 다음 작업: {next_job['name']} ({next_time})")
    except (RuntimeError, Exception):
        pass

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"⏰ 서버 시간: {now}")

    await message.answer("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /buy, /sell — 수동 주문 (승인 생략)
# ---------------------------------------------------------------------------


_BUY_USAGE = (
    "사용법: <code>/buy SYMBOL QTY [PRICE] [계좌명]</code>\n"
    "예: <code>/buy 005930 10</code> · <code>/buy 005930 10 70000 모던투자</code>"
)
_SELL_USAGE = (
    "사용법: <code>/sell SYMBOL QTY [PRICE] [계좌명]</code>\n"
    "예: <code>/sell 005930 5</code>"
)


def _parse_order_args(args: str) -> tuple[str, int, "Decimal | None", str] | None:
    """/buy·/sell 인자 파싱.

    SYMBOL QTY [PRICE] [ACCOUNT...]
    - 1번째: SYMBOL (필수)
    - 2번째: QTY 정수 (필수, >0)
    - 3번째 이후: 숫자면 PRICE, 남은 토큰은 ACCOUNT
    - PRICE는 3번째 토큰이 숫자일 때만 인정 (그 뒤 토큰은 모두 계좌명)

    Returns None if parsing fails.
    """
    from decimal import Decimal, InvalidOperation

    parts = args.split()
    if len(parts) < 2:
        return None

    symbol = parts[0].strip()
    if not symbol:
        return None

    try:
        qty = int(parts[1])
    except ValueError:
        return None
    if qty <= 0:
        return None

    price: Decimal | None = None
    account_tokens: list[str] = []
    if len(parts) >= 3:
        third = parts[2]
        try:
            price = Decimal(third)
            account_tokens = parts[3:]
        except (InvalidOperation, ValueError):
            price = None
            account_tokens = parts[2:]

    if price is not None and price <= 0:
        return None

    account_text = " ".join(account_tokens).strip()
    return symbol, qty, price, account_text


async def _run_manual_order(message: Message, side: str) -> None:
    """Shared handler for /buy and /sell."""
    from decimal import Decimal

    from src.api.routes.orders import _build_executor, _resolve_account_label
    from src.core.enums import DecisionAction, OrderSide
    from src.core.models import TradeDecision

    session_factory = _deps["session_factory"]
    usage = _BUY_USAGE if side == "buy" else _SELL_USAGE
    args = _extract_args(message.text, side)
    if not args:
        await message.answer(f"⚠️ {usage}", parse_mode="HTML")
        return

    parsed = _parse_order_args(args)
    if parsed is None:
        await message.answer(f"⚠️ 인자 파싱 실패\n{usage}", parse_mode="HTML")
        return

    symbol, qty, price, account_text = parsed

    account = await resolve_account(account_text, session_factory)
    if account is None:
        await message.answer(
            "⚠️ 계좌를 찾을 수 없습니다.", parse_mode="HTML",
        )
        return
    account_id, account_label = account

    broker = None
    try:
        executor, broker = await _build_executor(account_id)

        if price is None:
            try:
                price_info = await broker.get_price(symbol)
                price = price_info.current_price
            except Exception as exc:
                await message.answer(
                    f"❌ 현재가 조회 실패: <code>{html.escape(str(exc))}</code>",
                    parse_mode="HTML",
                )
                return
            if price is None or price <= 0:
                await message.answer("❌ 현재가가 유효하지 않습니다.", parse_mode="HTML")
                return

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        action = DecisionAction.BUY if order_side == OrderSide.BUY else DecisionAction.SELL

        td = TradeDecision(
            symbol=symbol,
            action=action,
            confidence=Decimal("1.0"),
            quantity=qty,
            price=price,
            reasoning=f"Telegram manual /{side} by admin",
        )
        # account_label은 여기서 broker label 대신 resolve_account 라벨을 쓰려면
        # 일관성 위해 _resolve_account_label 사용
        display_label = await _resolve_account_label(account_id)
        result = await executor.execute_entry(
            trade_decision=td,
            session_id=__import__("uuid").uuid4(),
            strategy_type="manual",
            account_id=account_id,
            account_label=display_label or account_label,
            manual=True,
        )
    except Exception as exc:
        logger.exception(
            "telegram_manual_order_failed", side=side, symbol=symbol,
        )
        await message.answer(
            f"❌ 주문 실행 오류: <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )
        return
    finally:
        if broker:
            try:
                await broker.disconnect()
            except Exception:
                logger.exception("telegram_manual_order_broker_disconnect_failed")

    side_ko = "매수" if side == "buy" else "매도"
    if result.success:
        fill_price = result.fill_price or price
        text = (
            f"✅ <b>{side_ko} 체결</b>\n"
            f"계좌: {html.escape(display_label or account_label)}\n"
            f"종목: <code>{symbol}</code>\n"
            f"수량: {result.quantity:,}주 @ {fill_price:,}원"
        )
    elif result.pending:
        text = (
            f"⏳ <b>{side_ko} 접수</b> (체결 대기)\n"
            f"계좌: {html.escape(display_label or account_label)}\n"
            f"종목: <code>{symbol}</code>\n"
            f"수량: {qty:,}주 @ {price:,}원\n"
            f"주문번호: <code>{result.broker_order_id or '-'}</code>"
        )
    else:
        text = (
            f"❌ <b>{side_ko} 실패</b>\n"
            f"계좌: {html.escape(display_label or account_label)}\n"
            f"종목: <code>{symbol}</code>\n"
            f"사유: {html.escape(result.error or '-')}"
        )
    await message.answer(text, parse_mode="HTML")


@command_router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    """수동 매수 — 승인 플로우 생략 즉시 접수."""
    await _run_manual_order(message, "buy")


@command_router.message(Command("sell"))
async def cmd_sell(message: Message) -> None:
    """수동 매도 — 승인 플로우 생략 즉시 접수."""
    await _run_manual_order(message, "sell")
