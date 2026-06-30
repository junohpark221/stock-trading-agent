"""Tests for ExitCoordinator — 청산 in-flight 가드 (이중 청산 방지, F-05)."""

from __future__ import annotations

import pytest

from src.core.enums import ExitReason
from src.execution.exit_coordinator import (
    PHASE_PARTIAL_TP,
    PHASE_PROTECTIVE,
    ExitCoordinator,
    exit_phase,
)


@pytest.mark.asyncio
async def test_claim_then_second_claim_blocked():
    coord = ExitCoordinator(ttl_sec=100)
    assert await coord.try_claim(7) is True
    assert await coord.try_claim(7) is False  # 이미 선점


@pytest.mark.asyncio
async def test_release_allows_reclaim():
    coord = ExitCoordinator(ttl_sec=100)
    assert await coord.try_claim(7) is True
    await coord.release(7)
    assert await coord.try_claim(7) is True  # 해제 후 재선점 가능


@pytest.mark.asyncio
async def test_distinct_positions_independent():
    coord = ExitCoordinator(ttl_sec=100)
    assert await coord.try_claim(1) is True
    assert await coord.try_claim(2) is True  # 다른 포지션은 독립


@pytest.mark.asyncio
async def test_ttl_expiry_auto_releases():
    coord = ExitCoordinator(ttl_sec=0)  # 즉시 만료
    assert await coord.try_claim(7) is True
    # TTL=0이므로 다음 시도 시 prune되어 재선점 가능
    assert await coord.try_claim(7) is True


@pytest.mark.asyncio
async def test_is_claimed_reflects_state():
    coord = ExitCoordinator(ttl_sec=100)
    assert await coord.is_claimed(7) is False
    await coord.try_claim(7)
    assert await coord.is_claimed(7) is True
    await coord.release(7)
    assert await coord.is_claimed(7) is False


@pytest.mark.asyncio
async def test_release_unknown_is_noop():
    coord = ExitCoordinator(ttl_sec=100)
    await coord.release(999)  # 미선점 해제는 무해
    assert await coord.try_claim(999) is True


# ── F-10 Phase 2: (position_id, phase) 복합키 ──────────────────────────


def test_exit_phase_mapping():
    assert exit_phase(ExitReason.PARTIAL_TAKE_PROFIT) == PHASE_PARTIAL_TP
    assert exit_phase(ExitReason.STOP_LOSS) == PHASE_PROTECTIVE
    assert exit_phase(ExitReason.TRAILING_STOP) == PHASE_PROTECTIVE
    assert exit_phase(ExitReason.TAKE_PROFIT) == PHASE_PROTECTIVE


@pytest.mark.asyncio
async def test_partial_and_protective_phases_independent():
    """같은 포지션이라도 partial_tp/protective 레그는 독립 선점된다."""
    coord = ExitCoordinator(ttl_sec=100)
    assert await coord.try_claim(7, PHASE_PARTIAL_TP) is True
    # 부분익절이 in-flight여도 보호(손절/트레일) 레그는 막히지 않음
    assert await coord.try_claim(7, PHASE_PROTECTIVE) is True


@pytest.mark.asyncio
async def test_same_phase_still_dedups():
    """같은 위상 중복은 그대로 차단(dedup 유지)."""
    coord = ExitCoordinator(ttl_sec=100)
    assert await coord.try_claim(7, PHASE_PARTIAL_TP) is True
    assert await coord.try_claim(7, PHASE_PARTIAL_TP) is False


@pytest.mark.asyncio
async def test_release_phase_specific():
    """해제는 위상별로 동작한다."""
    coord = ExitCoordinator(ttl_sec=100)
    await coord.try_claim(7, PHASE_PARTIAL_TP)
    await coord.try_claim(7, PHASE_PROTECTIVE)
    await coord.release(7, PHASE_PARTIAL_TP)
    assert await coord.is_claimed(7, PHASE_PARTIAL_TP) is False
    assert await coord.is_claimed(7, PHASE_PROTECTIVE) is True


@pytest.mark.asyncio
async def test_snapshot_includes_phase():
    coord = ExitCoordinator(ttl_sec=100)
    await coord.try_claim(7, PHASE_PARTIAL_TP)
    snap = await coord.snapshot()
    assert snap == [{"position_id": 7, "phase": PHASE_PARTIAL_TP, "age_sec": snap[0]["age_sec"]}]
