"""SchedulerFactory + main.py lifespan 통합 테스트.

factory.py: create_scheduler, _load_active_accounts, _decrypt_credentials,
            _make_account_label, _register_common_jobs, _register_account_jobs
main.py: get_scheduler(), SCHEDULER_ENABLED 분기
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.scheduler.factory import SchedulerFactory
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


def _make_mock_account(
    account_id: str = "acct-1",
    nickname: str = "공격형",
    strategy_type: str = "swing",
    investment_prompt: str = "고성장 성장주 위주",
    kis_account_no: str = "50071234-01",
    is_active: bool = True,
    risk_overrides: dict | None = None,
):
    """Account ORM mock."""
    account = MagicMock()
    account.id = account_id
    account.nickname = nickname
    account.kis_app_key_enc = "enc_key"
    account.kis_app_secret_enc = "enc_secret"
    account.kis_account_no = kis_account_no
    account.kis_account_prod = "01"
    account.kis_is_paper = True
    account.kis_hts_id = ""
    account.strategy_type = strategy_type
    account.investment_prompt = investment_prompt
    account.risk_overrides = risk_overrides
    account.is_active = is_active
    return account


# ── _get_watchlist_symbols ────────────────────────────────────────��──────


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
async def test_get_watchlist_symbols_filters_active_only():
    """is_active 필터가 SQL 쿼리에 포함되는지 확인."""
    factory, session = _mock_session_factory()
    result_mock = MagicMock()
    # is_active=True인 종목만 반환된다고 가정
    result_mock.all.return_value = [("005930",), ("035720",)]
    session.execute = AsyncMock(return_value=result_mock)

    symbols = await SchedulerFactory._get_watchlist_symbols(factory)

    assert symbols == ["005930", "035720"]
    # execute에 전달된 SQL에 is_active 조건이 포함되어야 함
    call_args = session.execute.call_args
    stmt = call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "is_active" in compiled


@pytest.mark.asyncio
async def test_get_watchlist_symbols_db_error():
    """DB 에러 시 빈 리스트 반환 (fail-open)."""
    factory, session = _mock_session_factory()
    session.execute = AsyncMock(side_effect=RuntimeError("DB down"))

    symbols = await SchedulerFactory._get_watchlist_symbols(factory)

    assert symbols == []


# ── _make_account_label ──────────────────────────────────────────────────


class TestMakeAccountLabel:
    def test_with_nickname(self):
        """닉네임 있으면 '닉네임 (뒤4자리)' 형식."""
        account = _make_mock_account(nickname="공격형", kis_account_no="50071234-01")
        label = SchedulerFactory._make_account_label(account)
        assert label == "공격형 (4-01)"

    def test_with_nickname_longer_account(self):
        account = _make_mock_account(nickname="보수형", kis_account_no="12345678")
        label = SchedulerFactory._make_account_label(account)
        assert label == "보수형 (5678)"

    def test_nickname_same_as_id(self):
        """닉네임이 id와 같으면 뒤4자리만."""
        account = _make_mock_account(
            account_id="default",
            nickname="default",
            kis_account_no="12345678",
        )
        label = SchedulerFactory._make_account_label(account)
        assert label == "5678"

    def test_no_nickname(self):
        """닉네임 빈 문자열이면 뒤4자리만."""
        account = _make_mock_account(nickname="", kis_account_no="12345678")
        label = SchedulerFactory._make_account_label(account)
        assert label == "5678"


# ── _load_active_accounts ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_active_accounts_empty():
    """활성 계좌 없으면 빈 리스트."""
    factory, session = _mock_session_factory()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=result_mock)

    accounts = await SchedulerFactory._load_active_accounts(factory)
    assert accounts == []


@pytest.mark.asyncio
async def test_load_active_accounts_db_error():
    """DB 에러 시 빈 리스트 (fail-open)."""
    factory, session = _mock_session_factory()
    session.execute = AsyncMock(side_effect=RuntimeError("DB down"))

    accounts = await SchedulerFactory._load_active_accounts(factory)
    assert accounts == []


# ── _decrypt_credentials ─────────────────────────────────────────────────


def test_decrypt_credentials():
    """Account ORM → AccountCredentials 변환."""
    account = _make_mock_account()

    with patch("src.scheduler.factory.AccountCrypto") as mock_crypto:
        mock_crypto.decrypt.side_effect = lambda ct, key: f"decrypted_{ct}"

        creds = SchedulerFactory._decrypt_credentials(account, "test_key")

    assert creds.account_id == "acct-1"
    assert creds.app_key == "decrypted_enc_key"
    assert creds.app_secret == "decrypted_enc_secret"
    assert creds.account_no == "50071234-01"
    assert creds.is_paper is True


# ── _synthesize_default_account ──────────────────────────────────────────


def test_synthesize_default_account():
    """env var로 레거시 default Account 합성."""
    settings = make_settings(
        KIS_APP_KEY="my_key",
        KIS_APP_SECRET="my_secret",
        KIS_ACCOUNT_NO="12345678-01",
        KIS_IS_PAPER=True,
    )
    account = SchedulerFactory._synthesize_default_account(settings)
    assert account.id == "default"
    assert account.nickname == "default"
    assert account.kis_account_no == "12345678-01"
    assert account.strategy_type == "position"
    assert account.is_active is True


# ── create_scheduler helpers ─────────────────────────────────────────────

# 모든 무거운 의존성 patch 목록
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


# ── create_scheduler (mock broker, 레거시 단일 계좌) ────────────────────


@pytest.mark.asyncio
async def test_create_scheduler_mock_broker_legacy():
    """USE_MOCK_BROKER=True, KIS_APP_KEY 설정 → 레거시 default 계좌 합성, BrokerRegistry 반환."""
    from contextlib import ExitStack

    settings = make_settings(
        USE_MOCK_BROKER=True,
        SCHEDULER_ENABLED=True,
        KIS_APP_KEY="test_key",
        KIS_APP_SECRET="test_secret",
        KIS_ACCOUNT_NO="12345678-01",
    )
    session_factory, _ = _mock_session_factory()

    with ExitStack() as stack:
        _enter_common_patches(
            stack,
            extra_patches=[
                (
                    "src.scheduler.factory.SchedulerFactory._get_watchlist_symbols",
                    {"new_callable": AsyncMock, "return_value": ["005930"]},
                ),
                (
                    "src.scheduler.factory.SchedulerFactory._load_active_accounts",
                    {"new_callable": AsyncMock, "return_value": []},
                ),
            ],
        )

        engine, registry, _stream, _sl, _rt = await SchedulerFactory.create_scheduler(
            settings=settings,
            session_factory=session_factory,
            cache=MagicMock(),
            telegram_bot=AsyncMock(),
            approval_manager=AsyncMock(),
        )

    from src.broker.registry import BrokerRegistry

    assert isinstance(registry, BrokerRegistry)
    # 공통 3 + 계좌별 (token_refresh 없음(mock) + stop_loss + daily) = 3 + 2 = 5
    # 계좌 position 전략 → position_analysis 추가 = 6
    assert len(engine._job_fns) >= 5


@pytest.mark.asyncio
async def test_create_scheduler_no_accounts_no_key():
    """활성 계좌 0개 + KIS_APP_KEY 없음 → 공통 작업만 등록."""
    from contextlib import ExitStack

    settings = make_settings(
        USE_MOCK_BROKER=True,
        SCHEDULER_ENABLED=True,
        KIS_APP_KEY="",
        # NAVER 키 없음 → news_collect 미등록(env .env 유입 차단, 결정론적).
        NAVER_CLIENT_ID="",
        NAVER_CLIENT_SECRET="",
    )
    session_factory, _ = _mock_session_factory()

    with ExitStack() as stack:
        _enter_common_patches(
            stack,
            extra_patches=[
                (
                    "src.scheduler.factory.SchedulerFactory._get_watchlist_symbols",
                    {"new_callable": AsyncMock, "return_value": []},
                ),
                (
                    "src.scheduler.factory.SchedulerFactory._load_active_accounts",
                    {"new_callable": AsyncMock, "return_value": []},
                ),
            ],
        )

        engine, registry, _stream, _sl, _rt = await SchedulerFactory.create_scheduler(
            settings=settings,
            session_factory=session_factory,
            cache=MagicMock(),
            telegram_bot=AsyncMock(),
            approval_manager=AsyncMock(),
        )

    # 공통 작업: pre_open_prep + weekly, monthly, llm_cost + reconcile (midday, eod,
    # positions, intraday) + cleanup_expired_memories. market_data_collect은 provider=None 스킵
    assert len(engine._job_fns) == 9
    assert "pre_open_prep" in engine._job_fns
    assert "weekly_report" in engine._job_fns
    assert "monthly_report" in engine._job_fns
    assert "llm_cost_report" in engine._job_fns
    assert "reconcile_open_orders_midday" in engine._job_fns
    assert "reconcile_open_orders_eod" in engine._job_fns
    assert "reconcile_positions" in engine._job_fns
    assert "sync_positions_intraday" in engine._job_fns
    assert "cleanup_expired_memories" in engine._job_fns


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


def test_get_scheduler_runtime_raises_when_not_initialized():
    """get_scheduler_runtime() — 초기화 전이면 RuntimeError(B-10)."""
    import src.main as main_mod

    original = main_mod._scheduler_runtime
    try:
        main_mod._scheduler_runtime = None
        with pytest.raises(RuntimeError, match="SchedulerRuntime not initialized"):
            main_mod.get_scheduler_runtime()
    finally:
        main_mod._scheduler_runtime = original


# ── SchedulerRuntime.reload_account (B-10) ───────────────────────────────


def _make_account_context(account_id: str, strategy_type):
    """모든 서비스 필드를 MagicMock으로 채운 AccountContext."""
    from src.scheduler.factory import AccountContext

    return AccountContext(
        account_id=account_id,
        nickname="닉",
        account_no="12345678-01",
        broker=MagicMock(),
        auth=None,
        strategy_type=strategy_type,
        portfolio_service=MagicMock(),
        position_manager=MagicMock(),
        exit_checker=MagicMock(),
        exit_service=MagicMock(),
        order_executor=MagicMock(),
        monitor=MagicMock(),
        investment_prompt="",
        risk_tolerance="moderate",
        account_label="닉 (8-01)",
    )


def _make_runtime(engine, contexts):
    from src.scheduler.factory import SchedulerRuntime

    return SchedulerRuntime(
        engine=engine,
        registry=MagicMock(),
        stoploss_stream=MagicMock(),
        session_factory=_mock_session_factory()[0],
        cache=MagicMock(),
        settings=make_settings(SCHEDULER_ENABLED=False),
        telegram_bot=AsyncMock(),
        approval_manager=AsyncMock(),
        orchestrator=MagicMock(),
        recorder=MagicMock(),
        web_verifier=MagicMock(),
        cost_tracker=MagicMock(),
        memory_manager=MagicMock(),
        thesis_monitor=MagicMock(),
        generator=MagicMock(),
        execution_stream=MagicMock(),
        decision_queue=MagicMock(),
        exit_coordinator=MagicMock(),
        watchlist_symbols=["005930"],
        contexts=contexts,
    )


def _make_engine_with_jobs(*job_names):
    from apscheduler.triggers.interval import IntervalTrigger

    from src.scheduler.engine import SchedulerEngine

    engine = SchedulerEngine(
        session_factory=_mock_session_factory()[0],
        settings=make_settings(SCHEDULER_ENABLED=False),
        telegram_bot=AsyncMock(),
    )
    for name in job_names:
        engine.register_job(name, AsyncMock(), IntervalTrigger(minutes=5))
    return engine


@pytest.mark.asyncio
async def test_reload_account_rebuilds_and_reconciles_jobs():
    """전략 타입이 position→swing으로 바뀌면 옛 잡 제거 + 새 잡 등록, 공통 잡은 보존."""
    from src.core.enums import StrategyType
    from src.scheduler.factory import SchedulerFactory

    engine = _make_engine_with_jobs(
        "position_decision:acct-1",
        "stop_loss_check:acct-1",
        "weekly_report",  # 공통 잡 — 보존되어야 함
    )
    old_ctx = _make_account_context("acct-1", StrategyType.POSITION)
    runtime = _make_runtime(engine, [old_ctx])

    new_ctx = _make_account_context("acct-1", StrategyType.SWING)
    account = MagicMock()
    account.id = "acct-1"

    def fake_register(eng, ctx, **kwargs):
        from apscheduler.triggers.interval import IntervalTrigger

        eng.register_job("swing_decision:acct-1", AsyncMock(), IntervalTrigger(minutes=5))
        eng.register_job("stop_loss_check:acct-1", AsyncMock(), IntervalTrigger(minutes=5))

    with (
        patch.object(
            SchedulerFactory, "_load_account", new_callable=AsyncMock, return_value=account
        ),
        patch.object(
            SchedulerFactory, "_build_account_context", return_value=new_ctx
        ) as mock_build,
        patch.object(
            SchedulerFactory, "_register_account_jobs", side_effect=fake_register
        ),
    ):
        ok = await runtime.reload_account("acct-1")

    assert ok is True
    # 빌드는 재로드된 account(새 risk_overrides 반영)로 호출됨
    assert mock_build.call_args.kwargs["account"] is account
    # 옛 전략 잡 제거, 새 전략 잡 등록
    assert "position_decision:acct-1" not in engine._job_fns
    assert "swing_decision:acct-1" in engine._job_fns
    assert "stop_loss_check:acct-1" in engine._job_fns
    # 공통 잡은 그대로
    assert "weekly_report" in engine._job_fns
    # 컨텍스트 교체 + 손절 스트림 재등록
    assert runtime.contexts[0] is new_ctx
    runtime.stoploss_stream.register_account.assert_called_once()


@pytest.mark.asyncio
async def test_reload_account_unknown_id_returns_false():
    from src.core.enums import StrategyType

    engine = _make_engine_with_jobs("stop_loss_check:acct-1")
    runtime = _make_runtime(engine, [_make_account_context("acct-1", StrategyType.POSITION)])

    ok = await runtime.reload_account("acct-2")
    assert ok is False
    # 잡 변동 없음
    assert "stop_loss_check:acct-1" in engine._job_fns


@pytest.mark.asyncio
async def test_reload_account_inactive_returns_false_keeps_context():
    """_load_account이 None(비활성/부재)이면 False, 컨텍스트·잡 불변."""
    from src.core.enums import StrategyType
    from src.scheduler.factory import SchedulerFactory

    engine = _make_engine_with_jobs("position_decision:acct-1")
    old_ctx = _make_account_context("acct-1", StrategyType.POSITION)
    runtime = _make_runtime(engine, [old_ctx])

    with patch.object(
        SchedulerFactory, "_load_account", new_callable=AsyncMock, return_value=None
    ):
        ok = await runtime.reload_account("acct-1")

    assert ok is False
    assert runtime.contexts[0] is old_ctx
    assert "position_decision:acct-1" in engine._job_fns


@pytest.mark.asyncio
async def test_reload_account_build_failure_keeps_old_context():
    """재빌드 중 예외 → False, 기존 컨텍스트 유지(부분 교체 금지)."""
    from src.core.enums import StrategyType
    from src.scheduler.factory import SchedulerFactory

    engine = _make_engine_with_jobs("position_decision:acct-1")
    old_ctx = _make_account_context("acct-1", StrategyType.POSITION)
    runtime = _make_runtime(engine, [old_ctx])
    account = MagicMock()
    account.id = "acct-1"

    with (
        patch.object(
            SchedulerFactory, "_load_account", new_callable=AsyncMock, return_value=account
        ),
        patch.object(
            SchedulerFactory, "_build_account_context", side_effect=RuntimeError("boom")
        ),
    ):
        ok = await runtime.reload_account("acct-1")

    assert ok is False
    assert runtime.contexts[0] is old_ctx


# ── PRJ-03: 수급 수집 잡 등록 (_register_common_jobs 직접 호출) ──────────


def _make_engine(settings):
    from src.scheduler.engine import SchedulerEngine

    sf, _ = _mock_session_factory()
    return SchedulerEngine(session_factory=sf, settings=settings, telegram_bot=None)


def _register_common(engine, settings, provider):
    sf, _ = _mock_session_factory()
    SchedulerFactory._register_common_jobs(
        engine,
        provider=provider,
        watchlist_symbols=["005930"],
        generator=MagicMock(),
        telegram_bot=AsyncMock(),
        settings=settings,
        session_factory=sf,
    )


def test_investor_flow_jobs_registered_by_default():
    """provider 존재 + 플래그 기본값(True) → 수급 잡 2종 등록."""
    settings = make_settings(SCHEDULER_ENABLED=False)
    engine = _make_engine(settings)

    _register_common(engine, settings, provider=AsyncMock())

    assert "investor_flow_collect" in engine._job_fns
    assert "short_interest_collect" in engine._job_fns


def test_investor_flow_jobs_skipped_when_disabled():
    """INVESTOR_FLOW_COLLECTION_ENABLED=False → 수급 잡 미등록."""
    settings = make_settings(
        SCHEDULER_ENABLED=False, INVESTOR_FLOW_COLLECTION_ENABLED=False
    )
    engine = _make_engine(settings)

    _register_common(engine, settings, provider=AsyncMock())

    assert "investor_flow_collect" not in engine._job_fns
    assert "short_interest_collect" not in engine._job_fns
    # 이웃 잡 market_data_collect는 플래그 무관하게 등록되어야 한다
    assert "market_data_collect" in engine._job_fns


def test_investor_flow_jobs_skipped_without_provider():
    """provider=None(InMemoryBroker) → 수급 잡 미등록."""
    settings = make_settings(SCHEDULER_ENABLED=False)
    engine = _make_engine(settings)

    _register_common(engine, settings, provider=None)

    assert "investor_flow_collect" not in engine._job_fns
    assert "short_interest_collect" not in engine._job_fns
