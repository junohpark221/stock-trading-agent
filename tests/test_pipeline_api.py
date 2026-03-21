"""Unit tests for Pipeline API and Decision Log API endpoints.

Uses httpx AsyncClient with FastAPI test transport.
All LLM calls are mocked — no real API invocations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import src.main as main_mod
from src.core.enums import DecisionAction
from src.core.models import (
    MarketCondition,
    PipelineResult,
    RiskAssessment,
    StockAnalysis,
    TradeDecision,
)
from src.db.session import get_db_session


# ── Helpers ───────────────────────────────────────────────────────────


def _pipeline_result(
    symbols: list[str] | None = None,
    *,
    success: bool = True,
    session_id: uuid.UUID | None = None,
) -> PipelineResult:
    """Create a sample PipelineResult for testing."""
    sid = session_id or uuid.uuid4()
    syms = symbols or ["005930"]
    return PipelineResult(
        session_id=sid,
        started_at=datetime(2026, 3, 21, 9, 0, tzinfo=UTC),
        completed_at=datetime(2026, 3, 21, 9, 1, tzinfo=UTC),
        market_condition=MarketCondition(
            condition="bullish",
            confidence=Decimal("0.75"),
            kospi_trend="상승",
            kosdaq_trend="횡보",
            market_risk_level="medium",
            reasoning="macro stable",
        ),
        stock_analyses=[
            StockAnalysis(
                symbol=s,
                name="테스트종목",
                action=DecisionAction.BUY,
                confidence=Decimal("0.8"),
                reasoning="good signals",
            )
            for s in syms
        ],
        risk_assessments=[
            RiskAssessment(
                symbol=s,
                approved=True,
                risk_level="medium",
                confidence=Decimal("0.85"),
                reasoning="risk ok",
            )
            for s in syms
        ],
        trade_decisions=[
            TradeDecision(
                symbol=s,
                action=DecisionAction.BUY,
                confidence=Decimal("0.82"),
                quantity=10,
                price=Decimal("78000"),
                reasoning="execute buy",
            )
            for s in syms
        ],
        symbols_requested=list(syms),
        symbols_analyzed=list(syms),
        symbols_skipped=[],
        total_llm_cost_usd=Decimal("0.02"),
        total_llm_calls=4,
        success=success,
    )


def _mock_decision_log_row(**kwargs) -> MagicMock:
    """DecisionLog ORM row mock."""
    row = MagicMock()
    row.decision_id = kwargs.get("decision_id", uuid.uuid4())
    row.parent_id = kwargs.get("parent_id", None)
    row.session_id = kwargs.get("session_id", uuid.uuid4())
    row.stage = kwargs.get("stage", "market_analysis")
    row.agent_type = kwargs.get("agent_type", "market_analyst")
    row.symbol = kwargs.get("symbol", None)
    row.llm_provider = kwargs.get("llm_provider", "openai")
    row.llm_model = kwargs.get("llm_model", "gpt-4o")
    row.llm_tokens_in = kwargs.get("llm_tokens_in", 500)
    row.llm_tokens_out = kwargs.get("llm_tokens_out", 200)
    row.llm_cost_usd = kwargs.get("llm_cost_usd", Decimal("0.005"))
    row.decision = kwargs.get("decision", "bullish")
    row.confidence = kwargs.get("confidence", Decimal("0.75"))
    row.reasoning = kwargs.get("reasoning", "macro stable")
    row.outcome = kwargs.get("outcome", None)
    row.outcome_pnl = kwargs.get("outcome_pnl", None)
    row.created_at = kwargs.get("created_at", datetime(2026, 3, 21, 9, 0, tzinfo=UTC))
    return row


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_db_session():
    """Mock AsyncSession for DI override."""
    session = AsyncMock()
    return session


@pytest.fixture
def app(mock_db_session):
    """FastAPI app with dependency overrides."""
    main_mod.app.dependency_overrides[get_db_session] = lambda: mock_db_session
    yield main_mod.app
    main_mod.app.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    """httpx AsyncClient bound to the test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── Pipeline API Tests ───────────────────────────────────────────────


@pytest.mark.asyncio
@patch("src.api.routes.pipeline._build_orchestrator")
async def test_pipeline_run_single_symbol(mock_build, client):
    """POST /api/pipeline/run — 1종목 성공."""
    mock_orch = MagicMock()
    mock_orch.execute = AsyncMock(return_value=_pipeline_result(["005930"]))
    mock_build.return_value = mock_orch

    resp = await client.post("/api/pipeline/run", json={"symbols": ["005930"]})

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["session_id"] is not None
    assert len(data["stock_analyses"]) == 1
    assert data["symbols_analyzed"] == ["005930"]


@pytest.mark.asyncio
@patch("src.api.routes.pipeline._build_orchestrator")
async def test_pipeline_run_multiple_symbols(mock_build, client):
    """POST /api/pipeline/run — 3종목 성공."""
    symbols = ["005930", "000660", "035420"]
    mock_orch = MagicMock()
    mock_orch.execute = AsyncMock(return_value=_pipeline_result(symbols))
    mock_build.return_value = mock_orch

    resp = await client.post("/api/pipeline/run", json={"symbols": symbols})

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["stock_analyses"]) == 3
    assert sorted(data["symbols_analyzed"]) == sorted(symbols)


