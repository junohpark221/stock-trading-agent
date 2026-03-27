"""SchedulerFactory + main.py lifespan 통합 테스트.

factory.py: create_scheduler, _build_job_closures, _get_watchlist_symbols
main.py: get_scheduler(), SCHEDULER_ENABLED 분기
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.scheduler.factory import SchedulerFactory, _noop
from tests.conftest import make_settings

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_mock_session():
    """DB session mock: add, commit, refresh, get, execute."""
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.get = AsyncMock(return_value=MagicMock())
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _mock_session_factory():
    session = _make_mock_session()
    return MagicMock(return_value=session), session


# ── _get_watchlist_symbols ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_watchlist_symbols_empty_table():
    """stock_master 비어있으면 빈 리스트 반환."""
    factory, session = _mock_session_factory()
    result_mock = MagicMock()
    result_mock.all.return_value = []
    session.execute = AsyncMock(return_value=result_mock)

    symbols = await SchedulerFactory._get_watchlist_symbols(factory)

    assert symbols == []
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_watchlist_symbols_with_data():
    """stock_master에 데이터 있으면 종목코드 리스트 반환."""
    factory, session = _mock_session_factory()
    result_mock = MagicMock()
    result_mock.all.return_value = [("005930",), ("035720",), ("051910",)]
    session.execute = AsyncMock(return_value=result_mock)

    symbols = await SchedulerFactory._get_watchlist_symbols(factory)

    assert symbols == ["005930", "035720", "051910"]


@pytest.mark.asyncio
async def test_get_watchlist_symbols_db_error():
    """DB 에러 시 빈 리스트 반환 (fail-open)."""
    factory, session = _mock_session_factory()
    session.execute = AsyncMock(side_effect=RuntimeError("DB down"))

    symbols = await SchedulerFactory._get_watchlist_symbols(factory)

    assert symbols == []


# ── _build_job_closures ──────────────────────────────────────────────────


def test_build_job_closures_returns_9_keys():
    """9개 클로저 딕셔너리 반환, 각 값이 callable."""
    closures = SchedulerFactory._build_job_closures(
        auth=MagicMock(),
        provider=MagicMock(),
        orchestrator=MagicMock(),
        position_manager=MagicMock(),
        portfolio_service=MagicMock(),
        exit_checker=MagicMock(),
        exit_service=MagicMock(),
        broker=MagicMock(),
        monitor=MagicMock(),
        generator=MagicMock(),
        telegram_bot=MagicMock(),
        watchlist_symbols=["005930"],
    )

    expected_keys = {
        "token_refresh_fn",
        "market_data_collect_fn",
        "swing_analysis_fn",
        "position_analysis_fn",
        "stop_loss_check_fn",
        "daily_report_fn",
        "weekly_report_fn",
        "monthly_report_fn",
        "llm_cost_report_fn",
    }
    assert set(closures.keys()) == expected_keys
    for fn in closures.values():
        assert callable(fn)


@pytest.mark.asyncio
async def test_build_job_closures_auth_none_uses_noop():
    """auth=None일 때 token_refresh_fn은 no-op (에러 없이 실행)."""
    closures = SchedulerFactory._build_job_closures(
        auth=None,
        provider=MagicMock(),
        orchestrator=MagicMock(),
        position_manager=MagicMock(),
        portfolio_service=MagicMock(),
        exit_checker=MagicMock(),
        exit_service=MagicMock(),
        broker=MagicMock(),
        monitor=MagicMock(),
        generator=MagicMock(),
        telegram_bot=MagicMock(),
        watchlist_symbols=[],
    )

    # token_refresh_fn은 _noop이어야 함
    assert closures["token_refresh_fn"] is _noop
    # 에러 없이 실행 가능
    await closures["token_refresh_fn"]()


@pytest.mark.asyncio
async def test_build_job_closures_provider_none_uses_noop():
    """provider=None일 때 market_data_collect_fn은 no-op."""
    closures = SchedulerFactory._build_job_closures(
        auth=None,
        provider=None,
        orchestrator=MagicMock(),
        position_manager=MagicMock(),
        portfolio_service=MagicMock(),
        exit_checker=MagicMock(),
        exit_service=MagicMock(),
        broker=MagicMock(),
        monitor=MagicMock(),
        generator=MagicMock(),
        telegram_bot=MagicMock(),
        watchlist_symbols=[],
    )

    assert closures["market_data_collect_fn"] is _noop
    await closures["market_data_collect_fn"]()


# ── create_scheduler helpers ─────────────────────────────────────────────

# 모든 무거운 의존성 patch 목록 (Python 20-block nesting 제한 회피)
_COMMON_PATCHES = [
    "src.agent.orchestrator.PipelineOrchestrator",
    "src.agent.agents.market_analyst.MarketAnalyst",
    "src.agent.agents.stock_analyst.StockAnalyst",
    "src.agent.agents.risk_manager.RiskManager",
    "src.agent.agents.trader.Trader",
    "src.agent.decision_recorder.DecisionRecorder",
    "src.agent.tools.context.ToolContext",
    "src.agent.tools.registry.ToolRegistry",
    "src.llm.cost_tracker.CostTracker",
    "src.llm.router.LLMRouter",
    "src.execution.web_verify.WebSearchVerifier",
    "src.execution.executor.OrderExecutor",
    "src.execution.exit_executor.ExitExecutionService",
    "src.report.data_fetcher.ReportDataFetcher",
    "src.report.generator.ReportGenerator",
    "src.scheduler.monitor.TradingMonitor",
    "src.strategy.exit_checker.ExitConditionChecker",
    "src.strategy.portfolio_state.PortfolioStateService",
    "src.strategy.position_manager.PositionManager",
    "src.strategy.risk_manager.AlgoRiskManager",
]


def _enter_common_patches(stack, extra_patches=None):
    """ExitStack에 공통 + 추가 patches 등록, ApprovalManager mock 반환."""
    from contextlib import ExitStack  # noqa: F811

    mocks = {}
    for target in extra_patches or []:
        if isinstance(target, tuple):
            name, kwargs = target
            mocks[name] = stack.enter_context(patch(name, **kwargs))
        else:
            mocks[target] = stack.enter_context(patch(target))

    for target in _COMMON_PATCHES:
        mocks[target] = stack.enter_context(patch(target))

    # ApprovalManager — initialize mock 필수
    approval_mock = stack.enter_context(
        patch("src.execution.approval.ApprovalManager"),
    )
    approval_mock.return_value.initialize = AsyncMock()
    mocks["approval"] = approval_mock

    return mocks


# ── create_scheduler (mock broker) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_create_scheduler_mock_broker():
    """USE_MOCK_BROKER=True → InMemoryBroker 사용, 9개 작업 등록."""
    from contextlib import ExitStack

    settings = make_settings(USE_MOCK_BROKER=True, SCHEDULER_ENABLED=True)
    session_factory, _ = _mock_session_factory()

    with ExitStack() as stack:
        _enter_common_patches(stack, extra_patches=[
            (
                "src.scheduler.factory.SchedulerFactory._get_watchlist_symbols",
                {"new_callable": AsyncMock, "return_value": ["005930"]},
            ),
        ])

        engine, broker = await SchedulerFactory.create_scheduler(
            settings=settings,
            session_factory=session_factory,
            cache=MagicMock(),
            telegram_bot=AsyncMock(),
        )

    from src.broker.mock.client import InMemoryBroker

    assert isinstance(broker, InMemoryBroker)
    assert len(engine._job_fns) == 9


@pytest.mark.asyncio
async def test_create_scheduler_kis_broker():
    """USE_MOCK_BROKER=False → KISClient 사용, KISAuth 접근."""
    from contextlib import ExitStack

    settings = make_settings(USE_MOCK_BROKER=False, SCHEDULER_ENABLED=True)
    session_factory, _ = _mock_session_factory()

    mock_auth = MagicMock()
    mock_kis_client = AsyncMock()
    mock_kis_client._auth = mock_auth
    mock_kis_client.connect = AsyncMock()

    with ExitStack() as stack:
        _enter_common_patches(stack, extra_patches=[
            (
                "src.scheduler.factory.SchedulerFactory._get_watchlist_symbols",
                {"new_callable": AsyncMock, "return_value": []},
            ),
            (
                "src.broker.kis.client.KISClient",
                {"return_value": mock_kis_client},
            ),
            "src.data.providers.kis_provider.KISDataProvider",
        ])

        engine, broker = await SchedulerFactory.create_scheduler(
            settings=settings,
            session_factory=session_factory,
            cache=MagicMock(),
            telegram_bot=AsyncMock(),
        )

    assert broker is mock_kis_client
    mock_kis_client.connect.assert_awaited_once()
    assert len(engine._job_fns) == 9


# ── main.py lifespan integration ─────────────────────────────────────────


def test_get_scheduler_raises_when_not_initialized():
    """get_scheduler() — 초기화 전이면 RuntimeError."""
    import src.main as main_mod

    original = main_mod._scheduler_engine
    try:
        main_mod._scheduler_engine = None
        with pytest.raises(RuntimeError, match="Scheduler not initialized"):
            main_mod.get_scheduler()
    finally:
        main_mod._scheduler_engine = original


def test_get_scheduler_returns_engine():
    """get_scheduler() — 초기화 후 정상 반환."""
    import src.main as main_mod

    sentinel = MagicMock()
    original = main_mod._scheduler_engine
    try:
        main_mod._scheduler_engine = sentinel
        assert main_mod.get_scheduler() is sentinel
    finally:
        main_mod._scheduler_engine = original
