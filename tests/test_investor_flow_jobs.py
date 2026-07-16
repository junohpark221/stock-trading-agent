"""PRJ-03 수급 수집 잡 단위 테스트.

job_investor_flow_collect / job_short_interest_collect — 휴장 가드·콜렉터 위임.
콜렉터는 전부 목 — 실호출 없음.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from src.core.time import KST
from src.data.collector import CollectionSummary
from src.scheduler.jobs import job_investor_flow_collect, job_short_interest_collect


def _today_kst_iso() -> str:
    return datetime.now(KST).date().isoformat()


def _summary(**kwargs) -> CollectionSummary:
    defaults = {"total_symbols": 2, "succeeded": 2, "failed": 0, "total_rows": 60}
    return CollectionSummary(**{**defaults, **kwargs})


class TestJobInvestorFlowCollect:
    @pytest.mark.asyncio
    async def test_holiday_skips_collectors(self):
        provider = AsyncMock()
        with (
            patch(
                "src.scheduler.jobs.collect_market_investor_flow", new_callable=AsyncMock
            ) as market_mock,
            patch(
                "src.scheduler.jobs.collect_investor_flow", new_callable=AsyncMock
            ) as flow_mock,
        ):
            await job_investor_flow_collect(
                provider=provider,
                symbols=["005930"],
                holidays=f"2026-01-01,{_today_kst_iso()}",
            )

        market_mock.assert_not_awaited()
        flow_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_market_then_symbols(self):
        provider = AsyncMock()
        with (
            patch(
                "src.scheduler.jobs.collect_market_investor_flow",
                new_callable=AsyncMock,
                return_value=_summary(total_symbols=2, total_rows=600),
            ) as market_mock,
            patch(
                "src.scheduler.jobs.collect_investor_flow",
                new_callable=AsyncMock,
                return_value=_summary(),
            ) as flow_mock,
        ):
            await job_investor_flow_collect(
                provider=provider, symbols=["005930", "000660"], holidays=""
            )

        market_mock.assert_awaited_once_with(provider)
        flow_mock.assert_awaited_once_with(provider, ["005930", "000660"])


class TestJobShortInterestCollect:
    @pytest.mark.asyncio
    async def test_holiday_skips_collector(self):
        provider = AsyncMock()
        with patch(
            "src.scheduler.jobs.collect_short_interest", new_callable=AsyncMock
        ) as si_mock:
            await job_short_interest_collect(
                provider=provider,
                symbols=["005930"],
                holidays=_today_kst_iso(),
            )

        si_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_with_window_days_forwarded(self):
        provider = AsyncMock()
        with patch(
            "src.scheduler.jobs.collect_short_interest",
            new_callable=AsyncMock,
            return_value=_summary(),
        ) as si_mock:
            await job_short_interest_collect(
                provider=provider,
                symbols=["005930"],
                holidays="2026-01-01",
                window_days=7,
            )

        si_mock.assert_awaited_once_with(provider, ["005930"], window_days=7)
