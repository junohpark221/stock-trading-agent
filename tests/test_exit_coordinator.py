"""Tests for ExitCoordinator — 청산 in-flight 가드 (이중 청산 방지, F-05)."""

from __future__ import annotations

import pytest

from src.execution.exit_coordinator import ExitCoordinator


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
