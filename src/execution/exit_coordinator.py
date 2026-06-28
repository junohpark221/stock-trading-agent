"""ExitCoordinator — 청산 in-flight 가드 (이중 청산 방지).

손절/트레일링 청산은 두 경로에서 트리거될 수 있다:
- 5분 폴링 잡(``job_stop_loss_check``)
- 실시간 체결가 WS(``StopLossStreamService``)

또한 폴링은 사이클 간(직전 청산 주문이 미체결인 동안)에도 같은 포지션을 다시 청산할
여지가 있다. 포지션 상태는 ``open``/``closed`` 뿐이고 중간 "청산 중" 상태가 없어,
두 경로(혹은 폴링 사이클 간)가 같은 ``position_id`` 에 대해 동시에 청산 주문을 낼 수 있다
(오버셀 위험).

앱은 **단일 프로세스**(web+scheduler+WS 한 이벤트루프)이므로 in-process 가드로 충분하다.
``try_claim`` 으로 ``position_id`` 를 선점한 경로만 청산을 진행하고, 나머지는 스킵한다.
청산이 실패하면 즉시 ``release`` 해 다음 사이클이 재시도하도록 하고, 성공(접수/체결)한
클레임은 TTL 만료까지 보유해 미체결 청산 주문이 도는 동안의 중복 발주를 막는다.
"""

from __future__ import annotations

import asyncio
import time

import structlog

logger = structlog.get_logger(__name__)


class ExitCoordinator:
    """포지션 청산 in-flight 가드. ``position_id`` 단위 선점/해제.

    Parameters
    ----------
    ttl_sec: 클레임 자동 만료 시간(초). 미체결 청산이 영구 점유로 남지 않도록 하는
        안전망. 이 시간 안에 청산 주문이 체결되어 포지션이 닫히면, 이후 폴링/WS는
        닫힌 포지션을 보지 못해 재청산하지 않는다.
    """

    def __init__(self, *, ttl_sec: float = 120.0) -> None:
        self._ttl = float(ttl_sec)
        self._inflight: dict[int, float] = {}  # position_id → claim monotonic ts
        self._lock = asyncio.Lock()

    async def try_claim(self, position_id: int) -> bool:
        """``position_id`` 를 선점. 미점유면 True(진행), 이미 점유면 False(스킵)."""
        async with self._lock:
            self._prune_locked()
            if position_id in self._inflight:
                return False
            self._inflight[position_id] = time.monotonic()
            return True

    async def release(self, position_id: int) -> None:
        """클레임 해제. 청산 실패로 재시도가 필요할 때 호출."""
        async with self._lock:
            self._inflight.pop(position_id, None)

    async def is_claimed(self, position_id: int) -> bool:
        """현재 in-flight 여부(만료 반영). 테스트/관찰용."""
        async with self._lock:
            self._prune_locked()
            return position_id in self._inflight

    def _prune_locked(self) -> None:
        """TTL 만료 클레임 정리. 호출자가 락을 보유한 상태에서만 호출."""
        if not self._inflight:
            return
        now = time.monotonic()
        expired = [pid for pid, ts in self._inflight.items() if now - ts >= self._ttl]
        for pid in expired:
            del self._inflight[pid]
        if expired:
            logger.debug("exit_coordinator.claims_expired", count=len(expired))


__all__ = ["ExitCoordinator"]
