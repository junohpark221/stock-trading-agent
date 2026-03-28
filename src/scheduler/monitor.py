"""Trading monitor — 4가지 트레이딩 위험 감지 + Telegram 알림.

감지 항목:
1. 손절가 근접 (stop-loss proximity)
2. 섹터 편중 (sector concentration)
3. LLM 예산 초과 (LLM budget)
4. 포트폴리오 낙폭 (portfolio drawdown)

Redis 기반 일일 중복방지: ``alert:{type}:{id}:{date}`` 키로 24시간 TTL.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from src.core.enums import MonitoringAlertType
from src.core.models import MonitoringAlert
from src.notification.templates import MessageTemplates

if TYPE_CHECKING:
    from src.broker.base import BrokerInterface
    from src.config import Settings
    from src.data.cache import RedisCache
    from src.llm.cost_tracker import CostTracker
    from src.notification.telegram import TelegramBot
    from src.strategy.portfolio_state import PortfolioStateService
    from src.strategy.position_manager import PositionManager

logger = structlog.get_logger(__name__)

_ALERT_NS = "alert"
_ALERT_TTL = 86_400  # 24 hours


class TradingMonitor:
    """4가지 트레이딩 위험을 감지하고 Telegram으로 알림을 전송한다."""

    def __init__(
        self,
        *,
        portfolio_state_service: PortfolioStateService,
        position_manager: PositionManager,
        cost_tracker: CostTracker,
        telegram_bot: TelegramBot,
        broker: BrokerInterface,
        cache: RedisCache,
        settings: Settings,
        account_id: str = "default",
        account_label: str = "",
    ) -> None:
        self._portfolio = portfolio_state_service
        self._positions = position_manager
        self._cost = cost_tracker
        self._telegram = telegram_bot
        self._broker = broker
        self._cache = cache
        self._settings = settings
        self._account_id = account_id
        self._account_label = account_label

    # ── Public ────────────────────────────────────────────────────────

    async def check_all(self) -> list[MonitoringAlert]:
        """4개 체커를 순차 실행하고 결과를 합산한다.

        개별 체커 실패 시 해당 체커만 skip하고 나머지를 계속 실행한다.
        """
        alerts: list[MonitoringAlert] = []
        checkers: list[tuple[str, object]] = [
            ("stop_loss_proximity", self.check_stop_loss_proximity),
            ("sector_concentration", self.check_sector_concentration),
            ("llm_budget", self.check_llm_budget),
            ("portfolio_drawdown", self.check_portfolio_drawdown),
        ]
        for name, checker in checkers:
            try:
                result = await checker()
                alerts.extend(result)
            except Exception:
                logger.error(f"monitor_{name}_failed", exc_info=True)
        return alerts

    async def check_stop_loss_proximity(self) -> list[MonitoringAlert]:
        """오픈 포지션의 손절가 근접 여부를 확인한다."""
        alerts: list[MonitoringAlert] = []
        threshold = Decimal(str(self._settings.MONITOR_STOP_LOSS_PROXIMITY_PCT))
        positions = await self._positions.get_open(account_id=self._account_id)

        for pos in positions:
            if not pos.stop_loss_price or pos.stop_loss_price <= 0:
                continue

            dedup_key = f"stop_loss_proximity:{self._account_id}:{pos.symbol}:{date.today().isoformat()}"
            if await self._is_alert_sent_today(dedup_key):
                continue

            try:
                price_info = await self._broker.get_price(pos.symbol)
            except Exception:
                logger.warning("monitor_get_price_failed", symbol=pos.symbol, exc_info=True)
                continue

            current = price_info.current_price
            if current <= 0:
                continue

            distance_pct = (current - pos.stop_loss_price) / current * Decimal(100)

            if distance_pct <= threshold:
                msg = f"{pos.symbol} 현재가가 손절가에 {distance_pct:.1f}% 근접"
                alert = MonitoringAlert(
                    alert_type=MonitoringAlertType.STOP_LOSS_PROXIMITY,
                    symbol=pos.symbol,
                    message=msg,
                    current_value=distance_pct,
                    threshold_value=threshold,
                    timestamp=datetime.now(timezone.utc),
                )
                await self._send_alert(alert)
                await self._mark_alert_sent(dedup_key)
                alerts.append(alert)

        return alerts

    async def check_sector_concentration(self) -> list[MonitoringAlert]:
        """섹터별 비중이 경고 기준을 초과하는지 확인한다."""
        alerts: list[MonitoringAlert] = []
        threshold = Decimal(str(self._settings.MONITOR_SECTOR_WEIGHT_WARN_PCT))
        state = await self._portfolio.get_current_state()

        for sector, allocation in state.sector_allocations.items():
            if allocation <= threshold:
                continue

            dedup_key = f"sector_concentration:{self._account_id}:{sector}:{date.today().isoformat()}"
            if await self._is_alert_sent_today(dedup_key):
                continue

            msg = f"{sector} 섹터 비중 {allocation:.1f}%로 경고 기준 초과"
            alert = MonitoringAlert(
                alert_type=MonitoringAlertType.SECTOR_CONCENTRATION,
                symbol=sector,
                message=msg,
                current_value=allocation,
                threshold_value=threshold,
                timestamp=datetime.now(timezone.utc),
            )
            await self._send_alert(alert)
            await self._mark_alert_sent(dedup_key)
            alerts.append(alert)

        return alerts

    async def check_llm_budget(self) -> list[MonitoringAlert]:
        """LLM 월간 예산 사용률이 경고 기준을 초과하는지 확인한다."""
        threshold = Decimal(str(self._settings.MONITOR_LLM_BUDGET_WARN_PCT))
        budget = await self._cost.get_budget_status()

        if budget.usage_percent < threshold:
            return []

        dedup_key = f"llm_budget:monthly:{date.today().isoformat()}"
        if await self._is_alert_sent_today(dedup_key):
            return []

        msg = f"LLM 예산 사용률 {budget.usage_percent:.1f}%"
        alert = MonitoringAlert(
            alert_type=MonitoringAlertType.LLM_BUDGET,
            symbol=None,
            message=msg,
            current_value=budget.usage_percent,
            threshold_value=threshold,
            timestamp=datetime.now(timezone.utc),
        )
        await self._send_alert(alert)
        await self._mark_alert_sent(dedup_key)
        return [alert]

    async def check_portfolio_drawdown(self) -> list[MonitoringAlert]:
        """포트폴리오 낙폭이 최대 허용치를 초과하는지 확인한다."""
        threshold = Decimal(str(self._settings.MAX_DRAWDOWN_PCT))
        state = await self._portfolio.get_current_state()

        if state.drawdown_pct < threshold:
            return []

        dedup_key = f"portfolio_drawdown:{self._account_id}:portfolio:{date.today().isoformat()}"
        if await self._is_alert_sent_today(dedup_key):
            return []

        msg = f"포트폴리오 낙폭 {state.drawdown_pct:.1f}%로 최대 허용치 초과"
        alert = MonitoringAlert(
            alert_type=MonitoringAlertType.PORTFOLIO_DRAWDOWN,
            symbol=None,
            message=msg,
            current_value=state.drawdown_pct,
            threshold_value=threshold,
            timestamp=datetime.now(timezone.utc),
        )
        await self._send_alert(alert)
        await self._mark_alert_sent(dedup_key)
        return [alert]

    # ── Internal helpers ──────────────────────────────────────────────

    async def _send_alert(self, alert: MonitoringAlert) -> None:
        """알림을 HTML로 포맷하고 Telegram으로 전송한다."""
        html = MessageTemplates.monitoring_alert(
            account_label=self._account_label,
            alert_type=alert.alert_type,
            symbol=alert.symbol,
            message=alert.message,
            current_value=alert.current_value,
            threshold_value=alert.threshold_value,
        )
        await self._telegram.send_message(html)
        logger.info(
            "monitor_alert_sent",
            alert_type=alert.alert_type,
            symbol=alert.symbol,
            current_value=str(alert.current_value),
        )

    async def _is_alert_sent_today(self, dedup_key: str) -> bool:
        """오늘 이미 동일 알림을 전송했는지 Redis로 확인한다.

        Redis 장애 시 False 반환 (fail-open: 중복 허용 > 누락 방지).
        """
        try:
            result = await self._cache.get(_ALERT_NS, dedup_key)
            return result is not None
        except Exception:
            logger.warning("monitor_dedup_check_failed", key=dedup_key, exc_info=True)
            return False

    async def _mark_alert_sent(self, dedup_key: str) -> None:
        """알림 전송 완료를 Redis에 기록한다 (TTL 24시간)."""
        try:
            await self._cache.set(_ALERT_NS, dedup_key, "1", ttl=_ALERT_TTL)
        except Exception:
            logger.warning("monitor_dedup_mark_failed", key=dedup_key, exc_info=True)
