"""Phase 8 Step 8: 스케줄러 다중 계좌 실행 테스트.

- 2계좌 작업 수 검증
- 계좌별 investment_prompt 전달
- 계좌별 position 필터링
- 활성 계좌 0개 graceful
- "default" 레거시 호환
- AccountContext 구조 검증
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.enums import StrategyType
from src.scheduler.engine import SchedulerEngine
from src.scheduler.factory import AccountContext, SchedulerFactory
from src.scheduler.jobs import (
    job_position_analysis,
    job_stop_loss_check,
    job_swing_analysis,
)
from tests.conftest import make_settings

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
        """swing 전략 계좌 → swing_analysis:{id} 등록, position_analysis 미등록."""
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
        )

        job_names = set(engine._job_fns.keys())
        assert "swing_analysis:acct-1" in job_names
        assert "position_analysis:acct-1" not in job_names
        assert "stop_loss_check:acct-1" in job_names
        assert "daily_report:acct-1" in job_names
        assert "token_refresh:acct-1" in job_names

    def test_position_account_registers_position_job(self, engine):
        """position 전략 계좌 → position_analysis:{id} 등록, swing 미등록."""
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
        )

        job_names = set(engine._job_fns.keys())
        assert "position_analysis:acct-2" in job_names
        assert "swing_analysis:acct-2" not in job_names

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
        )

        # 계좌별 작업 등록
        account_kwargs = dict(
            orchestrator=orchestrator,
            watchlist_symbols=["005930"],
            generator=generator,
            telegram_bot=telegram_bot,
            settings=settings,
        )
        SchedulerFactory._register_account_jobs(engine, swing_ctx, **account_kwargs)
        SchedulerFactory._register_account_jobs(engine, position_ctx, **account_kwargs)

        job_names = set(engine._job_fns.keys())

        # 공통: market_data_collect, weekly_report, monthly_report, llm_cost_report = 4
        assert "market_data_collect" in job_names
        assert "weekly_report" in job_names
        assert "monthly_report" in job_names
        assert "llm_cost_report" in job_names

        # acct-1 (swing): token_refresh, swing_analysis, stop_loss_check, daily_report = 4
        assert "token_refresh:acct-1" in job_names
        assert "swing_analysis:acct-1" in job_names
        assert "stop_loss_check:acct-1" in job_names
        assert "daily_report:acct-1" in job_names

        # acct-2 (position): token_refresh, position_analysis, stop_loss_check, daily_report = 4
        assert "token_refresh:acct-2" in job_names
        assert "position_analysis:acct-2" in job_names
        assert "stop_loss_check:acct-2" in job_names
        assert "daily_report:acct-2" in job_names

        # 총: 4 + 4 + 4 = 12
        assert len(job_names) == 12


# ── job 함수 계좌별 파라미터 전달 ──────────────────────────────────────────


class TestJobAccountParams:
    @pytest.mark.asyncio
    async def test_swing_analysis_passes_investment_prompt(self):
        """job_swing_analysis가 orchestrator.execute에 investment_prompt 전달."""
        orchestrator = AsyncMock()
        orchestrator.execute = AsyncMock(return_value=MagicMock(session_id="test"))

        await job_swing_analysis(
            orchestrator=orchestrator,
            symbols=["005930"],
            account_id="acct-1",
            investment_prompt="aggressive growth",
        )

        orchestrator.execute.assert_awaited_once_with(
            ["005930"],
            investment_prompt="aggressive growth",
            account_id="acct-1",
        )

    @pytest.mark.asyncio
    async def test_position_analysis_filters_by_account(self):
        """job_position_analysis가 account_id로 포지션 필터링."""
        position = MagicMock()
        position.symbol = "005930"

        position_manager = AsyncMock()
        position_manager.get_open = AsyncMock(return_value=[position])

        orchestrator = AsyncMock()
        orchestrator.execute = AsyncMock(return_value=MagicMock(session_id="test"))

        await job_position_analysis(
            orchestrator=orchestrator,
            position_manager=position_manager,
            account_id="acct-2",
            investment_prompt="value investing",
        )

        position_manager.get_open.assert_awaited_once_with(account_id="acct-2")
        orchestrator.execute.assert_awaited_once_with(
            ["005930"],
            investment_prompt="value investing",
            account_id="acct-2",
        )

    @pytest.mark.asyncio
    async def test_position_analysis_no_positions_skip(self):
        """포지션 없으면 orchestrator 미호출."""
        position_manager = AsyncMock()
        position_manager.get_open = AsyncMock(return_value=[])

        orchestrator = AsyncMock()

        await job_position_analysis(
            orchestrator=orchestrator,
            position_manager=position_manager,
            account_id="acct-2",
        )

        orchestrator.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_loss_check_account_filtering(self):
        """job_stop_loss_check가 account_id로 포지션 필터링."""
        position_manager = AsyncMock()
        position_manager.get_open = AsyncMock(return_value=[])
        monitor = AsyncMock()

        await job_stop_loss_check(
            exit_checker=MagicMock(),
            exit_service=AsyncMock(),
            position_manager=position_manager,
            portfolio_service=AsyncMock(),
            broker=AsyncMock(),
            monitor=monitor,
            account_id="acct-1",
            account_label="공격형 (1234)",
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
        """provider 있으면 market_data_collect 포함 4개 등록."""
        SchedulerFactory._register_common_jobs(
            engine,
            provider=MagicMock(),
            watchlist_symbols=["005930"],
            generator=MagicMock(),
            telegram_bot=AsyncMock(),
            settings=make_settings(),
        )
        assert len(engine._job_fns) == 4
        assert "market_data_collect" in engine._job_fns

    def test_without_provider(self, engine):
        """provider=None이면 market_data_collect 미등록, 3개만."""
        SchedulerFactory._register_common_jobs(
            engine,
            provider=None,
            watchlist_symbols=[],
            generator=MagicMock(),
            telegram_bot=AsyncMock(),
            settings=make_settings(),
        )
        assert len(engine._job_fns) == 3
        assert "market_data_collect" not in engine._job_fns
        assert "weekly_report" in engine._job_fns
        assert "monthly_report" in engine._job_fns
        assert "llm_cost_report" in engine._job_fns
