"""TradingMonitor 단위 테스트.

4가지 체커(stop_loss, sector, llm_budget, drawdown) + 중복방지 + check_all 격리.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.enums import MonitoringAlertType
from src.core.exceptions import CacheError
from src.scheduler.monitor import TradingMonitor, _ALERT_NS, _ALERT_TTL


# ── Helpers ───────────────────────────────────────────────────────────


def _make_position(
    symbol: str = "005930",
    stop_loss_price: Decimal = Decimal("68000"),
    status: str = "open",
) -> MagicMock:
    pos = MagicMock()
    pos.symbol = symbol
    pos.stop_loss_price = stop_loss_price
    pos.status = status
    return pos


def _make_price_info(symbol: str, current_price: Decimal) -> MagicMock:
    info = MagicMock()
    info.symbol = symbol
    info.current_price = current_price
    return info


def _make_budget_status(usage_percent: Decimal) -> MagicMock:
    status = MagicMock()
    status.usage_percent = usage_percent
    return status


def _make_portfolio_state(
    drawdown_pct: Decimal = Decimal("5.0"),
    sector_allocations: dict[str, Decimal] | None = None,
) -> MagicMock:
    state = MagicMock()
    state.drawdown_pct = drawdown_pct
    state.sector_allocations = sector_allocations or {}
    return state


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def mock_settings() -> MagicMock:
    s = MagicMock()
    s.MONITOR_STOP_LOSS_PROXIMITY_PCT = 2.0
    s.MONITOR_SECTOR_WEIGHT_WARN_PCT = 25.0
    s.MONITOR_LLM_BUDGET_WARN_PCT = 80.0
    s.MAX_DRAWDOWN_PCT = 10.0
    return s


@pytest.fixture()
def mock_portfolio() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def mock_position_manager() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def mock_cost_tracker() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def mock_telegram() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def mock_broker() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def mock_cache() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def monitor(
    mock_portfolio: AsyncMock,
    mock_position_manager: AsyncMock,
    mock_cost_tracker: AsyncMock,
    mock_telegram: AsyncMock,
    mock_broker: AsyncMock,
    mock_cache: AsyncMock,
    mock_settings: MagicMock,
) -> TradingMonitor:
    return TradingMonitor(
        portfolio_state_service=mock_portfolio,
        position_manager=mock_position_manager,
        cost_tracker=mock_cost_tracker,
        telegram_bot=mock_telegram,
        broker=mock_broker,
        cache=mock_cache,
        settings=mock_settings,
    )


# ── check_stop_loss_proximity ─────────────────────────────────────────


@pytest.mark.asyncio()
async def test_stop_loss_proximity_triggered(
    monitor: TradingMonitor,
    mock_position_manager: AsyncMock,
    mock_broker: AsyncMock,
    mock_cache: AsyncMock,
    mock_telegram: AsyncMock,
) -> None:
    """현재가가 손절가에 1.5% 근접 → 알림 생성."""
    # 손절가 68000, 현재가 69000 → distance ≈ 1.45%
    mock_position_manager.get_open.return_value = [_make_position("005930", Decimal("68000"))]
    mock_broker.get_price.return_value = _make_price_info("005930", Decimal("69000"))
    mock_cache.get.return_value = None  # 중복 없음

    alerts = await monitor.check_stop_loss_proximity()

    assert len(alerts) == 1
    assert alerts[0].alert_type == MonitoringAlertType.STOP_LOSS_PROXIMITY
    assert alerts[0].symbol == "005930"
    mock_telegram.send_message.assert_called_once()


@pytest.mark.asyncio()
async def test_stop_loss_proximity_safe(
    monitor: TradingMonitor,
    mock_position_manager: AsyncMock,
    mock_broker: AsyncMock,
    mock_cache: AsyncMock,
    mock_telegram: AsyncMock,
) -> None:
    """현재가가 손절가에서 5% 이상 → 알림 없음."""
    # 손절가 68000, 현재가 72000 → distance ≈ 5.6%
    mock_position_manager.get_open.return_value = [_make_position("005930", Decimal("68000"))]
    mock_broker.get_price.return_value = _make_price_info("005930", Decimal("72000"))
    mock_cache.get.return_value = None

    alerts = await monitor.check_stop_loss_proximity()

    assert len(alerts) == 0
    mock_telegram.send_message.assert_not_called()


@pytest.mark.asyncio()
async def test_stop_loss_proximity_exactly_at_threshold(
    monitor: TradingMonitor,
    mock_position_manager: AsyncMock,
    mock_broker: AsyncMock,
    mock_cache: AsyncMock,
) -> None:
    """정확히 2.0% 경계값 → 알림 생성."""
    # distance = (current - stop) / current * 100 = 2.0
    # stop = current * (1 - 0.02) = current * 0.98
    # current = 50000, stop = 49000 → distance = 2.0%
    mock_position_manager.get_open.return_value = [_make_position("000660", Decimal("49000"))]
    mock_broker.get_price.return_value = _make_price_info("000660", Decimal("50000"))
    mock_cache.get.return_value = None

    alerts = await monitor.check_stop_loss_proximity()

    assert len(alerts) == 1
    assert alerts[0].current_value == Decimal("2.0")


@pytest.mark.asyncio()
async def test_stop_loss_proximity_no_positions(
    monitor: TradingMonitor,
    mock_position_manager: AsyncMock,
    mock_telegram: AsyncMock,
) -> None:
    """오픈 포지션 없음 → 빈 리스트."""
    mock_position_manager.get_open.return_value = []

    alerts = await monitor.check_stop_loss_proximity()

    assert alerts == []
    mock_telegram.send_message.assert_not_called()


@pytest.mark.asyncio()
async def test_stop_loss_proximity_price_below_stop(
    monitor: TradingMonitor,
    mock_position_manager: AsyncMock,
    mock_broker: AsyncMock,
    mock_cache: AsyncMock,
) -> None:
    """현재가가 손절가 이하 (이미 돌파) → 알림 생성 (distance ≤ 0)."""
    mock_position_manager.get_open.return_value = [_make_position("005930", Decimal("70000"))]
    mock_broker.get_price.return_value = _make_price_info("005930", Decimal("69000"))
    mock_cache.get.return_value = None

    alerts = await monitor.check_stop_loss_proximity()

    assert len(alerts) == 1
    assert alerts[0].current_value < 0  # 음수 distance


@pytest.mark.asyncio()
async def test_stop_loss_proximity_dedup(
    monitor: TradingMonitor,
    mock_position_manager: AsyncMock,
    mock_cache: AsyncMock,
    mock_telegram: AsyncMock,
) -> None:
    """이미 오늘 알림 전송 → cache hit → 전송 안함."""
    mock_position_manager.get_open.return_value = [_make_position("005930", Decimal("68000"))]
    mock_cache.get.return_value = "1"  # 중복

    alerts = await monitor.check_stop_loss_proximity()

    assert alerts == []
    mock_telegram.send_message.assert_not_called()


@pytest.mark.asyncio()
async def test_stop_loss_proximity_broker_error(
    monitor: TradingMonitor,
    mock_position_manager: AsyncMock,
    mock_broker: AsyncMock,
    mock_cache: AsyncMock,
    mock_telegram: AsyncMock,
) -> None:
    """broker.get_price 에러 → 해당 종목 skip, 다른 종목은 정상."""
    pos1 = _make_position("005930", Decimal("68000"))
    pos2 = _make_position("000660", Decimal("48000"))
    mock_position_manager.get_open.return_value = [pos1, pos2]
    mock_cache.get.return_value = None

    # 첫 번째 종목 에러, 두 번째 종목 성공 (근접)
    mock_broker.get_price.side_effect = [
        RuntimeError("broker error"),
        _make_price_info("000660", Decimal("48500")),  # distance ≈ 1.03%
    ]

    alerts = await monitor.check_stop_loss_proximity()

    assert len(alerts) == 1
    assert alerts[0].symbol == "000660"


@pytest.mark.asyncio()
async def test_stop_loss_proximity_multiple_positions(
    monitor: TradingMonitor,
    mock_position_manager: AsyncMock,
    mock_broker: AsyncMock,
    mock_cache: AsyncMock,
) -> None:
    """복수 포지션 중 일부만 근접 → 해당 포지션만 알림."""
    pos_near = _make_position("005930", Decimal("68000"))  # 근접할 것
    pos_safe = _make_position("000660", Decimal("40000"))  # 안전할 것
    mock_position_manager.get_open.return_value = [pos_near, pos_safe]
    mock_cache.get.return_value = None

    mock_broker.get_price.side_effect = [
        _make_price_info("005930", Decimal("69000")),  # 1.45% → 알림
        _make_price_info("000660", Decimal("50000")),  # 20% → 안전
    ]

    alerts = await monitor.check_stop_loss_proximity()

    assert len(alerts) == 1
    assert alerts[0].symbol == "005930"


# ── check_sector_concentration ────────────────────────────────────────


@pytest.mark.asyncio()
async def test_sector_concentration_triggered(
    monitor: TradingMonitor,
    mock_portfolio: AsyncMock,
    mock_cache: AsyncMock,
    mock_telegram: AsyncMock,
) -> None:
    """섹터 30% → 알림 생성."""
    mock_portfolio.get_current_state.return_value = _make_portfolio_state(
        sector_allocations={"반도체": Decimal("30.0"), "바이오": Decimal("15.0")}
    )
    mock_cache.get.return_value = None

    alerts = await monitor.check_sector_concentration()

    assert len(alerts) == 1
    assert alerts[0].alert_type == MonitoringAlertType.SECTOR_CONCENTRATION
    assert alerts[0].symbol == "반도체"
    mock_telegram.send_message.assert_called_once()


@pytest.mark.asyncio()
async def test_sector_concentration_safe(
    monitor: TradingMonitor,
    mock_portfolio: AsyncMock,
    mock_cache: AsyncMock,
    mock_telegram: AsyncMock,
) -> None:
    """모든 섹터 25% 미만 → 알림 없음."""
    mock_portfolio.get_current_state.return_value = _make_portfolio_state(
        sector_allocations={"반도체": Decimal("20.0"), "바이오": Decimal("15.0")}
    )

    alerts = await monitor.check_sector_concentration()

    assert alerts == []
    mock_telegram.send_message.assert_not_called()


@pytest.mark.asyncio()
async def test_sector_concentration_multiple(
    monitor: TradingMonitor,
    mock_portfolio: AsyncMock,
    mock_cache: AsyncMock,
) -> None:
    """복수 섹터 초과 → 복수 알림."""
    mock_portfolio.get_current_state.return_value = _make_portfolio_state(
        sector_allocations={"반도체": Decimal("30.0"), "바이오": Decimal("28.0")}
    )
    mock_cache.get.return_value = None

    alerts = await monitor.check_sector_concentration()

    assert len(alerts) == 2
    symbols = {a.symbol for a in alerts}
    assert symbols == {"반도체", "바이오"}


@pytest.mark.asyncio()
async def test_sector_concentration_dedup(
    monitor: TradingMonitor,
    mock_portfolio: AsyncMock,
    mock_cache: AsyncMock,
    mock_telegram: AsyncMock,
) -> None:
    """이미 전송된 섹터 알림 → 전송 안함."""
    mock_portfolio.get_current_state.return_value = _make_portfolio_state(
        sector_allocations={"반도체": Decimal("30.0")}
    )
    mock_cache.get.return_value = "1"

    alerts = await monitor.check_sector_concentration()

    assert alerts == []
    mock_telegram.send_message.assert_not_called()


# ── check_llm_budget ──────────────────────────────────────────────────


@pytest.mark.asyncio()
async def test_llm_budget_triggered(
    monitor: TradingMonitor,
    mock_cost_tracker: AsyncMock,
    mock_cache: AsyncMock,
    mock_telegram: AsyncMock,
) -> None:
    """사용률 85% → 알림 생성."""
    mock_cost_tracker.get_budget_status.return_value = _make_budget_status(Decimal("85.0"))
    mock_cache.get.return_value = None

    alerts = await monitor.check_llm_budget()

    assert len(alerts) == 1
    assert alerts[0].alert_type == MonitoringAlertType.LLM_BUDGET
    assert alerts[0].symbol is None
    mock_telegram.send_message.assert_called_once()


@pytest.mark.asyncio()
async def test_llm_budget_safe(
    monitor: TradingMonitor,
    mock_cost_tracker: AsyncMock,
    mock_telegram: AsyncMock,
) -> None:
    """사용률 50% → 알림 없음."""
    mock_cost_tracker.get_budget_status.return_value = _make_budget_status(Decimal("50.0"))

    alerts = await monitor.check_llm_budget()

    assert alerts == []
    mock_telegram.send_message.assert_not_called()


@pytest.mark.asyncio()
async def test_llm_budget_at_threshold(
    monitor: TradingMonitor,
    mock_cost_tracker: AsyncMock,
    mock_cache: AsyncMock,
) -> None:
    """정확히 80% 경계값 → 알림 생성."""
    mock_cost_tracker.get_budget_status.return_value = _make_budget_status(Decimal("80.0"))
    mock_cache.get.return_value = None

    alerts = await monitor.check_llm_budget()

    assert len(alerts) == 1
    assert alerts[0].current_value == Decimal("80.0")


# ── check_portfolio_drawdown ──────────────────────────────────────────


@pytest.mark.asyncio()
async def test_portfolio_drawdown_triggered(
    monitor: TradingMonitor,
    mock_portfolio: AsyncMock,
    mock_cache: AsyncMock,
    mock_telegram: AsyncMock,
) -> None:
    """낙폭 12% → 알림 생성."""
    mock_portfolio.get_current_state.return_value = _make_portfolio_state(
        drawdown_pct=Decimal("12.0")
    )
    mock_cache.get.return_value = None

    alerts = await monitor.check_portfolio_drawdown()

    assert len(alerts) == 1
    assert alerts[0].alert_type == MonitoringAlertType.PORTFOLIO_DRAWDOWN
    assert alerts[0].symbol is None
    mock_telegram.send_message.assert_called_once()


@pytest.mark.asyncio()
async def test_portfolio_drawdown_safe(
    monitor: TradingMonitor,
    mock_portfolio: AsyncMock,
    mock_telegram: AsyncMock,
) -> None:
    """낙폭 5% → 알림 없음."""
    mock_portfolio.get_current_state.return_value = _make_portfolio_state(
        drawdown_pct=Decimal("5.0")
    )

    alerts = await monitor.check_portfolio_drawdown()

    assert alerts == []
    mock_telegram.send_message.assert_not_called()


@pytest.mark.asyncio()
async def test_portfolio_drawdown_dedup(
    monitor: TradingMonitor,
    mock_portfolio: AsyncMock,
    mock_cache: AsyncMock,
    mock_telegram: AsyncMock,
) -> None:
    """이미 전송된 드로다운 알림 → 전송 안함."""
    mock_portfolio.get_current_state.return_value = _make_portfolio_state(
        drawdown_pct=Decimal("12.0")
    )
    mock_cache.get.return_value = "1"

    alerts = await monitor.check_portfolio_drawdown()

    assert alerts == []
    mock_telegram.send_message.assert_not_called()


# ── check_all ─────────────────────────────────────────────────────────


@pytest.mark.asyncio()
async def test_check_all_collects_all(
    monitor: TradingMonitor,
    mock_position_manager: AsyncMock,
    mock_broker: AsyncMock,
    mock_portfolio: AsyncMock,
    mock_cost_tracker: AsyncMock,
    mock_cache: AsyncMock,
) -> None:
    """4개 체커 모두 알림 생성 → 전체 수집."""
    mock_cache.get.return_value = None

    # stop_loss: 근접 포지션
    mock_position_manager.get_open.return_value = [_make_position("005930", Decimal("68000"))]
    mock_broker.get_price.return_value = _make_price_info("005930", Decimal("69000"))

    # sector + drawdown
    mock_portfolio.get_current_state.return_value = _make_portfolio_state(
        drawdown_pct=Decimal("12.0"),
        sector_allocations={"반도체": Decimal("30.0")},
    )

    # llm budget
    mock_cost_tracker.get_budget_status.return_value = _make_budget_status(Decimal("85.0"))

    alerts = await monitor.check_all()

    assert len(alerts) == 4
    alert_types = {a.alert_type for a in alerts}
    assert alert_types == {
        MonitoringAlertType.STOP_LOSS_PROXIMITY,
        MonitoringAlertType.SECTOR_CONCENTRATION,
        MonitoringAlertType.LLM_BUDGET,
        MonitoringAlertType.PORTFOLIO_DRAWDOWN,
    }


@pytest.mark.asyncio()
async def test_check_all_exception_isolation(
    monitor: TradingMonitor,
    mock_position_manager: AsyncMock,
    mock_portfolio: AsyncMock,
    mock_cost_tracker: AsyncMock,
    mock_cache: AsyncMock,
) -> None:
    """한 체커 예외 → 나머지 정상 실행."""
    mock_cache.get.return_value = None

    # stop_loss 체커가 예외 발생
    mock_position_manager.get_open.side_effect = RuntimeError("DB error")

    # sector + drawdown 정상
    mock_portfolio.get_current_state.return_value = _make_portfolio_state(
        drawdown_pct=Decimal("12.0"),
        sector_allocations={"반도체": Decimal("30.0")},
    )

    # llm budget 정상
    mock_cost_tracker.get_budget_status.return_value = _make_budget_status(Decimal("85.0"))

    alerts = await monitor.check_all()

    # stop_loss 빠지고 3개만
    assert len(alerts) == 3


@pytest.mark.asyncio()
async def test_check_all_all_fail(
    monitor: TradingMonitor,
    mock_position_manager: AsyncMock,
    mock_portfolio: AsyncMock,
    mock_cost_tracker: AsyncMock,
) -> None:
    """모든 체커 예외 → 빈 리스트, 예외 전파 없음."""
    mock_position_manager.get_open.side_effect = RuntimeError("err")
    mock_portfolio.get_current_state.side_effect = RuntimeError("err")
    mock_cost_tracker.get_budget_status.side_effect = RuntimeError("err")

    alerts = await monitor.check_all()

    assert alerts == []


# ── Dedup internals ───────────────────────────────────────────────────


@pytest.mark.asyncio()
async def test_is_alert_sent_today_cache_miss(
    monitor: TradingMonitor, mock_cache: AsyncMock
) -> None:
    """cache miss → False."""
    mock_cache.get.return_value = None

    result = await monitor._is_alert_sent_today("test:key")

    assert result is False
    mock_cache.get.assert_called_once_with(_ALERT_NS, "test:key")


@pytest.mark.asyncio()
async def test_is_alert_sent_today_cache_hit(
    monitor: TradingMonitor, mock_cache: AsyncMock
) -> None:
    """cache hit → True."""
    mock_cache.get.return_value = "1"

    result = await monitor._is_alert_sent_today("test:key")

    assert result is True


@pytest.mark.asyncio()
async def test_is_alert_sent_today_cache_error(
    monitor: TradingMonitor, mock_cache: AsyncMock
) -> None:
    """CacheError → False (fail-open)."""
    mock_cache.get.side_effect = CacheError("redis down")

    result = await monitor._is_alert_sent_today("test:key")

    assert result is False


@pytest.mark.asyncio()
async def test_mark_alert_sent(
    monitor: TradingMonitor, mock_cache: AsyncMock
) -> None:
    """mark_alert_sent → cache.set 호출 확인."""
    await monitor._mark_alert_sent("test:key")

    mock_cache.set.assert_called_once_with(_ALERT_NS, "test:key", "1", ttl=_ALERT_TTL)
