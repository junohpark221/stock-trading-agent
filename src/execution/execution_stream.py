"""ExecutionStreamManager — KIS 체결통보 WS 이벤트 디스패처.

계정별 ``KISExecutionStream`` 을 기동/종료하고, 수신된 ``ExecutionEvent`` 를
다음 경로 중 하나로 라우팅한다:

1. **대기 중인 OrderExecutor 코루틴이 있으면** → 해당 Future에 결과 공급.
   OrderExecutor가 사후 처리(체결기록·포지션생성·알림)를 담당한다.
2. **대기자가 없으면** → DB 기반 fallback finalize. OrderReconciler와 동일한
   ``FillFinalizer`` 를 사용한다. 앱 재시작/타이밍 race 시에만 발생.

TR ID는 H0STCNI0 (실전) / H0STCNI9 (모의). 계정당 1 WS, 공용 접속.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from src.broker.kis.ws_execution import KISExecutionStream

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.broker.credentials import AccountCredentials
    from src.config import Settings
    from src.core.models import ExecutionEvent
    from src.execution.fill_finalizer import FillFinalizer

logger = structlog.get_logger(__name__)


class ExecutionStreamManager:
    """Per-account WS 체결통보 디스패처 + pending order 대기자 등록소.

    Parameters
    ----------
    settings: Settings
    session_factory: DB 세션 팩토리 (fallback finalize에 전달)
    fill_finalizer: WS 이벤트를 DB에 반영하는 공용 서비스 (대기자 없을 때 fallback)
    """

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        fill_finalizer: FillFinalizer,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._fill_finalizer = fill_finalizer

        # broker_order_id → {"event": ExecutionEvent|None, "signal": asyncio.Event}
        self._waiters: dict[str, dict[str, object]] = {}
        self._lock = asyncio.Lock()
        self._streams: dict[str, KISExecutionStream] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self, credentials_list: list[AccountCredentials]) -> None:
        """Spin up one WS subscription per credential entry."""
        if not self._settings.EXECUTION_STREAM_ENABLED:
            logger.info("execution_stream.disabled_by_config")
            return

        for creds in credentials_list:
            if not creds.hts_id:
                logger.warning(
                    "execution_stream.skip_missing_hts_id",
                    account_id=creds.account_id,
                )
                continue
            try:
                stream = KISExecutionStream(
                    account_id=creds.account_id,
                    app_key=creds.app_key,
                    app_secret=creds.app_secret,
                    hts_id=creds.hts_id,
                    is_paper=creds.is_paper,
                    on_event=self._on_event,
                    settings=self._settings,
                )
                stream.start()
                self._streams[creds.account_id] = stream
                logger.info(
                    "execution_stream.account_started",
                    account_id=creds.account_id,
                    tr_id=stream._tr_id,
                )
            except Exception:
                logger.exception(
                    "execution_stream.account_start_failed",
                    account_id=creds.account_id,
                )

    async def stop(self) -> None:
        """Stop all per-account subscriptions."""
        for account_id, stream in list(self._streams.items()):
            try:
                await stream.stop()
            except Exception:
                logger.exception(
                    "execution_stream.account_stop_failed",
                    account_id=account_id,
                )
        self._streams.clear()
        # Release any lingering waiters so callers don't hang.
        async with self._lock:
            for entry in self._waiters.values():
                sig = entry.get("signal")
                if isinstance(sig, asyncio.Event):
                    sig.set()
            self._waiters.clear()

    # ── Waiter registration ───────────────────────────────────────────

    async def wait_for_fill(
        self,
        *,
        broker_order_id: str,
        account_id: str,
        timeout: float,
    ) -> ExecutionEvent:
        """Suspend until a fill/reject event for *broker_order_id* arrives.

        Raises:
            asyncio.TimeoutError: if no event arrives within *timeout* seconds.
        """
        from src.core.models import ExecutionEvent  # local import

        async with self._lock:
            entry = self._waiters.get(broker_order_id)
            if entry is None:
                entry = {"event": None, "signal": asyncio.Event()}
                self._waiters[broker_order_id] = entry
            elif isinstance(entry.get("event"), ExecutionEvent):
                # Event arrived before waiter registered — pop + return
                event = entry["event"]
                self._waiters.pop(broker_order_id, None)
                return event  # type: ignore[return-value]

        signal = entry["signal"]
        assert isinstance(signal, asyncio.Event)
        try:
            await asyncio.wait_for(signal.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            async with self._lock:
                self._waiters.pop(broker_order_id, None)
            raise

        async with self._lock:
            final_entry = self._waiters.pop(broker_order_id, None)
        if final_entry is None:
            raise asyncio.TimeoutError(
                f"waiter entry disappeared for {broker_order_id}",
            )
        event = final_entry.get("event")
        if not isinstance(event, ExecutionEvent):
            raise asyncio.TimeoutError(
                f"no event delivered for {broker_order_id}",
            )
        return event

    # ── Event routing ─────────────────────────────────────────────────

    async def _on_event(self, event: ExecutionEvent) -> None:
        """Primary entry point invoked by KISExecutionStream."""
        async with self._lock:
            entry = self._waiters.get(event.broker_order_id)
            if entry is None:
                # No waiter — buffer for a short grace, else DB fallback.
                self._waiters[event.broker_order_id] = {
                    "event": event,
                    "signal": asyncio.Event(),
                }
                has_waiter = False
            else:
                entry["event"] = event
                sig = entry.get("signal")
                if isinstance(sig, asyncio.Event):
                    sig.set()
                has_waiter = True

        if has_waiter:
            return

        # Stash for a brief grace window so an in-flight executor can still
        # pick it up before we write to the DB. 2s is long enough to cover
        # round-trip scheduling but short enough to keep UX snappy.
        asyncio.create_task(self._late_finalize(event))

    async def _late_finalize(self, event: ExecutionEvent) -> None:
        await asyncio.sleep(2.0)
        async with self._lock:
            entry = self._waiters.get(event.broker_order_id)
            # If the executor picked up the buffered event, the entry is either
            # gone or its signal was set — bail out.
            if entry is None:
                return
            sig = entry.get("signal")
            if isinstance(sig, asyncio.Event) and sig.is_set():
                return
            # Still orphaned — claim it and run DB fallback.
            self._waiters.pop(event.broker_order_id, None)

        try:
            await self._fill_finalizer.finalize_from_event(event)
        except Exception:
            logger.exception(
                "execution_stream.fallback_finalize_failed",
                broker_order_id=event.broker_order_id,
                account_id=event.account_id,
            )


__all__ = ["ExecutionStreamManager"]
