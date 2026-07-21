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


class TestJobInvestorFlowRevisionAlert:
    """단계 5: 리비전 감지 텔레그램 경보 — 임계 이상 발송 / 미만·비활성 미발송."""

    async def _run(self, *, flagged: int, threshold: int, telegram_bot):
        with (
            patch(
                "src.scheduler.jobs.collect_market_investor_flow",
                new_callable=AsyncMock,
                return_value=_summary(revision_rows=0, cross_source_rows=0),
            ),
            patch(
                "src.scheduler.jobs.collect_investor_flow",
                new_callable=AsyncMock,
                return_value=_summary(
                    revision_rows=flagged, cross_source_rows=0, mismatched_cells=flagged
                ),
            ),
        ):
            await job_investor_flow_collect(
                provider=AsyncMock(),
                symbols=["005930"],
                holidays="",
                telegram_bot=telegram_bot,
                revision_alert_threshold=threshold,
            )

    @pytest.mark.asyncio
    async def test_alert_sent_at_threshold(self):
        bot = AsyncMock()
        await self._run(flagged=10, threshold=10, telegram_bot=bot)
        bot.send_message.assert_awaited_once()
        assert "수급 데이터 리비전 감지" in bot.send_message.call_args.args[0]

    @pytest.mark.asyncio
    async def test_no_alert_below_threshold(self):
        bot = AsyncMock()
        await self._run(flagged=9, threshold=10, telegram_bot=bot)
        bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_threshold_zero_disables_alert(self):
        bot = AsyncMock()
        await self._run(flagged=100, threshold=0, telegram_bot=bot)
        bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_bot_no_crash(self):
        await self._run(flagged=100, threshold=10, telegram_bot=None)


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
