"""DecisionRecorder 단위 테스트 (~15개).

A. record — 기본 삽입 (5개)
B. update_outcome (3개)
C. get_session_decisions (2개)
D. get_decision_chain (3개)
E. 에러 처리 (1개)
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.decision_recorder import DecisionRecorder
from src.core.exceptions import DatabaseError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_session_factory(
    *,
    execute_return: MagicMock | None = None,
    raise_on_commit: Exception | None = None,
    raise_on_execute: Exception | None = None,
) -> MagicMock:
    """Create a mock async_sessionmaker for DecisionRecorder tests."""
    session = AsyncMock()

    if raise_on_execute:
        session.execute = AsyncMock(side_effect=raise_on_execute)
    elif execute_return is not None:
        session.execute = AsyncMock(return_value=execute_return)
    else:
        session.execute = AsyncMock(return_value=MagicMock())

    if raise_on_commit:
        session.commit = AsyncMock(side_effect=raise_on_commit)
    else:
        session.commit = AsyncMock()

    session.add = MagicMock()

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=ctx)
    factory._session = session  # expose for assertions
    return factory


def _make_decision_row(
    decision_id: uuid.UUID,
    session_id: uuid.UUID,
    parent_id: uuid.UUID | None = None,
    stage: str = "stock_analysis",
    decision: str = "BUY",
    reasoning: str = "test",
) -> MagicMock:
    """Create a mock DecisionLog row."""
    row = MagicMock()
    row.decision_id = decision_id
    row.session_id = session_id
    row.parent_id = parent_id
    row.stage = stage
    row.decision = decision
    row.reasoning = reasoning
    row.created_at = None
    return row


# ===========================================================================
# A. record — 기본 삽입
# ===========================================================================


class TestRecord:
    """DecisionRecorder.record() 테스트."""

    @pytest.mark.asyncio
    async def test_record_returns_uuid(self) -> None:
        factory = _make_mock_session_factory()
        recorder = DecisionRecorder(factory)

        sid = uuid.uuid4()
        result = await recorder.record(
            session_id=sid,
            stage="market_analysis",
            decision="HOLD",
            reasoning="test reasoning",
        )
        assert isinstance(result, uuid.UUID)
        factory._session.add.assert_called_once()
        factory._session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_record_minimal_fields(self) -> None:
        factory = _make_mock_session_factory()
        recorder = DecisionRecorder(factory)

        result = await recorder.record(
            session_id=uuid.uuid4(),
            stage="risk_check",
            decision="APPROVE",
            reasoning="minimal",
        )
        assert isinstance(result, uuid.UUID)

    @pytest.mark.asyncio
    async def test_record_all_fields(self) -> None:
        factory = _make_mock_session_factory()
        recorder = DecisionRecorder(factory)

        result = await recorder.record(
            session_id=uuid.uuid4(),
            stage="stock_analysis",
            decision="BUY",
            reasoning="strong fundamentals",
            parent_id=uuid.uuid4(),
            agent_type="stock_analyst",
            symbol="005930",
            confidence=Decimal("0.8500"),
            llm_provider="openai",
            llm_model="gpt-4o",
            llm_prompt="Analyze 005930",
            llm_response="Buy recommendation",
            llm_tokens_in=500,
            llm_tokens_out=200,
            llm_cost_usd=Decimal("0.005000"),
            data_snapshot={"rsi": 35, "macd": "bullish"},
        )
        assert isinstance(result, uuid.UUID)

    @pytest.mark.asyncio
    async def test_record_with_parent_id(self) -> None:
        factory = _make_mock_session_factory()
        recorder = DecisionRecorder(factory)

        parent = uuid.uuid4()
        result = await recorder.record(
            session_id=uuid.uuid4(),
            stage="trade_decision",
            decision="BUY",
            reasoning="approved by risk manager",
            parent_id=parent,
        )
        assert isinstance(result, uuid.UUID)
        # Verify the added row has parent_id set
        added_row = factory._session.add.call_args[0][0]
        assert added_row.parent_id == parent

    @pytest.mark.asyncio
    async def test_record_with_data_snapshot(self) -> None:
        factory = _make_mock_session_factory()
        recorder = DecisionRecorder(factory)

        snapshot = {"technical_score": 75, "patterns": ["golden_cross"]}
        await recorder.record(
            session_id=uuid.uuid4(),
            stage="stock_analysis",
            decision="BUY",
            reasoning="pattern detected",
            data_snapshot=snapshot,
        )
        added_row = factory._session.add.call_args[0][0]
        assert added_row.data_snapshot == snapshot


# ===========================================================================
# B. update_outcome
# ===========================================================================


class TestUpdateOutcome:
    """DecisionRecorder.update_outcome() 테스트."""

    @pytest.mark.asyncio
    async def test_update_outcome_success(self) -> None:
        result_mock = MagicMock()
        result_mock.rowcount = 1
        factory = _make_mock_session_factory(execute_return=result_mock)
        recorder = DecisionRecorder(factory)

        success = await recorder.update_outcome(
            uuid.uuid4(),
            outcome="profit",
            outcome_pnl=Decimal("15000.00"),
            outcome_note="Sold at target price",
        )
        assert success is True

    @pytest.mark.asyncio
    async def test_update_outcome_not_found(self) -> None:
        result_mock = MagicMock()
        result_mock.rowcount = 0
        factory = _make_mock_session_factory(execute_return=result_mock)
        recorder = DecisionRecorder(factory)

        success = await recorder.update_outcome(
            uuid.uuid4(), outcome="expired"
        )
        assert success is False

    @pytest.mark.asyncio
    async def test_update_outcome_minimal(self) -> None:
        result_mock = MagicMock()
        result_mock.rowcount = 1
        factory = _make_mock_session_factory(execute_return=result_mock)
        recorder = DecisionRecorder(factory)

        success = await recorder.update_outcome(
            uuid.uuid4(), outcome="cancelled"
        )
        assert success is True


# ===========================================================================
# C. get_session_decisions
# ===========================================================================


class TestGetSessionDecisions:
    """DecisionRecorder.get_session_decisions() 테스트."""

    @pytest.mark.asyncio
    async def test_get_session_decisions_ordered(self) -> None:
        sid = uuid.uuid4()
        rows = [
            _make_decision_row(uuid.uuid4(), sid, stage="market_analysis"),
            _make_decision_row(uuid.uuid4(), sid, stage="stock_analysis"),
        ]
        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = rows
        result_mock.scalars.return_value = scalars_mock
        factory = _make_mock_session_factory(execute_return=result_mock)
        recorder = DecisionRecorder(factory)

        decisions = await recorder.get_session_decisions(sid)
        assert len(decisions) == 2
        assert decisions[0].stage == "market_analysis"
        assert decisions[1].stage == "stock_analysis"

    @pytest.mark.asyncio
    async def test_get_session_decisions_empty(self) -> None:
        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        result_mock.scalars.return_value = scalars_mock
        factory = _make_mock_session_factory(execute_return=result_mock)
        recorder = DecisionRecorder(factory)

        decisions = await recorder.get_session_decisions(uuid.uuid4())
        assert decisions == []


# ===========================================================================
# D. get_decision_chain
# ===========================================================================


class TestGetDecisionChain:
    """DecisionRecorder.get_decision_chain() 테스트."""

    @pytest.mark.asyncio
    async def test_get_decision_chain_single(self) -> None:
        """Single node with no parent."""
        did = uuid.uuid4()
        row = _make_decision_row(did, uuid.uuid4(), parent_id=None)

        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.first.return_value = row
        result_mock.scalars.return_value = scalars_mock

        factory = _make_mock_session_factory(execute_return=result_mock)
        recorder = DecisionRecorder(factory)

        chain = await recorder.get_decision_chain(did)
        assert len(chain) == 1
        assert chain[0].decision_id == did

    @pytest.mark.asyncio
    async def test_get_decision_chain_multi(self) -> None:
        """3-level chain: leaf → middle → root."""
        root_id = uuid.uuid4()
        mid_id = uuid.uuid4()
        leaf_id = uuid.uuid4()
        sid = uuid.uuid4()

        root = _make_decision_row(root_id, sid, parent_id=None, stage="market_analysis")
        mid = _make_decision_row(mid_id, sid, parent_id=root_id, stage="stock_analysis")
        leaf = _make_decision_row(leaf_id, sid, parent_id=mid_id, stage="trade_decision")

        chain_map = {leaf_id: leaf, mid_id: mid, root_id: root}

        session = AsyncMock()
        call_count = 0

        async def _mock_execute(stmt):
            nonlocal call_count
            # Extract the decision_id from the query by tracking call order
            ids = [leaf_id, mid_id, root_id]
            if call_count < len(ids):
                target_id = ids[call_count]
                call_count += 1
                result_mock = MagicMock()
                scalars_mock = MagicMock()
                scalars_mock.first.return_value = chain_map.get(target_id)
                result_mock.scalars.return_value = scalars_mock
                return result_mock
            result_mock = MagicMock()
            scalars_mock = MagicMock()
            scalars_mock.first.return_value = None
            result_mock.scalars.return_value = scalars_mock
            return result_mock

        session.execute = _mock_execute

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=ctx)

        recorder = DecisionRecorder(factory)
        chain = await recorder.get_decision_chain(leaf_id)
        assert len(chain) == 3
        assert chain[0].decision_id == leaf_id
        assert chain[1].decision_id == mid_id
        assert chain[2].decision_id == root_id

    @pytest.mark.asyncio
    async def test_get_decision_chain_not_found(self) -> None:
        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.first.return_value = None
        result_mock.scalars.return_value = scalars_mock
        factory = _make_mock_session_factory(execute_return=result_mock)
        recorder = DecisionRecorder(factory)

        chain = await recorder.get_decision_chain(uuid.uuid4())
        assert chain == []


# ===========================================================================
# E. 에러 처리
# ===========================================================================


class TestErrorHandling:
    """DB 에러 → DatabaseError 래핑 테스트."""

    @pytest.mark.asyncio
    async def test_record_db_error(self) -> None:
        factory = _make_mock_session_factory(
            raise_on_commit=RuntimeError("connection lost")
        )
        recorder = DecisionRecorder(factory)

        with pytest.raises(DatabaseError, match="Decision record insert failed"):
            await recorder.record(
                session_id=uuid.uuid4(),
                stage="test",
                decision="HOLD",
                reasoning="test",
            )

    @pytest.mark.asyncio
    async def test_update_outcome_db_error(self) -> None:
        factory = _make_mock_session_factory(
            raise_on_execute=RuntimeError("connection lost")
        )
        recorder = DecisionRecorder(factory)

        with pytest.raises(DatabaseError, match="Decision outcome update failed"):
            await recorder.update_outcome(uuid.uuid4(), outcome="profit")

    @pytest.mark.asyncio
    async def test_get_session_decisions_db_error(self) -> None:
        factory = _make_mock_session_factory(
            raise_on_execute=RuntimeError("connection lost")
        )
        recorder = DecisionRecorder(factory)

        with pytest.raises(DatabaseError, match="Session decisions query failed"):
            await recorder.get_session_decisions(uuid.uuid4())
