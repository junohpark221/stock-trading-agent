"""Phase 6 Step 2: JobExecution ORM 모델 + 마이그레이션 테스트.

Tests:
- JobExecution ORM 모델 생성/기본값/인덱스
- __init__.py exports 확인
- 마이그레이션 체인 검증
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Index

from src.db.models import JobExecution

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _get_index_names(model):
    """__table_args__ 튜플에서 Index 이름 목록 추출."""
    return [arg.name for arg in model.__table_args__ if isinstance(arg, Index)]


# ---------------------------------------------------------------------------
# JobExecution Tests
# ---------------------------------------------------------------------------


class TestJobExecution:
    """JobExecution ORM 모델 테스트."""

    def test_tablename(self):
        assert JobExecution.__tablename__ == "job_executions"

    def test_create_with_all_fields(self):
        now = datetime.now(tz=UTC)
        rec = JobExecution(
            job_name="daily_report",
            status="success",
            started_at=now,
            finished_at=now,
            duration_sec=Decimal("1.234"),
            error_message="",
            result_summary="Report sent",
        )
        assert rec.job_name == "daily_report"
        assert rec.status == "success"
        assert rec.started_at == now
        assert rec.finished_at == now
        assert rec.duration_sec == Decimal("1.234")
        assert rec.error_message == ""
        assert rec.result_summary == "Report sent"

    def test_default_status(self):
        now = datetime.now(tz=UTC)
        rec = JobExecution(job_name="token_refresh", started_at=now)
        assert rec.status is None  # Python default not applied without flush
        # Column default="running" is applied at DB level

    def test_default_error_message(self):
        now = datetime.now(tz=UTC)
        rec = JobExecution(job_name="test", status="running", started_at=now)
        assert rec.error_message is None  # Python default not applied without flush

    def test_default_result_summary(self):
        now = datetime.now(tz=UTC)
        rec = JobExecution(job_name="test", status="running", started_at=now)
        assert rec.result_summary is None  # Python default not applied without flush

    def test_optional_finished_at(self):
        now = datetime.now(tz=UTC)
        rec = JobExecution(
            job_name="stop_loss_check",
            status="running",
            started_at=now,
        )
        assert rec.finished_at is None

    def test_optional_duration_sec(self):
        now = datetime.now(tz=UTC)
        rec = JobExecution(
            job_name="stop_loss_check",
            status="running",
            started_at=now,
        )
        assert rec.duration_sec is None

    def test_status_update(self):
        now = datetime.now(tz=UTC)
        rec = JobExecution(
            job_name="swing_analysis",
            status="running",
            started_at=now,
        )
        assert rec.status == "running"

        rec.status = "success"
        rec.finished_at = datetime.now(tz=UTC)
        rec.duration_sec = Decimal("45.678")
        assert rec.status == "success"
        assert rec.duration_sec == Decimal("45.678")

    def test_failed_status_with_error(self):
        now = datetime.now(tz=UTC)
        rec = JobExecution(
            job_name="market_data_collect",
            status="failed",
            started_at=now,
            finished_at=now,
            duration_sec=Decimal("0.500"),
            error_message="Connection timeout",
        )
        assert rec.status == "failed"
        assert rec.error_message == "Connection timeout"

    def test_skipped_status(self):
        now = datetime.now(tz=UTC)
        rec = JobExecution(
            job_name="position_analysis",
            status="skipped",
            started_at=now,
            finished_at=now,
            duration_sec=Decimal("0.001"),
            result_summary="Market closed",
        )
        assert rec.status == "skipped"
        assert rec.result_summary == "Market closed"


# ---------------------------------------------------------------------------
# Index Tests
# ---------------------------------------------------------------------------


class TestJobExecutionIndexes:
    """JobExecution 인덱스 테스트."""

    def test_has_three_indexes(self):
        index_names = _get_index_names(JobExecution)
        assert len(index_names) == 4

    def test_name_started_composite_index(self):
        index_names = _get_index_names(JobExecution)
        assert "ix_job_exec_name_started" in index_names

    def test_status_index(self):
        index_names = _get_index_names(JobExecution)
        assert "ix_job_exec_status" in index_names

    def test_started_at_index(self):
        index_names = _get_index_names(JobExecution)
        assert "ix_job_exec_started_at" in index_names


# ---------------------------------------------------------------------------
# __init__.py Export Tests
# ---------------------------------------------------------------------------


class TestInitExports:
    """src.db.models 패키지 export 확인."""

    def test_job_execution_importable(self):
        from src.db.models import JobExecution as JE

        assert JE is JobExecution

    def test_in_all(self):
        import src.db.models as models

        assert "JobExecution" in models.__all__


# ---------------------------------------------------------------------------
# Migration Chain Tests
# ---------------------------------------------------------------------------


class TestMigrationChain:
    """마이그레이션 체인 검증."""

    @pytest.fixture()
    def migration_module(self):
        import importlib.util
        from pathlib import Path

        migration_path = (
            Path(__file__).parent.parent / "alembic" / "versions" / "007_add_scheduler_tables.py"
        )
        spec = importlib.util.spec_from_file_location("migration_007", migration_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_revision_id(self, migration_module):
        assert migration_module.revision == "007_scheduler"
        assert migration_module.down_revision == "006_execution"

    def test_upgrade_downgrade_defined(self, migration_module):
        assert callable(migration_module.upgrade)
        assert callable(migration_module.downgrade)
