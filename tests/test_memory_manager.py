"""Phase 4 Step 9: AgentMemoryManager 단위 테스트.

CRUD, 만료 처리, 관련성 정렬, 학습 자동 생성 = 20 테스트.
mock async session 패턴 사용.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.core.exceptions import DatabaseError
from src.strategy.memory_manager import (
    AgentMemoryManager,
    _format_entry_snapshot,
    _strategy_to_agent,
    build_entry_snapshot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_session_factory():
    """mock async session factory 생성."""
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, session


def _mock_memory_record(**overrides: object) -> MagicMock:
    """AgentMemory mock 생성."""
    record = MagicMock()
    record.id = 1
    record.agent_type = "stock_analyst"
    record.memory_type = "lesson"
    record.symbol = "005930"
    record.content = "RSI 과매수 상태에서 매수 → 손실"
    record.context = {"pnl_pct": "-3.5"}
    record.source_session_id = None
    record.relevance_score = Decimal("0.9")
    record.expires_at = datetime.now(timezone.utc) + timedelta(days=90)
    record.is_active = True
    for k, v in overrides.items():
        setattr(record, k, v)
    return record


def _mock_position_record(**overrides: object) -> MagicMock:
    """PositionRecord mock 생성."""
    record = MagicMock()
    record.id = 1
    record.symbol = "005930"
    record.strategy_type = "position"
    record.quantity = 100
    record.avg_cost = Decimal("70000")
    record.entry_price = Decimal("70000")
    record.entry_date = date(2026, 3, 20)
    record.stop_loss_price = Decimal("67000")
    record.take_profit_price = Decimal("77000")
    record.status = "closed"
    record.exit_price = Decimal("75000")
    record.exit_date = date(2026, 3, 23)
    record.exit_reason = "take_profit"
    record.realized_pnl = Decimal("500000")
    record.entry_session_id = uuid4()
    record.exit_session_id = uuid4()
    record.account_id = "default"
    record.entry_analysis_snapshot = {
        "symbol": "005930",
        "action": "buy",
        "confidence": "0.85",
        "key_factors": ["RSI 과매도", "골든크로스", "실적 개선"],
    }
    for k, v in overrides.items():
        setattr(record, k, v)
    return record


def _mock_pipeline_result(symbol: str = "005930") -> MagicMock:
    """PipelineResult mock 생성."""
    result = MagicMock()
    sa = MagicMock()
    sa.symbol = symbol
    sa.action.value = "buy"
    sa.confidence = Decimal("0.85")
    sa.key_factors = ["RSI 과매도", "골든크로스", "실적 개선"]
    result.stock_analyses = [sa]
    result.session_id = uuid4()
    return result


def _mock_scalars_result(records: list) -> MagicMock:
    """session.execute() scalars 결과 mock."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = records
    return result


# ===========================================================================
# save_lesson
# ===========================================================================


class TestSaveLesson:
    """save_lesson 메서드 테스트."""

    @pytest.mark.asyncio
    async def test_save_basic(self):
        """기본 학습 메모리 저장."""
        factory, session = _mock_session_factory()
        mgr = AgentMemoryManager(factory)

        async def _refresh(record):
            record.id = 42

        session.refresh = _refresh
        memory_id = await mgr.save_lesson(
            agent_type="stock_analyst",
            symbol="005930",
            content="RSI 과매수 상태에서 매수 시 손실 확률 높음",
        )
        assert memory_id == 42
        session.add.assert_called_once()
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_save_without_symbol(self):
        """범용 메모리 (symbol=None) 저장."""
        factory, session = _mock_session_factory()
        mgr = AgentMemoryManager(factory)

        async def _refresh(record):
            record.id = 10

        session.refresh = _refresh
        memory_id = await mgr.save_lesson(
            agent_type="risk_manager",
            symbol=None,
            content="베어 마켓에서 포지션 축소 필요",
        )
        assert memory_id == 10
        added = session.add.call_args[0][0]
        assert added.symbol is None

    @pytest.mark.asyncio
    async def test_save_with_context(self):
        """컨텍스트 포함 저장."""
        factory, session = _mock_session_factory()
        mgr = AgentMemoryManager(factory)

        async def _refresh(record):
            record.id = 5

        session.refresh = _refresh
        ctx = {"realized_pnl": "-350000", "exit_reason": "stop_loss"}
        memory_id = await mgr.save_lesson(
            agent_type="stock_analyst",
            symbol="035720",
            content="손절 교훈",
            context=ctx,
        )
        assert memory_id == 5
        added = session.add.call_args[0][0]
        assert added.context == ctx

    @pytest.mark.asyncio
    async def test_save_with_session_id(self):
        """source_session_id 포함 저장."""
        factory, session = _mock_session_factory()
        mgr = AgentMemoryManager(factory)
        sid = str(uuid4())

        async def _refresh(record):
            record.id = 7

        session.refresh = _refresh
        await mgr.save_lesson(
            agent_type="stock_analyst",
            symbol="005930",
            content="교훈",
            source_session_id=sid,
        )
        added = session.add.call_args[0][0]
        assert str(added.source_session_id) == sid

    @pytest.mark.asyncio
    async def test_save_custom_expires(self):
        """커스텀 만료 기간 설정."""
        factory, session = _mock_session_factory()
        mgr = AgentMemoryManager(factory)

        async def _refresh(record):
            record.id = 3

        session.refresh = _refresh
        await mgr.save_lesson(
            agent_type="stock_analyst",
            symbol="005930",
            content="교훈",
            expires_days=30,
        )
        added = session.add.call_args[0][0]
        # 만료일이 약 30일 후
        delta = added.expires_at - datetime.now(timezone.utc)
        assert 29 <= delta.days <= 30

    @pytest.mark.asyncio
    async def test_save_db_error(self):
        """DB 에러 시 DatabaseError 발생."""
        factory, session = _mock_session_factory()
        session.commit.side_effect = Exception("connection refused")
        mgr = AgentMemoryManager(factory)

        with pytest.raises(DatabaseError, match="Memory save failed"):
            await mgr.save_lesson(
                agent_type="stock_analyst",
                symbol="005930",
                content="교훈",
            )


