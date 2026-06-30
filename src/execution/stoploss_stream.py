"""StopLossStreamService — 실시간 체결가 기반 손절/트레일링 즉시 트리거 (F-05).

5분 폴링(``job_stop_loss_check``)은 안전망으로 유지하고, 본 서비스는 보유 종목의
KIS 실시간 체결가(``H0STCNT0``)를 구독해 **손절·트레일링 스톱만** 초 단위로 트리거한다.
익절·시간청산·모니터 알림은 폴링 잡의 책임이다.

설계 요점
---------
- 시세는 계좌 무관(시장 데이터)이므로 **단일 공유** ``KISPriceStream`` 으로 전 계좌 보유
  종목의 **합집합**을 구독한다. WS approval은 대표 계정 자격증명으로 발급한다.
- 보유 포지션 스냅샷을 주기적으로(``PRICE_STREAM_SYNC_INTERVAL_SEC``) 갱신하고, 그 합집합으로
  구독 종목을 동기화한다. 틱 평가는 캐시된 스냅샷을 사용해 틱당 DB 조회를 피한다.
- 청산 트리거는 ``ExitCoordinator`` 로 ``position_id`` 를 선점한 경우에만 발주해 폴링/WS
  이중 청산을 방지한다(``ExitConditionChecker`` 는 stateless라 그대로 재사용).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

import structlog

from src.broker.kis.ws_price import KISPriceStream
from src.strategy.trailing import is_trailing_active, trailing_stop_price

if TYPE_CHECKING:
    from src.broker.credentials import AccountCredentials
    from src.broker.kis.ws_codec import PriceTick
    from src.config import Settings
    from src.db.models.strategy import PositionRecord
    from src.execution.exit_coordinator import ExitCoordinator
    from src.execution.exit_executor import ExitExecutionService
    from src.strategy.exit_checker import ExitConditionChecker
    from src.strategy.position_manager import PositionManager

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")
_Q2 = Decimal("0.01")


@dataclass(frozen=True)
class AccountExitDeps:
    """계좌별 청산 의존성 번들 (틱 평가에 필요)."""

    exit_checker: ExitConditionChecker
    exit_service: ExitExecutionService
    position_manager: PositionManager
    account_label: str


class StopLossStreamService:
    """실시간 체결가 손절/트레일링 트리거 서비스 (단일 공유 스트림).

    Parameters
    ----------
    settings: Settings
    position_manager: 전 계좌 오픈 포지션 조회용 공유 매니저
    coordinator: 이중 청산 방지 in-flight 가드
    """

    def __init__(
        self,
        *,
        settings: Settings,
        position_manager: PositionManager,
        coordinator: ExitCoordinator,
    ) -> None:
        self._settings = settings
        self._position_manager = position_manager
        self._coordinator = coordinator

        self._deps: dict[str, AccountExitDeps] = {}
        self._stream: KISPriceStream | None = None
        self._sync_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        # 주기 동기화로 갱신되는 오픈 포지션 스냅샷 (틱 평가에 사용).
        self._positions: list[PositionRecord] = []

    # ── Registration ──────────────────────────────────────────────────

    def register_account(
        self,
        account_id: str,
        *,
        exit_checker: ExitConditionChecker,
        exit_service: ExitExecutionService,
        position_manager: PositionManager,
        account_label: str,
    ) -> None:
        """계좌별 청산 의존성 등록 (factory가 AccountContext별로 호출)."""
        self._deps[account_id] = AccountExitDeps(
            exit_checker=exit_checker,
            exit_service=exit_service,
            position_manager=position_manager,
            account_label=account_label,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self, credentials_list: list[AccountCredentials]) -> None:
        """대표 자격증명으로 단일 체결가 스트림 기동 + 구독 동기화 루프 시작."""
        if not self._settings.STOP_LOSS_WS_ENABLED:
            logger.info("stoploss_stream.disabled_by_config")
            return
        if not self._deps:
            logger.info("stoploss_stream.no_registered_accounts")
            return

        creds = next((c for c in credentials_list if c.app_key), None)
        if creds is None:
            logger.warning("stoploss_stream.no_credentials")
            return

        self._stream = KISPriceStream(
            account_id=creds.account_id,
            app_key=creds.app_key,
            app_secret=creds.app_secret,
            is_paper=creds.is_paper,
            on_tick=self._on_tick,
            settings=self._settings,
        )
        self._stream.start()
        self._stop_event.clear()
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info(
            "stoploss_stream.started",
            account_id=creds.account_id, accounts=len(self._deps),
        )

    async def stop(self) -> None:
        """동기화 루프 + 스트림 종료."""
        self._stop_event.set()
        if self._sync_task is not None:
            self._sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sync_task
            self._sync_task = None
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None

    # ── Observability (admin read API) ────────────────────────────────

    async def get_status(self) -> dict:
        """어드민 exec-monitor용 읽기 상태 요약 (private 노출 없음)."""
        watched = sorted({p.symbol for p in self._positions})
        return {
            "enabled": bool(self._settings.STOP_LOSS_WS_ENABLED),
            "stream_connected": self._stream is not None,
            "registered_accounts": sorted(self._deps.keys()),
            "watched_symbols": watched,
            "watched_count": len(watched),
            "positions_tracked": len(self._positions),
            "inflight": await self._coordinator.snapshot(),
        }

    # ── Subscription sync ─────────────────────────────────────────────

    async def _sync_loop(self) -> None:
        interval = float(self._settings.PRICE_STREAM_SYNC_INTERVAL_SEC)
        while not self._stop_event.is_set():
            try:
                await self._sync_symbols()
            except Exception:
                logger.exception("stoploss_stream.sync_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)

    async def _sync_symbols(self) -> None:
        """오픈 포지션 스냅샷 갱신 + 구독 종목 합집합 동기화."""
        positions = await self._position_manager.get_open()
        self._positions = [p for p in positions if p.account_id in self._deps]
        symbols = {p.symbol for p in self._positions}
        if self._stream is not None:
            await self._stream.set_symbols(symbols)
        logger.debug("stoploss_stream.synced", symbols=len(symbols))

    # ── Tick evaluation ───────────────────────────────────────────────

    async def _on_tick(self, tick: PriceTick) -> None:
        """체결가 틱 → 해당 종목 보유 포지션(전 계좌) 손절/트레일링 평가."""
        matched = [p for p in self._positions if p.symbol == tick.symbol]
        for position in matched:
            try:
                await self._evaluate(position, tick.price)
            except Exception:
                logger.exception(
                    "stoploss_stream.evaluate_failed",
                    symbol=tick.symbol, position_id=position.id,
                )

    async def _evaluate(self, position: PositionRecord, current_price: Decimal) -> None:
        deps = self._deps.get(position.account_id)
        if deps is None:
            return

        if position.entry_price > _ZERO:
            unrealized_pnl_pct = (
                (current_price - position.entry_price) / position.entry_price * _HUNDRED
            ).quantize(_Q2, rounding=ROUND_HALF_UP)
        else:
            unrealized_pnl_pct = _ZERO

        # 트레일링 고점(high water mark) 갱신
        if position.trailing_stop_pct is not None and (
            position.highest_price is None or current_price > position.highest_price
        ):
            await deps.position_manager.update_highest_price(position.id, current_price)
            position.highest_price = current_price  # 스냅샷 로컬 캐시 동기화

        # 손절 → 익절 → 트레일링 (폴링 잡 job_stop_loss_check와 동일 우선순위).
        # 시간청산(time_based)은 가격 무관이라 5분 폴링에 맡긴다.
        # 트레일링 활성 여부(F-10 B2): 미실현 수익이 전략 임계 이상일 때만 bite.
        # WS는 고빈도라 저장된 trailing_stop_pct(진입 ATR 베이스라인/고정 폭)를 사용하고,
        # POSITION의 ATR 동적 재계산은 5분 폴링(job_stop_loss_check)에 맡긴다.
        trailing_on = (
            position.trailing_stop_pct is not None
            and is_trailing_active(position.strategy_type, unrealized_pnl_pct)
        )

        signal = deps.exit_checker.check_stop_loss(position, current_price, unrealized_pnl_pct)
        if signal is None:
            tp_signal = deps.exit_checker.check_take_profit(
                position, current_price, unrealized_pnl_pct
            )
            if tp_signal is not None and not trailing_on:
                # 트레일링 미설정/비활성 → 익절가 도달 시 매도.
                signal = tp_signal
            elif trailing_on:
                # 트레일링 활성 → 익절가 도달은 즉시 매도가 아니라 고점 추적 계속.
                # 트레일링 스톱가만 평가한다(폴링 잡과 동일).
                baseline = position.highest_price or position.entry_price
                ts_price = trailing_stop_price(
                    position.strategy_type,
                    entry_price=position.entry_price,
                    baseline_high=baseline,
                    stored_pct=position.trailing_stop_pct,
                )
                if ts_price is not None:
                    signal = deps.exit_checker.check_trailing_stop(
                        position, current_price, unrealized_pnl_pct, ts_price,
                    )
        if signal is None:
            return

        # 이중 청산 방지: position_id 선점 성공 시에만 발주.
        if not await self._coordinator.try_claim(position.id):
            logger.debug(
                "stoploss_stream.skip_inflight",
                symbol=position.symbol, position_id=position.id,
            )
            return

        # 재평가 방지: 다음 동기화 전까지 스냅샷에서 제거.
        self._positions = [p for p in self._positions if p.id != position.id]

        session_id = uuid.uuid4()
        try:
            results = await deps.exit_service.process_exit_signals(
                [signal], [position],
                session_id=session_id,
                account_id=position.account_id,
                account_label=deps.account_label,
            )
        except Exception:
            await self._coordinator.release(position.id)
            logger.exception(
                "stoploss_stream.dispatch_failed",
                symbol=position.symbol, position_id=position.id,
            )
            return

        # 발주 실패 시 재시도 가능하도록 클레임 해제(성공 클레임은 TTL까지 보유).
        if not any(r.success for r in results):
            await self._coordinator.release(position.id)
        logger.info(
            "stoploss_stream.exit_triggered",
            symbol=position.symbol,
            position_id=position.id,
            reason=signal.reason.value,
            current_price=str(current_price),
            account_id=position.account_id,
        )


__all__ = ["StopLossStreamService", "AccountExitDeps"]
