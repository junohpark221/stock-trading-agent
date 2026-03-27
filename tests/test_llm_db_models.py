"""Tests for LLM agent DB models: DecisionLog, AgentModelConfigDB, LLMUsage."""

import uuid

import pytest
from sqlalchemy import inspect

from src.db.models.llm import AgentModelConfigDB, DecisionLog, LLMUsage


# ── Helpers ──────────────────────────────────────────────────────────


def _col_map(model):
    """Return {col_name: Column} dict from ORM mapper."""
    mapper = inspect(model)
    return {c.key: c for c in mapper.columns}


def _table(model):
    """Return the underlying Table object."""
    return model.__table__


def _unique_constraint_names(model):
    """Return set of unique constraint names."""
    return {c.name for c in _table(model).constraints if hasattr(c, "columns") and len(c.columns) > 0 and c.name and c.name.startswith("uq_")}


def _index_names(model):
    """Return set of index names."""
    return {idx.name for idx in _table(model).indexes}


# ── TestDecisionLogModel ─────────────────────────────────────────────


class TestDecisionLogModel:
    """decision_log 테이블 ORM 메타데이터 검증."""

    def test_tablename(self):
        assert DecisionLog.__tablename__ == "decision_log"

    def test_column_count(self):
        """24 columns: 21 data + account_id + created_at + updated_at."""
        cols = _col_map(DecisionLog)
        assert len(cols) == 24

    def test_primary_key(self):
        pk_cols = [c.name for c in _table(DecisionLog).primary_key.columns]
        assert pk_cols == ["id"]

    def test_id_is_biginteger(self):
        col = _table(DecisionLog).c.id
        assert col.autoincrement is True

    def test_decision_id_uuid_type(self):
        col = _table(DecisionLog).c.decision_id
        assert col.nullable is False
        assert "UUID" in str(col.type).upper()

    def test_parent_id_nullable(self):
        col = _table(DecisionLog).c.parent_id
        assert col.nullable is True

    def test_session_id_not_nullable(self):
        col = _table(DecisionLog).c.session_id
        assert col.nullable is False

    def test_unique_decision_id(self):
        names = _unique_constraint_names(DecisionLog)
        assert "uq_decision_log_decision_id" in names

    def test_indexes(self):
        names = _index_names(DecisionLog)
        assert "ix_decision_log_session_id" in names
        assert "ix_decision_log_symbol" in names
        assert "ix_decision_log_stage" in names

    def test_stage_not_nullable(self):
        col = _table(DecisionLog).c.stage
        assert col.nullable is False
        assert col.type.length == 30

    def test_decision_not_nullable(self):
        col = _table(DecisionLog).c.decision
        assert col.nullable is False

    def test_reasoning_not_nullable(self):
        col = _table(DecisionLog).c.reasoning
        assert col.nullable is False

    def test_llm_fields_nullable(self):
        table = _table(DecisionLog)
        for name in ("llm_provider", "llm_model", "llm_prompt", "llm_response",
                      "llm_tokens_in", "llm_tokens_out", "llm_cost_usd"):
            assert table.c[name].nullable is True, f"{name} should be nullable"

    def test_confidence_precision(self):
        col = _table(DecisionLog).c.confidence
        assert col.type.precision == 5
        assert col.type.scale == 4

    def test_data_snapshot_jsonb(self):
        col = _table(DecisionLog).c.data_snapshot
        assert "JSONB" in str(col.type).upper()

    def test_outcome_fields_nullable(self):
        table = _table(DecisionLog)
        for name in ("outcome", "outcome_pnl", "outcome_note"):
            assert table.c[name].nullable is True, f"{name} should be nullable"


# ── TestAgentModelConfigDBModel ──────────────────────────────────────


class TestAgentModelConfigDBModel:
    """agent_model_config 테이블 ORM 메타데이터 검증."""

    def test_tablename(self):
        assert AgentModelConfigDB.__tablename__ == "agent_model_config"

    def test_column_count(self):
        """10 columns: 8 data + created_at + updated_at."""
        cols = _col_map(AgentModelConfigDB)
        assert len(cols) == 10

    def test_primary_key_integer(self):
        col = _table(AgentModelConfigDB).c.id
        pk_cols = [c.name for c in _table(AgentModelConfigDB).primary_key.columns]
        assert pk_cols == ["id"]
        # Integer PK, not BigInteger
        assert "INTEGER" in str(col.type).upper()

    def test_unique_agent_type(self):
        names = _unique_constraint_names(AgentModelConfigDB)
        assert "uq_agent_model_config_agent_type" in names

    def test_agent_type_string_length(self):
        col = _table(AgentModelConfigDB).c.agent_type
        assert col.type.length == 50
        assert col.nullable is False

    def test_routing_mode_not_nullable(self):
        col = _table(AgentModelConfigDB).c.routing_mode
        assert col.nullable is False
        assert col.type.length == 20

    def test_is_active_boolean_default(self):
        col = _table(AgentModelConfigDB).c.is_active
        assert col.nullable is False

    def test_updated_by_default(self):
        col = _table(AgentModelConfigDB).c.updated_by
        assert col.nullable is False
        assert col.type.length == 50

    def test_escalation_model_nullable(self):
        col = _table(AgentModelConfigDB).c.escalation_model
        assert col.nullable is True

    def test_confidence_threshold_precision(self):
        col = _table(AgentModelConfigDB).c.confidence_threshold
        assert col.type.precision == 5
        assert col.type.scale == 4
        assert col.nullable is True


# ── TestLLMUsageModel ────────────────────────────────────────────────


class TestLLMUsageModel:
    """llm_usage 테이블 ORM 메타데이터 검증."""

    def test_tablename(self):
        assert LLMUsage.__tablename__ == "llm_usage"

    def test_column_count(self):
        """12 columns: 10 data + created_at + updated_at."""
        cols = _col_map(LLMUsage)
        assert len(cols) == 12

    def test_unique_daily(self):
        names = _unique_constraint_names(LLMUsage)
        assert "uq_llm_usage_daily" in names

    def test_indexes(self):
        names = _index_names(LLMUsage)
        assert "ix_llm_usage_date" in names
        assert "ix_llm_usage_provider" in names

    def test_agent_type_nullable(self):
        """agent_type NULL = 시스템 호출."""
        col = _table(LLMUsage).c.agent_type
        assert col.nullable is True

    def test_counter_fields_not_nullable(self):
        table = _table(LLMUsage)
        for name in ("tokens_in", "tokens_out", "cost_usd", "call_count", "escalation_count"):
            assert table.c[name].nullable is False, f"{name} should be NOT NULL"

    def test_cost_usd_precision(self):
        col = _table(LLMUsage).c.cost_usd
        assert col.type.precision == 12
        assert col.type.scale == 6

    def test_date_not_nullable(self):
        col = _table(LLMUsage).c.date
        assert col.nullable is False

    def test_provider_string_length(self):
        col = _table(LLMUsage).c.provider
        assert col.type.length == 20
        assert col.nullable is False


# ── TestModelsRegistered ─────────────────────────────────────────────


class TestModelsRegistered:
    """패키지 import 및 __all__ 검증."""

    def test_all_exports(self):
        from src.db.models import __all__

        assert "DecisionLog" in __all__
        assert "AgentModelConfigDB" in __all__
        assert "LLMUsage" in __all__

    def test_package_import(self):
        from src.db.models import AgentModelConfigDB, DecisionLog, LLMUsage

        assert DecisionLog.__tablename__ == "decision_log"
        assert AgentModelConfigDB.__tablename__ == "agent_model_config"
        assert LLMUsage.__tablename__ == "llm_usage"