# ===========================================================================
# get_relevant_memories
# ===========================================================================


class TestGetRelevantMemories:
    """get_relevant_memories 메서드 테스트."""

    @pytest.mark.asyncio
    async def test_get_by_agent_type(self):
        """agent_type 기반 조회."""
        factory, session = _mock_session_factory()
        records = [_mock_memory_record(id=1), _mock_memory_record(id=2)]
        session.execute.return_value = _mock_scalars_result(records)

        mgr = AgentMemoryManager(factory)
        result = await mgr.get_relevant_memories("stock_analyst")
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_get_with_symbol(self):
        """symbol 필터 포함 조회."""
        factory, session = _mock_session_factory()
        records = [_mock_memory_record(symbol="005930")]
        session.execute.return_value = _mock_scalars_result(records)

        mgr = AgentMemoryManager(factory)
        result = await mgr.get_relevant_memories(
            "stock_analyst", symbol="005930"
        )
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_get_empty_result(self):
        """메모리 없을 때 빈 리스트 반환."""
        factory, session = _mock_session_factory()
        session.execute.return_value = _mock_scalars_result([])

        mgr = AgentMemoryManager(factory)
        result = await mgr.get_relevant_memories("trader")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_with_limit(self):
        """limit 파라미터 적용."""
        factory, session = _mock_session_factory()
        records = [_mock_memory_record(id=i) for i in range(5)]
        session.execute.return_value = _mock_scalars_result(records)

        mgr = AgentMemoryManager(factory)
        result = await mgr.get_relevant_memories(
            "stock_analyst", limit=5
        )
        assert len(result) == 5

    @pytest.mark.asyncio
    async def test_get_db_error(self):
        """DB 에러 시 DatabaseError 발생."""
        factory, session = _mock_session_factory()
        session.execute.side_effect = Exception("timeout")
        mgr = AgentMemoryManager(factory)

        with pytest.raises(DatabaseError, match="Memory query failed"):
            await mgr.get_relevant_memories("stock_analyst")


# ===========================================================================
# record_trade_outcome
# ===========================================================================


