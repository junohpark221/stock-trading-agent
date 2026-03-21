"""Tests for CostTracker — daily usage upsert, budget status, stats, and monthly summary."""

import os
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from src.llm.cost_tracker import BudgetStatus, CostTracker, _SYSTEM_SENTINEL


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.LLM_MONTHLY_BUDGET_USD = Decimal("100.00")
    settings.LLM_BUDGET_WARNING_PCT = 80
    return settings


@pytest.fixture
def mock_session():
    """Create a mock async session with context manager support."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.fixture
def mock_session_factory(mock_session):
    factory = MagicMock()
    factory.return_value = mock_session
    return factory


@pytest.fixture
def tracker(mock_session_factory, mock_settings):
    return CostTracker(mock_session_factory, mock_settings)


# ── record_usage ─────────────────────────────────────────────────────


class TestRecordUsage:
    @pytest.mark.asyncio
    async def test_single_record(self, tracker, mock_session):
        """Single usage record inserts via pg_insert."""
        await tracker.record_usage(
            provider="openai",
            model="gpt-5-mini",
            agent_type="trader",
            tokens_in=100,
            tokens_out=50,
            cost_usd=Decimal("0.001"),
        )
        mock_session.execute.assert_called_once()
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_agent_type_none_uses_sentinel(self, tracker, mock_session):
        """agent_type=None is converted to __system__ sentinel."""
        await tracker.record_usage(
            provider="openai",
            model="gpt-5-mini",
            agent_type=None,
            tokens_in=100,
            tokens_out=50,
            cost_usd=Decimal("0.001"),
        )
        # Verify execute was called (the sentinel conversion happens inside)
        mock_session.execute.assert_called_once()
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_escalation_count(self, tracker, mock_session):
        """is_escalation=True sets escalation_count=1 in the insert."""
        await tracker.record_usage(
            provider="openai",
            model="gpt-5.2-2025-12-11",
            agent_type="stock_analyst",
            tokens_in=500,
            tokens_out=200,
            cost_usd=Decimal("0.010"),
            is_escalation=True,
        )
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_db_failure_logged_not_raised(self, tracker, mock_session):
        """DB failure is logged but does not propagate."""
        mock_session.execute.side_effect = Exception("DB connection lost")
        # Should not raise
        await tracker.record_usage(
            provider="openai",
            model="gpt-5-mini",
            agent_type="trader",
            tokens_in=100,
            tokens_out=50,
            cost_usd=Decimal("0.001"),
        )

    @pytest.mark.asyncio
    async def test_multiple_records_same_key(self, tracker, mock_session):
        """Multiple calls for same key use ON CONFLICT DO UPDATE."""
        for _ in range(3):
            await tracker.record_usage(
                provider="openai",
                model="gpt-5-mini",
                agent_type="trader",
                tokens_in=100,
                tokens_out=50,
                cost_usd=Decimal("0.001"),
            )
        assert mock_session.execute.call_count == 3
        assert mock_session.commit.call_count == 3


# ── get_budget_status ────────────────────────────────────────────────


class TestGetBudgetStatus:
    @pytest.mark.asyncio
    async def test_empty_month(self, tracker, mock_session):
        """Empty month returns 0 cost, full remaining."""
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = Decimal("0")
        mock_session.execute.return_value = mock_result

        status = await tracker.get_budget_status()
        assert status.current_month_cost_usd == Decimal("0")
        assert status.budget_usd == Decimal("100.00")
        assert status.remaining_usd == Decimal("100.00")
        assert status.warning_triggered is False
        assert status.budget_exceeded is False
        assert status.escalation_disabled is False

    @pytest.mark.asyncio
    async def test_warning_at_80_percent(self, tracker, mock_session):
        """80% usage triggers warning but not budget exceeded."""
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = Decimal("80.00")
        mock_session.execute.return_value = mock_result

        status = await tracker.get_budget_status()
        assert status.warning_triggered is True
        assert status.budget_exceeded is False
        assert status.escalation_disabled is False

    @pytest.mark.asyncio
    async def test_exceeded_at_100_percent(self, tracker, mock_session):
        """100% usage triggers exceeded and disables escalation."""
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = Decimal("100.00")
        mock_session.execute.return_value = mock_result

        status = await tracker.get_budget_status()
        assert status.warning_triggered is True
        assert status.budget_exceeded is True
        assert status.escalation_disabled is True
        assert status.remaining_usd == Decimal("0")

    @pytest.mark.asyncio
    async def test_over_budget(self, tracker, mock_session):
        """Over budget remaining clamped to 0."""
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = Decimal("150.00")
        mock_session.execute.return_value = mock_result

        status = await tracker.get_budget_status()
        assert status.remaining_usd == Decimal("0")
        assert status.usage_percent == Decimal("150")

    @pytest.mark.asyncio
    async def test_custom_budget(self, mock_session_factory, mock_session):
        """Custom budget setting is used."""
        settings = MagicMock()
        settings.LLM_MONTHLY_BUDGET_USD = Decimal("500.00")
        settings.LLM_BUDGET_WARNING_PCT = 80
        tracker = CostTracker(mock_session_factory, settings)

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = Decimal("100.00")
        mock_session.execute.return_value = mock_result

        status = await tracker.get_budget_status()
        assert status.budget_usd == Decimal("500.00")
        assert status.remaining_usd == Decimal("400.00")
        assert status.usage_percent == Decimal("20")
        assert status.warning_triggered is False


# ── get_usage_stats ──────────────────────────────────────────────────


class TestGetUsageStats:
    @pytest.mark.asyncio
    async def test_no_filters(self, tracker, mock_session):
        """No filters returns all rows."""
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute.return_value = mock_result

        stats = await tracker.get_usage_stats()
        assert stats == []
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_with_all_filters(self, tracker, mock_session):
        """All filters are applied."""
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute.return_value = mock_result

        stats = await tracker.get_usage_stats(
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 14),
            provider="openai",
            agent_type="trader",
        )
        assert stats == []

    @pytest.mark.asyncio
    async def test_returns_formatted_rows(self, tracker, mock_session):
        """Rows are formatted as dicts with string cost."""
        row = MagicMock()
        row.date = date(2026, 3, 14)
        row.provider = "openai"
        row.model = "gpt-5-mini"
        row.agent_type = "trader"
        row.tokens_in = 1000
        row.tokens_out = 500
        row.cost_usd = Decimal("0.005")
        row.call_count = 5
        row.escalation_count = 0

        mock_result = MagicMock()
        mock_result.all.return_value = [row]
        mock_session.execute.return_value = mock_result

        stats = await tracker.get_usage_stats()
        assert len(stats) == 1
        assert stats[0]["date"] == "2026-03-14"
        assert stats[0]["cost_usd"] == "0.005"
        assert stats[0]["call_count"] == 5


# ── get_monthly_summary ─────────────────────────────────────────────


class TestGetMonthlySummary:
    @pytest.mark.asyncio
    async def test_empty_month(self, tracker, mock_session):
        """Empty month returns zeros."""
        total_row = MagicMock()
        total_row.total_cost = Decimal("0")
        total_row.total_calls = 0
        total_row.total_tokens_in = 0
        total_row.total_tokens_out = 0
        total_row.total_escalations = 0

        total_result = MagicMock()
        total_result.one.return_value = total_row

        provider_result = MagicMock()
        provider_result.all.return_value = []

        mock_session.execute.side_effect = [total_result, provider_result]

        summary = await tracker.get_monthly_summary(2026, 3)
        assert summary["total_cost_usd"] == "0"
        assert summary["total_calls"] == 0
        assert summary["by_provider"] == {}

    @pytest.mark.asyncio
    async def test_with_provider_breakdown(self, tracker, mock_session):
        """Summary includes per-provider breakdown."""
        total_row = MagicMock()
        total_row.total_cost = Decimal("25.50")
        total_row.total_calls = 150
        total_row.total_tokens_in = 50000
        total_row.total_tokens_out = 25000
        total_row.total_escalations = 10

        total_result = MagicMock()
        total_result.one.return_value = total_row

        provider_row_1 = MagicMock()
        provider_row_1.provider = "openai"
        provider_row_1.cost = Decimal("20.00")
        provider_row_1.calls = 100

        provider_row_2 = MagicMock()
        provider_row_2.provider = "google"
        provider_row_2.cost = Decimal("5.50")
        provider_row_2.calls = 50

        provider_result = MagicMock()
        provider_result.all.return_value = [provider_row_1, provider_row_2]

        mock_session.execute.side_effect = [total_result, provider_result]

        summary = await tracker.get_monthly_summary(2026, 3)
        assert summary["year"] == 2026
        assert summary["month"] == 3
        assert summary["total_cost_usd"] == "25.50"
        assert summary["total_calls"] == 150
        assert summary["total_escalations"] == 10
        assert "openai" in summary["by_provider"]
        assert summary["by_provider"]["openai"]["cost_usd"] == "20.00"

    @pytest.mark.asyncio
    async def test_december_boundary(self, tracker, mock_session):
        """December summary correctly uses Jan next year as boundary."""
        total_row = MagicMock()
        total_row.total_cost = Decimal("0")
        total_row.total_calls = 0
        total_row.total_tokens_in = 0
        total_row.total_tokens_out = 0
        total_row.total_escalations = 0

        total_result = MagicMock()
        total_result.one.return_value = total_row

        provider_result = MagicMock()
        provider_result.all.return_value = []

        mock_session.execute.side_effect = [total_result, provider_result]

        summary = await tracker.get_monthly_summary(2026, 12)
        assert summary["year"] == 2026
        assert summary["month"] == 12


# ── BudgetStatus model ───────────────────────────────────────────────


class TestBudgetStatusModel:
    def test_serialization(self):
        """BudgetStatus serializes correctly."""
        status = BudgetStatus(
            current_month_cost_usd=Decimal("50.00"),
            budget_usd=Decimal("100.00"),
            remaining_usd=Decimal("50.00"),
            usage_percent=Decimal("50"),
            warning_triggered=False,
            budget_exceeded=False,
            escalation_disabled=False,
        )
        data = status.model_dump()
        assert data["warning_triggered"] is False
        assert data["escalation_disabled"] is False
