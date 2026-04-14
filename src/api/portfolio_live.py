"""UI용 실시간 포트폴리오 조회 헬퍼.

백오피스/텔레그램이 공통으로 사용한다. BrokerRegistry에 등록된 KISClient로
실시간 잔고를 가져오고(30초 Redis 캐시), 실패 시 DB `portfolio_snapshots`로
폴백한다. Registry에 없는 활성 계좌는 자격증명을 복호화해 지연 등록한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from decimal import Decimal

import structlog

from src.core.models import PortfolioState
from src.data.cache import get_cache
from src.db.session import get_session_factory

logger = structlog.get_logger(__name__)

_CACHE_NS = "portfolio_live"
_CACHE_TTL = 30


@dataclass
class PortfolioView:
    """PortfolioSnapshot 호환 뷰. 라이브/DB 모두 이 형태로 정규화한다."""

    total_value: Decimal
    cash: Decimal
    invested: Decimal
    unrealized_pnl: Decimal
    realized_pnl_daily: Decimal
    drawdown_pct: Decimal
    positions_count: int
    trade_count_daily: int
    snapshot_date: _date
    is_live: bool


async def _get_or_register_broker(account_id: str):
    """BrokerRegistry에서 broker 획득. 미등록 활성 계좌는 지연 등록한다."""
    from sqlalchemy import select

    from src.config import get_settings
    from src.db.models.account import Account
    from src.main import get_broker_registry
    from src.scheduler.factory import SchedulerFactory

    registry = get_broker_registry()
    try:
        return registry.get(account_id)
    except KeyError:
        pass

    settings = get_settings()
    if not settings.ACCOUNT_ENCRYPTION_KEY:
        return None

    session_factory = get_session_factory()
    async with session_factory() as session:
        account = (
            await session.execute(
                select(Account).where(
                    Account.id == account_id, Account.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()

    if account is None:
        return None

    creds = SchedulerFactory._decrypt_credentials(
        account, settings.ACCOUNT_ENCRYPTION_KEY,
    )
    logger.info("portfolio_live_broker_lazy_register", account_id=account_id)
    return await registry.register(creds)


async def _live_state(account_id: str) -> PortfolioState | None:
    """30초 캐시를 거친 라이브 PortfolioState 반환. 실패 시 None."""
    from src.strategy.portfolio_state import PortfolioStateService

    cache = get_cache()

    try:
        cached = await cache.get_json(_CACHE_NS, account_id)
    except Exception:
        cached = None
    if cached is not None:
        try:
            return PortfolioState.model_validate(cached)
        except Exception:
            logger.warning("portfolio_live_cache_invalid", account_id=account_id)

    try:
        broker = await _get_or_register_broker(account_id)
    except RuntimeError:
        return None
    except Exception:
        logger.exception("portfolio_live_broker_acquire_failed", account_id=account_id)
        return None
    if broker is None:
        return None

    service = PortfolioStateService(
        broker=broker,
        session_factory=get_session_factory(),
        cache=cache,
        account_id=account_id,
    )
    try:
        state = await service.get_current_state()
    except Exception:
        logger.exception("portfolio_live_fetch_failed", account_id=account_id)
        return None

    try:
        await cache.set_json(
            _CACHE_NS, account_id, state.model_dump(mode="json"), ttl=_CACHE_TTL,
        )
    except Exception:
        logger.warning("portfolio_live_cache_write_failed", account_id=account_id)

    return state


def _state_to_view(state: PortfolioState) -> PortfolioView:
    return PortfolioView(
        total_value=state.total_value,
        cash=state.cash,
        invested=state.invested,
        unrealized_pnl=state.unrealized_pnl,
        realized_pnl_daily=state.daily_pnl,
        drawdown_pct=state.drawdown_pct,
        positions_count=len(state.positions),
        trade_count_daily=state.daily_trade_count,
        snapshot_date=state.timestamp.date(),
        is_live=True,
    )


def _snapshot_to_view(snap) -> PortfolioView:
    return PortfolioView(
        total_value=snap.total_value,
        cash=snap.cash,
        invested=snap.invested,
        unrealized_pnl=snap.unrealized_pnl,
        realized_pnl_daily=snap.realized_pnl_daily,
        drawdown_pct=snap.drawdown_pct,
        positions_count=snap.positions_count,
        trade_count_daily=snap.trade_count_daily,
        snapshot_date=snap.snapshot_date,
        is_live=False,
    )


async def fetch_portfolio_view(account_id: str) -> PortfolioView | None:
    """라이브 잔고 우선, 실패 시 DB `portfolio_snapshots` 폴백."""
    state = await _live_state(account_id)
    if state is not None:
        return _state_to_view(state)

    from src.report.data_fetcher import ReportDataFetcher

    fetcher = ReportDataFetcher(get_session_factory())
    snap = await fetcher.get_latest_snapshot(account_id=account_id)
    if snap is None:
        return None
    return _snapshot_to_view(snap)