class TestRecordTradeOutcome:
    """record_trade_outcome 메서드 테스트."""

    @pytest.mark.asyncio
    async def test_profit_outcome(self):
        """수익 청산 시 학습 메모리 생성."""
        factory, session = _mock_session_factory()
        mgr = AgentMemoryManager(factory)

        async def _refresh(record):
            record.id = 100

        session.refresh = _refresh

        position = _mock_position_record(
            realized_pnl=Decimal("500000"),
        )

        memory_id = await mgr.record_trade_outcome(position)
        assert memory_id == 100

        added = session.add.call_args[0][0]
        assert "수익" in added.content
        # 진입 스냅샷이 교훈 본문에 반영됨
        assert "buy" in added.content
        assert added.relevance_score == Decimal("0.7")

    @pytest.mark.asyncio
    async def test_loss_outcome(self):
        """손실 청산 시 학습 메모리 생성 (관련성 높음)."""
        factory, session = _mock_session_factory()
        mgr = AgentMemoryManager(factory)

        async def _refresh(record):
            record.id = 101

        session.refresh = _refresh

        position = _mock_position_record(
            realized_pnl=Decimal("-350000"),
            exit_reason="stop_loss",
        )

        memory_id = await mgr.record_trade_outcome(position)
        assert memory_id == 101

        added = session.add.call_args[0][0]
        assert "손실" in added.content
        assert added.relevance_score == Decimal("0.9")

    @pytest.mark.asyncio
    async def test_skip_open_position(self):
        """열린 포지션은 학습 건너뜀."""
        factory, _ = _mock_session_factory()
        mgr = AgentMemoryManager(factory)

        position = _mock_position_record(status="open", realized_pnl=None)

        result = await mgr.record_trade_outcome(position)
        assert result is None

    @pytest.mark.asyncio
    async def test_skip_no_pnl(self):
        """realized_pnl이 None인 경우 건너뜀."""
        factory, _ = _mock_session_factory()
        mgr = AgentMemoryManager(factory)

        position = _mock_position_record(
            status="closed", realized_pnl=None
        )

        result = await mgr.record_trade_outcome(position)
        assert result is None

    @pytest.mark.asyncio
    async def test_outcome_context_fields(self):
        """학습 메모리의 context에 필수 필드 포함."""
        factory, session = _mock_session_factory()
        mgr = AgentMemoryManager(factory)

        async def _refresh(record):
            record.id = 200

        session.refresh = _refresh

        position = _mock_position_record()

        await mgr.record_trade_outcome(position)

        added = session.add.call_args[0][0]
        ctx = added.context
        assert "realized_pnl" in ctx
        assert "exit_reason" in ctx
        assert "strategy_type" in ctx
        assert "holding_days" in ctx


# ===========================================================================
# cleanup_expired
# ===========================================================================


class TestCleanupExpired:
    """cleanup_expired 메서드 테스트."""

    @pytest.mark.asyncio
    async def test_cleanup_deactivates(self):
        """만료된 메모리 비활성화."""
        factory, session = _mock_session_factory()
        result_mock = MagicMock()
        result_mock.rowcount = 3
        session.execute.return_value = result_mock

        mgr = AgentMemoryManager(factory)
        count = await mgr.cleanup_expired()

        assert count == 3
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cleanup_none_expired(self):
        """만료된 메모리 없을 때 0 반환."""
        factory, session = _mock_session_factory()
        result_mock = MagicMock()
        result_mock.rowcount = 0
        session.execute.return_value = result_mock

        mgr = AgentMemoryManager(factory)
        count = await mgr.cleanup_expired()
        assert count == 0

    @pytest.mark.asyncio
    async def test_cleanup_db_error(self):
        """DB 에러 시 DatabaseError 발생."""
        factory, session = _mock_session_factory()
        session.execute.side_effect = Exception("lock timeout")
        mgr = AgentMemoryManager(factory)

        with pytest.raises(DatabaseError, match="Memory cleanup failed"):
            await mgr.cleanup_expired()


# ===========================================================================
# Helper functions
# ===========================================================================


class TestHelpers:
    """헬퍼 함수 테스트."""

    def test_build_entry_snapshot_found(self):
        """종목이 PipelineResult에 있으면 직렬화 가능한 스냅샷 dict 반환."""
        pipeline = _mock_pipeline_result("005930")
        snap = build_entry_snapshot("005930", pipeline)
        assert snap is not None
        assert snap["symbol"] == "005930"
        assert snap["action"] == "buy"
        assert snap["confidence"] == "0.85"  # Decimal → str
        assert snap["key_factors"] == ["RSI 과매도", "골든크로스", "실적 개선"]

    def test_build_entry_snapshot_not_found(self):
        """종목이 PipelineResult에 없으면 None 반환."""
        pipeline = _mock_pipeline_result("035720")
        assert build_entry_snapshot("005930", pipeline) is None

    def test_format_entry_snapshot_with_dict(self):
        """스냅샷 dict → 사람이 읽는 요약 문자열."""
        snap = {
            "symbol": "005930",
            "action": "buy",
            "confidence": "0.85",
            "key_factors": ["RSI 과매도", "골든크로스", "실적 개선"],
        }
        result = _format_entry_snapshot(snap)
        assert "buy" in result
        assert "0.85" in result

    def test_format_entry_snapshot_none(self):
        """스냅샷이 None이면 기본 메시지 반환."""
        assert "분석 데이터 없음" in _format_entry_snapshot(None)

    def test_strategy_to_agent(self):
        """전략 유형 → 에이전트 유형 매핑."""
        assert _strategy_to_agent("position") == "stock_analyst"
        assert _strategy_to_agent("swing") == "stock_analyst"