@pytest.mark.asyncio
async def test_pipeline_run_empty_symbols(client):
    """POST /api/pipeline/run — 빈 symbols → 422."""
    resp = await client.post("/api/pipeline/run", json={"symbols": []})
    assert resp.status_code == 422


@pytest.mark.asyncio
@patch("src.api.routes.pipeline._build_orchestrator")
async def test_pipeline_run_market_failure(mock_build, client):
    """POST /api/pipeline/run — 시장분석 실패 → success=False."""
    mock_orch = MagicMock()
    result = _pipeline_result(["005930"], success=False)
    result.market_condition = None
    result.stock_analyses = []
    result.risk_assessments = []
    result.trade_decisions = []
    result.errors = ["Market analysis failed: LLM timeout"]
    mock_orch.execute = AsyncMock(return_value=result)
    mock_build.return_value = mock_orch

    resp = await client.post("/api/pipeline/run", json={"symbols": ["005930"]})

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert len(data["errors"]) > 0


@pytest.mark.asyncio
@patch("src.api.routes.pipeline._build_orchestrator")
async def test_pipeline_run_partial_failure(mock_build, client):
    """POST /api/pipeline/run — 일부 종목 실패."""
    result = _pipeline_result(["005930"])
    result.symbols_requested = ["005930", "000660"]
    result.symbols_skipped = ["000660"]
    result.errors = ["000660: Stock analysis failed"]
    mock_orch = MagicMock()
    mock_orch.execute = AsyncMock(return_value=result)
    mock_build.return_value = mock_orch

    resp = await client.post(
        "/api/pipeline/run", json={"symbols": ["005930", "000660"]}
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "000660" in data["symbols_skipped"]


@pytest.mark.asyncio
@patch("src.api.routes.pipeline._build_orchestrator")
async def test_pipeline_run_explicit_session_id(mock_build, client):
    """POST /api/pipeline/run — 명시적 session_id."""
    explicit_id = uuid.uuid4()
    mock_orch = MagicMock()
    mock_orch.execute = AsyncMock(
        return_value=_pipeline_result(["005930"], session_id=explicit_id)
    )
    mock_build.return_value = mock_orch

    resp = await client.post(
        "/api/pipeline/run",
        json={"symbols": ["005930"], "session_id": str(explicit_id)},
    )

    assert resp.status_code == 200
    assert resp.json()["session_id"] == str(explicit_id)


@pytest.mark.asyncio
@patch("src.api.routes.pipeline.DecisionRecorder")
@patch("src.api.routes.pipeline.get_session_factory")
async def test_pipeline_result_success(mock_factory, mock_recorder_cls, client):
    """GET /api/pipeline/result/{session_id} — 성공."""
    sid = uuid.uuid4()
    rows = [
        _mock_decision_log_row(session_id=sid, stage="market_analysis"),
        _mock_decision_log_row(session_id=sid, stage="stock_analysis", symbol="005930"),
    ]
    mock_recorder = MagicMock()
    mock_recorder.get_session_decisions = AsyncMock(return_value=rows)
    mock_recorder_cls.return_value = mock_recorder

    resp = await client.get(f"/api/pipeline/result/{sid}")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


@pytest.mark.asyncio
@patch("src.api.routes.pipeline.DecisionRecorder")
@patch("src.api.routes.pipeline.get_session_factory")
async def test_pipeline_result_empty(mock_factory, mock_recorder_cls, client):
    """GET /api/pipeline/result/{session_id} — 빈 결과 → 200."""
    sid = uuid.uuid4()
    mock_recorder = MagicMock()
    mock_recorder.get_session_decisions = AsyncMock(return_value=[])
    mock_recorder_cls.return_value = mock_recorder

    resp = await client.get(f"/api/pipeline/result/{sid}")

    assert resp.status_code == 200
    assert resp.json() == []


# ── Decision Log API Tests ───────────────────────────────────────────


def _setup_mock_session_execute(mock_session, rows, total=None):
    """Configure mock_session.execute() to return rows for select queries."""
    # This helper is intentionally simple — callers set up side_effect directly
    pass


@pytest.mark.asyncio
async def test_decisions_session_success(mock_db_session, client):
    """GET /api/decisions/{session_id} — 성공."""
    sid = uuid.uuid4()
    rows = [
        _mock_decision_log_row(session_id=sid, stage="market_analysis"),
        _mock_decision_log_row(session_id=sid, stage="stock_analysis", symbol="005930"),
    ]

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = rows
    mock_db_session.execute = AsyncMock(return_value=mock_result)

    resp = await client.get(f"/api/decisions/{sid}")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["stage"] == "market_analysis"
    assert data[1]["symbol"] == "005930"


@pytest.mark.asyncio
async def test_decisions_session_empty(mock_db_session, client):
    """GET /api/decisions/{session_id} — 없는 세션 → 200 빈 리스트."""
    sid = uuid.uuid4()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db_session.execute = AsyncMock(return_value=mock_result)

    resp = await client.get(f"/api/decisions/{sid}")

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_decisions_list_default(mock_db_session, client):
    """GET /api/decisions — 기본 조회 (pagination)."""
    rows = [_mock_decision_log_row() for _ in range(3)]

    # execute returns: count scalar, then select scalars
    count_result = MagicMock()
    count_result.scalar_one.return_value = 3
    select_result = MagicMock()
    select_result.scalars.return_value.all.return_value = rows

    mock_db_session.execute = AsyncMock(side_effect=[count_result, select_result])

    resp = await client.get("/api/decisions")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert len(data["items"]) == 3
    assert data["limit"] == 50
    assert data["offset"] == 0


@pytest.mark.asyncio
async def test_decisions_list_filter_symbol(mock_db_session, client):
    """GET /api/decisions?symbol=005930 — symbol 필터."""
    rows = [_mock_decision_log_row(symbol="005930")]

    count_result = MagicMock()
    count_result.scalar_one.return_value = 1
    select_result = MagicMock()
    select_result.scalars.return_value.all.return_value = rows

    mock_db_session.execute = AsyncMock(side_effect=[count_result, select_result])

    resp = await client.get("/api/decisions?symbol=005930")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["symbol"] == "005930"


@pytest.mark.asyncio
async def test_decisions_list_filter_stage(mock_db_session, client):
    """GET /api/decisions?stage=market_analysis — stage 필터."""
    rows = [_mock_decision_log_row(stage="market_analysis")]

    count_result = MagicMock()
    count_result.scalar_one.return_value = 1
    select_result = MagicMock()
    select_result.scalars.return_value.all.return_value = rows

    mock_db_session.execute = AsyncMock(side_effect=[count_result, select_result])

    resp = await client.get("/api/decisions?stage=market_analysis")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["stage"] == "market_analysis"


@pytest.mark.asyncio
async def test_decisions_stats_default(mock_db_session, client):
    """GET /api/decisions/stats — 기본 집계."""
    # Multiple execute calls: count, avg, cost, llm_calls, by_stage, by_agent, by_symbol
    count_result = MagicMock()
    count_result.scalar_one.return_value = 10

    avg_result = MagicMock()
    avg_result.scalar_one.return_value = Decimal("0.8000")

    cost_result = MagicMock()
    cost_result.scalar_one.return_value = Decimal("0.050000")

    calls_result = MagicMock()
    calls_result.scalar_one.return_value = 8

    stage_row1 = MagicMock()
    stage_row1.stage = "market_analysis"
    stage_row1.cnt = 2
    stage_row2 = MagicMock()
    stage_row2.stage = "stock_analysis"
    stage_row2.cnt = 4
    stage_result = MagicMock()
    stage_result.all.return_value = [stage_row1, stage_row2]

    agent_row1 = MagicMock()
    agent_row1.agent_type = "market_analyst"
    agent_row1.cnt = 2
    agent_result = MagicMock()
    agent_result.all.return_value = [agent_row1]

    symbol_row1 = MagicMock()
    symbol_row1.symbol = "005930"
    symbol_row1.cnt = 4
    symbol_result = MagicMock()
    symbol_result.all.return_value = [symbol_row1]

    mock_db_session.execute = AsyncMock(
        side_effect=[
            count_result,
            avg_result,
            cost_result,
            calls_result,
            stage_result,
            agent_result,
            symbol_result,
        ]
    )

    resp = await client.get("/api/decisions/stats")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_decisions"] == 10
    assert data["by_stage"]["market_analysis"] == 2
    assert data["by_agent_type"]["market_analyst"] == 2
    assert data["by_symbol"]["005930"] == 4
    assert data["total_llm_calls"] == 8


@pytest.mark.asyncio
async def test_decisions_stats_with_symbol_filter(mock_db_session, client):
    """GET /api/decisions/stats?symbol=005930 — 필터된 집계."""
    count_result = MagicMock()
    count_result.scalar_one.return_value = 4

    avg_result = MagicMock()
    avg_result.scalar_one.return_value = Decimal("0.8200")

    cost_result = MagicMock()
    cost_result.scalar_one.return_value = Decimal("0.020000")

    calls_result = MagicMock()
    calls_result.scalar_one.return_value = 3

    stage_result = MagicMock()
    stage_result.all.return_value = []

    agent_result = MagicMock()
    agent_result.all.return_value = []

    symbol_row = MagicMock()
    symbol_row.symbol = "005930"
    symbol_row.cnt = 4
    symbol_result = MagicMock()
    symbol_result.all.return_value = [symbol_row]

    mock_db_session.execute = AsyncMock(
        side_effect=[
            count_result,
            avg_result,
            cost_result,
            calls_result,
            stage_result,
            agent_result,
            symbol_result,
        ]
    )

    resp = await client.get("/api/decisions/stats?symbol=005930")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_decisions"] == 4
