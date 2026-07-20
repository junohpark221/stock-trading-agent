"""Phase 8 Step 8: 스케줄러 다중 계좌 실행 테스트.

- 2계좌 작업 수 검증
- 계좌별 investment_prompt 전달
- 계좌별 position 필터링
- 활성 계좌 0개 graceful
- "default" 레거시 호환
- AccountContext 구조 검증
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.enums import StrategyType
from src.scheduler.engine import SchedulerEngine
from src.scheduler.factory import AccountContext, SchedulerFactory
from src.scheduler.jobs import (
    job_position_decision,
    job_stop_loss_check,
    job_swing_decision,
)
from tests.conftest import make_settings

# 결정/실행 분리 후 _register_*_jobs가 요구하는 추가 의존성(테스트용 목).
_REG_DEPS = dict(session_factory=MagicMock(), decision_queue=MagicMock())

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_mock_account(
    account_id: str = "acct-1",
    nickname: str = "공격형",
    strategy_type: str = "swing",
    investment_prompt: str = "고성장 성장주 위주",
    kis_account_no: str = "50071234-01",
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
    account.is_active = True
    return account


def _make_account_context(
    account_id: str = "acct-1",
    nickname: str = "공격형",
    strategy_type: StrategyType = StrategyType.SWING,
    investment_prompt: str = "고성장 성장주 위주",
    risk_tolerance: str = "moderate",
    account_label: str = "공격형 (4-01)",
) -> AccountContext:
    """AccountContext 테스트용 인스턴스."""
    return AccountContext(
        account_id=account_id,
        nickname=nickname,
        account_no="50071234-01",
        broker=AsyncMock(),
        auth=MagicMock(),
        strategy_type=strategy_type,
        portfolio_service=AsyncMock(),
        position_manager=AsyncMock(),
        exit_checker=MagicMock(),
        exit_service=AsyncMock(),
        order_executor=AsyncMock(),
        monitor=AsyncMock(),
        investment_prompt=investment_prompt,
        risk_tolerance=risk_tolerance,
        account_label=account_label,
    )


# ── AccountContext 구조 검증 ─────────────────────────────────────────────


class TestAccountContext:
    def test_frozen(self):
        """AccountContext는 frozen dataclass."""
        ctx = _make_account_context()
        with pytest.raises(AttributeError):
            ctx.account_id = "new"  # type: ignore[misc]

    def test_fields(self):
        """필수 필드가 모두 존재."""
        ctx = _make_account_context()
        assert ctx.account_id == "acct-1"
        assert ctx.strategy_type == StrategyType.SWING
        assert ctx.investment_prompt == "고성장 성장주 위주"
        assert ctx.account_label == "공격형 (4-01)"


# ── _register_account_jobs 단위 테스트 ───────────────────────────────────


class TestRegisterAccountJobs:
    @pytest.fixture
    def engine(self):
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=session)
        return SchedulerEngine(
            session_factory=factory,
            settings=make_settings(SCHEDULER_ENABLED=True),
        )

    def test_swing_account_registers_swing_job(self, engine):
        """swing 전략 계좌 → swing_decision:{id} + execution_drain 등록, position 미등록."""
        ctx = _make_account_context(
            account_id="acct-1",
            strategy_type=StrategyType.SWING,
        )
        SchedulerFactory._register_account_jobs(
            engine,
            ctx,
            orchestrator=MagicMock(),
            watchlist_symbols=["005930"],
            generator=MagicMock(),
            telegram_bot=AsyncMock(),
            settings=make_settings(),
            **_REG_DEPS,
        )

        job_names = set(engine._job_fns.keys())
        assert "swing_decision:acct-1" in job_names
        assert "position_decision:acct-1" not in job_names
        assert "execution_drain:acct-1" in job_names
        assert "stop_loss_check:acct-1" in job_names
        assert "daily_report:acct-1" in job_names
        assert "token_refresh:acct-1" in job_names

    def test_position_account_registers_position_job(self, engine):
        """position 전략 계좌 → position_decision:{id} 등록, swing 미등록."""
        ctx = _make_account_context(
            account_id="acct-2",
            strategy_type=StrategyType.POSITION,
        )
        SchedulerFactory._register_account_jobs(
            engine,
            ctx,
            orchestrator=MagicMock(),
            watchlist_symbols=["005930"],
            generator=MagicMock(),
            telegram_bot=AsyncMock(),
            settings=make_settings(),
            **_REG_DEPS,
        )

        job_names = set(engine._job_fns.keys())
        assert "position_decision:acct-2" in job_names
        assert "swing_decision:acct-2" not in job_names
        assert "execution_drain:acct-2" in job_names

    def test_mock_broker_no_token_refresh(self, engine):
        """auth=None (mock broker) → token_refresh 미등록."""
        ctx = _make_account_context(account_id="mock-acct")
        # auth=None으로 변경
        ctx = AccountContext(
            account_id="mock-acct",
            nickname="모의",
            account_no="12345678",
            broker=AsyncMock(),
            auth=None,
            strategy_type=StrategyType.SWING,
            portfolio_service=AsyncMock(),
            position_manager=AsyncMock(),
            exit_checker=MagicMock(),
            exit_service=AsyncMock(),
            order_executor=AsyncMock(),
            monitor=AsyncMock(),
            investment_prompt="",
            risk_tolerance="moderate",
            account_label="모의 (5678)",
        )
        SchedulerFactory._register_account_jobs(
            engine,
            ctx,
            orchestrator=MagicMock(),
            watchlist_symbols=[],
            generator=MagicMock(),
            telegram_bot=AsyncMock(),
            settings=make_settings(),
            **_REG_DEPS,
        )

        assert "token_refresh:mock-acct" not in engine._job_fns

    def test_two_accounts_job_count(self, engine):
        """2계좌 (swing + position) 등록 시 작업 수 검증."""
        swing_ctx = _make_account_context(
            account_id="acct-1",
            strategy_type=StrategyType.SWING,
        )
        position_ctx = _make_account_context(
            account_id="acct-2",
            strategy_type=StrategyType.POSITION,
        )
        orchestrator = MagicMock()
        generator = MagicMock()
        telegram_bot = AsyncMock()
        settings = make_settings()

        # 공통 작업 등록
        SchedulerFactory._register_common_jobs(
            engine,
            provider=MagicMock(),
            watchlist_symbols=["005930"],
            generator=generator,
            telegram_bot=telegram_bot,
            settings=settings,
            session_factory=MagicMock(),
        )

        # 계좌별 작업 등록
        account_kwargs = dict(
            orchestrator=orchestrator,
            watchlist_symbols=["005930"],
            generator=generator,
            telegram_bot=telegram_bot,
            settings=settings,
            **_REG_DEPS,
        )
        SchedulerFactory._register_account_jobs(engine, swing_ctx, **account_kwargs)
        SchedulerFactory._register_account_jobs(engine, position_ctx, **account_kwargs)

        job_names = set(engine._job_fns.keys())

        # 공통: market_data_collect, calendar_sync(F-23), investor_flow_collect,
        #       short_interest_collect, pre_open_prep, weekly_report,
        #       monthly_report, llm_cost_report = 8
        assert "market_data_collect" in job_names
        assert "calendar_sync" in job_names
        assert "investor_flow_collect" in job_names
        assert "short_interest_collect" in job_names
        assert "pre_open_prep" in job_names
        assert "weekly_report" in job_names
        assert "monthly_report" in job_names
        assert "llm_cost_report" in job_names

        # acct-1 (swing): token_refresh, swing_decision, execution_drain,
        #                 stop_loss_check, daily_report = 5
        assert "token_refresh:acct-1" in job_names
        assert "swing_decision:acct-1" in job_names
        assert "execution_drain:acct-1" in job_names
        assert "stop_loss_check:acct-1" in job_names
        assert "daily_report:acct-1" in job_names

        # acct-2 (position): token_refresh, position_decision, execution_drain,
        #                    stop_loss_check, daily_report = 5
        assert "token_refresh:acct-2" in job_names
        assert "position_decision:acct-2" in job_names
        assert "execution_drain:acct-2" in job_names
        assert "stop_loss_check:acct-2" in job_names
        assert "daily_report:acct-2" in job_names

        # 총: 8 + 5 + 5 = 18
        assert len(job_names) == 18


# ── job 함수 계좌별 파라미터 전달 ──────────────────────────────────────────


def _empty_session_factory():
    """resolve_symbol_names(종목명 조회)용 세션 팩토리 mock — execute().all()=[].

    결정 잡이 종목명 매핑을 조회하므로(F-19), execute 결과가 빈 리스트를 반환하는
    async 컨텍스트 세션을 yield한다 → names={} (프롬프트는 코드 폴백).
    """
    result = MagicMock()
    result.all.return_value = []
    sess = MagicMock()
    sess.execute = AsyncMock(return_value=result)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=sess)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


class TestJobAccountParams:
    @pytest.mark.asyncio
    async def test_swing_decision_passes_investment_prompt(self, monkeypatch):
        """job_swing_decision이 orchestrator.execute에 investment_prompt 전달."""
        monkeypatch.setattr(
            "src.scheduler.jobs._check_data_freshness",
            AsyncMock(return_value=(True, {"coverage_pct": 100.0, "max_date": "x"})),
        )
        orchestrator = AsyncMock()
        orchestrator.execute = AsyncMock(
            return_value=MagicMock(session_id="test", trade_decisions=[])
        )

        await job_swing_decision(
            orchestrator=orchestrator,
            symbols=["005930"],
            queue=AsyncMock(),
            session_factory=_empty_session_factory(),
            settings=make_settings(),
            account_id="acct-1",
            investment_prompt="aggressive growth",
        )

        orchestrator.execute.assert_awaited_once_with(
            ["005930"],
            investment_prompt="aggressive growth",
            risk_tolerance="moderate",
            account_id="acct-1",
            names={},
        )

    @pytest.mark.asyncio
    async def test_position_decision_filters_by_account(self, monkeypatch):
        """job_position_decision이 account_id로 포지션 필터링."""
        monkeypatch.setattr(
            "src.scheduler.jobs._check_data_freshness",
            AsyncMock(return_value=(True, {"coverage_pct": 100.0, "max_date": "x"})),
        )
        position = MagicMock()
        position.symbol = "005930"

        position_manager = AsyncMock()
        position_manager.get_open = AsyncMock(return_value=[position])

        orchestrator = AsyncMock()
        orchestrator.execute = AsyncMock(
            return_value=MagicMock(session_id="test", trade_decisions=[])
        )

        await job_position_decision(
            orchestrator=orchestrator,
            position_manager=position_manager,
            queue=AsyncMock(),
            session_factory=_empty_session_factory(),
            settings=make_settings(),
            account_id="acct-2",
            investment_prompt="value investing",
        )

        position_manager.get_open.assert_awaited_once_with(account_id="acct-2")
        orchestrator.execute.assert_awaited_once_with(
            ["005930"],
            investment_prompt="value investing",
            risk_tolerance="moderate",
            account_id="acct-2",
            names={},
        )

    @pytest.mark.asyncio
    async def test_position_decision_no_positions_skip(self, monkeypatch):
        """포지션 없으면 orchestrator 미호출."""
        monkeypatch.setattr(
            "src.scheduler.jobs._check_data_freshness",
            AsyncMock(return_value=(True, {"coverage_pct": 100.0, "max_date": "x"})),
        )
        position_manager = AsyncMock()
        position_manager.get_open = AsyncMock(return_value=[])

        orchestrator = AsyncMock()

        await job_position_decision(
            orchestrator=orchestrator,
            position_manager=position_manager,
            queue=AsyncMock(),
            session_factory=MagicMock(),
            settings=make_settings(),
            account_id="acct-2",
        )

        orchestrator.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_position_decision_scans_universe_for_new_entries(self, monkeypatch):
        """보유 0이어도 scan_universe 후보가 있으면 분석(신규 진입)."""
        monkeypatch.setattr(
            "src.scheduler.jobs._check_data_freshness",
            AsyncMock(return_value=(True, {"coverage_pct": 100.0, "max_date": "x"})),
        )
        position_manager = AsyncMock()
        position_manager.get_open = AsyncMock(return_value=[])  # 보유 없음

        strategy = AsyncMock()
        strategy.scan_universe = AsyncMock(return_value=["035420", "000660"])

        orchestrator = AsyncMock()
        orchestrator.execute = AsyncMock(
            return_value=MagicMock(session_id="test", trade_decisions=[])
        )

        await job_position_decision(
            orchestrator=orchestrator,
            position_manager=position_manager,
            queue=AsyncMock(),
            session_factory=_empty_session_factory(),
            settings=make_settings(),
            strategy=strategy,
            account_id="acct-2",
        )

        strategy.scan_universe.assert_awaited_once()
        orchestrator.execute.assert_awaited_once()
        called_symbols = orchestrator.execute.await_args.args[0]
        assert set(called_symbols) == {"035420", "000660"}

    @pytest.mark.asyncio
    async def test_decision_skips_on_stale_data(self, monkeypatch):
        """신선도 미달이면 분석을 보류(orchestrator 미호출)."""
        monkeypatch.setattr(
            "src.scheduler.jobs._check_data_freshness",
            AsyncMock(return_value=(False, {"coverage_pct": 10.0, "max_date": None})),
        )
        orchestrator = AsyncMock()
        await job_swing_decision(
            orchestrator=orchestrator,
            symbols=["005930"],
            queue=AsyncMock(),
            session_factory=MagicMock(),
            settings=make_settings(),
            account_id="acct-1",
        )
        orchestrator.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_loss_check_account_filtering(self):
        """job_stop_loss_check가 account_id로 포지션 필터링."""
        position_manager = AsyncMock()
        position_manager.get_open = AsyncMock(return_value=[])
        monitor = AsyncMock()

        with patch("src.scheduler.jobs._is_market_open", return_value=True):
            await job_stop_loss_check(
                exit_checker=MagicMock(),
                exit_service=AsyncMock(),
                position_manager=position_manager,
                portfolio_service=AsyncMock(),
                broker=AsyncMock(),
                monitor=monitor,
                account_id="acct-1",
                account_label="공격형 (1234)",
                market_open="00:00",
                market_close="23:59",
            )

        position_manager.get_open.assert_awaited_once_with(account_id="acct-1")


# ── _register_common_jobs 단위 테스트 ────────────────────────────────────


class TestRegisterCommonJobs:
    @pytest.fixture
    def engine(self):
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=session)
        return SchedulerEngine(
            session_factory=factory,
            settings=make_settings(SCHEDULER_ENABLED=True),
        )

    def test_with_provider(self, engine):
        """provider 있으면 market_data_collect + calendar_sync + 수급 2종 + pre_open_prep 포함 8개 등록."""
        SchedulerFactory._register_common_jobs(
            engine,
            provider=MagicMock(),
            watchlist_symbols=["005930"],
            generator=MagicMock(),
            telegram_bot=AsyncMock(),
            settings=make_settings(),
            session_factory=MagicMock(),
        )
        assert len(engine._job_fns) == 8
        assert "market_data_collect" in engine._job_fns
        assert "calendar_sync" in engine._job_fns
        assert "investor_flow_collect" in engine._job_fns
        assert "short_interest_collect" in engine._job_fns
        assert "pre_open_prep" in engine._job_fns

    def test_without_provider(self, engine):
        """provider=None이면 market_data_collect 미등록, pre_open_prep + 리포트 3개 = 4개."""
        SchedulerFactory._register_common_jobs(
            engine,
            provider=None,
            watchlist_symbols=[],
            generator=MagicMock(),
            telegram_bot=AsyncMock(),
            settings=make_settings(),
            session_factory=MagicMock(),
        )
        assert len(engine._job_fns) == 4
        assert "market_data_collect" not in engine._job_fns
        assert "pre_open_prep" in engine._job_fns
        assert "weekly_report" in engine._job_fns
        assert "monthly_report" in engine._job_fns
        assert "llm_cost_report" in engine._job_fns
