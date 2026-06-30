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

from src.core.enums import ExitReason

logger = structlog.get_logger(__name__)

# 청산 위상(phase) — 같은 포지션의 부분익절 레그와 보호(손절/트레일/시간/전량익절)
# 레그가 코디네이터에서 서로를 차단하지 않도록 분리하는 키 구성요소.
PHASE_PARTIAL_TP = "partial_tp"
PHASE_PROTECTIVE = "protective"


def exit_phase(reason: ExitReason) -> str:
    """청산 사유 → 코디네이터 위상(F-10 Phase 2 복합키).

    부분익절(``PARTIAL_TAKE_PROFIT``)은 ``partial_tp`` 레그, 그 외(손절/트레일/시간/
    전량익절)는 ``protective`` 레그로 묶는다. 같은 포지션이라도 두 레그는 독립 claim이라,
    부분익절이 in-flight인 동안에도 손절/트레일링이 막히지 않는다(WS 손절 지연 방지).
    """
    return PHASE_PARTIAL_TP if reason == ExitReason.PARTIAL_TAKE_PROFIT else PHASE_PROTECTIVE


class ExitCoordinator:
    """포지션 청산 in-flight 가드. ``(position_id, phase)`` 단위 선점/해제.

    Parameters
    ----------
    ttl_sec: 클레임 자동 만료 시간(초). 미체결 청산이 영구 점유로 남지 않도록 하는
        안전망. 이 시간 안에 청산 주문이 체결되어 포지션이 닫히면, 이후 폴링/WS는
        닫힌 포지션을 보지 못해 재청산하지 않는다.

    Notes
    -----
    키는 ``(position_id, phase)`` 복합키(F-10 Phase 2). ``phase`` 기본값은
    ``protective`` 이라, 위상을 명시하지 않는 기존 호출은 단일 보호 레그로 동작한다.
    """

    def __init__(self, *, ttl_sec: float = 120.0) -> None:
        self._ttl = float(ttl_sec)
        # (position_id, phase) → claim monotonic ts
        self._inflight: dict[tuple[int, str], float] = {}
        self._lock = asyncio.Lock()

    async def try_claim(self, position_id: int, phase: str = PHASE_PROTECTIVE) -> bool:
        """``(position_id, phase)`` 를 선점. 미점유면 True(진행), 점유면 False(스킵)."""
        key = (position_id, phase)
        async with self._lock:
            self._prune_locked()
            if key in self._inflight:
                return False
            self._inflight[key] = time.monotonic()
            return True

    async def release(self, position_id: int, phase: str = PHASE_PROTECTIVE) -> None:
        """클레임 해제. 청산 실패로 재시도가 필요할 때 호출."""
        async with self._lock:
            self._inflight.pop((position_id, phase), None)

    async def is_claimed(self, position_id: int, phase: str = PHASE_PROTECTIVE) -> bool:
        """현재 in-flight 여부(만료 반영). 테스트/관찰용."""
        async with self._lock:
            self._prune_locked()
            return (position_id, phase) in self._inflight

    async def snapshot(self) -> list[dict]:
        """현재 in-flight 클레임 목록(만료 정리 후). 어드민 관측용 read API.

        private ``_inflight`` 직접 노출 대신 ``[{position_id, phase, age_sec}]`` 요약 반환.
        """
        async with self._lock:
            self._prune_locked()
            now = time.monotonic()
            return [
                {"position_id": pid, "phase": phase, "age_sec": round(now - ts, 1)}
                for (pid, phase), ts in sorted(self._inflight.items())
            ]

    def _prune_locked(self) -> None:
        """TTL 만료 클레임 정리. 호출자가 락을 보유한 상태에서만 호출."""
        if not self._inflight:
            return
        now = time.monotonic()
        expired = [key for key, ts in self._inflight.items() if now - ts >= self._ttl]
        for key in expired:
            del self._inflight[key]
        if expired:
            logger.debug("exit_coordinator.claims_expired", count=len(expired))


__all__ = ["ExitCoordinator", "exit_phase", "PHASE_PARTIAL_TP", "PHASE_PROTECTIVE"]
