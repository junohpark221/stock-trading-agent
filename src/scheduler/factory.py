"""Scheduler dependency factory — assembles all service instances for jobs.

DB에서 활성 계좌를 로드하고, 공통 작업 1회 + 계좌별 작업 N회를 등록한다.
한 번 조립된 인스턴스를 SchedulerEngine의 전체 생명주기 동안 재사용.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

import structlog
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from src.broker.credentials import AccountCredentials
from src.broker.registry import BrokerRegistry
from src.core.enums import StrategyType
from src.db.models.account import Account, AccountCrypto
from src.db.models.market_data import StockMaster
from src.scheduler.engine import SchedulerEngine
from src.scheduler.jobs import (
    job_daily_report,
    job_llm_cost_report,
    job_market_data_collect,
    job_monthly_report,
    job_position_analysis,
    job_stop_loss_check,
    job_swing_analysis,
    job_token_refresh,
    job_weekly_report,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.agent.orchestrator import PipelineOrchestrator
    from src.broker.base import BrokerInterface
    from src.broker.kis.auth import KISAuth
    from src.config import Settings
    from src.data.cache import RedisCache
    from src.data.providers.base import DataProvider
    from src.execution.executor import OrderExecutor
    from src.execution.exit_executor import ExitExecutionService
    from src.notification.telegram import TelegramBot
    from src.report.generator import ReportGenerator
    from src.scheduler.monitor import TradingMonitor
    from src.strategy.exit_checker import ExitConditionChecker
    from src.strategy.portfolio_state import PortfolioStateService
    from src.strategy.position_manager import PositionManager

logger = structlog.get_logger(__name__)


# ── AccountContext ────────────────────────────────────────────────────


@dataclass(frozen=True)
class AccountContext:
    """계좌별 서비스 인스턴스 번들.

    SchedulerFactory가 각 계좌에 대해 하나씩 생성하며,
    per-account 작업 등록 시 필요한 모든 의존성을 담고 있다.
    """

    account_id: str
    nickname: str
    account_no: str
    broker: BrokerInterface
    auth: KISAuth | None
    strategy_type: StrategyType
    portfolio_service: PortfolioStateService
    position_manager: PositionManager
    exit_checker: ExitConditionChecker
    exit_service: ExitExecutionService
    order_executor: OrderExecutor
    monitor: TradingMonitor
    investment_prompt: str
    account_label: str  # "닉네임 (뒤4자리)"


# ── SchedulerFactory ──────────────────────────────────────────────────


class SchedulerFactory:
    """스케줄러 의존성 팩토리.

    DB에서 활성 계좌를 로드하고, 공유 의존성 1회 생성 + 계좌별 서비스 N회 생성.
    공통 작업 + 계좌별 작업을 SchedulerEngine에 등록한다.
    """

    @staticmethod
    async def create_scheduler(
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        cache: RedisCache,
        telegram_bot: TelegramBot,
    ) -> tuple[SchedulerEngine, BrokerRegistry]:
        """서비스 그래프 조립 → SchedulerEngine 반환.

        Returns:
            (scheduler_engine, broker_registry) — registry는 main.py shutdown에서
            disconnect_all() 필요.
        """
        from src.agent.agents.market_analyst import MarketAnalyst
        from src.agent.agents.risk_manager import RiskManager
        from src.agent.agents.stock_analyst import StockAnalyst
        from src.agent.agents.trader import Trader
        from src.agent.decision_recorder import DecisionRecorder
        from src.agent.tools.context import ToolContext
        from src.agent.tools.registry import ToolRegistry
        from src.execution.approval import ApprovalManager
        from src.execution.web_verify import WebSearchVerifier
        from src.llm.cost_tracker import CostTracker
        from src.llm.router import LLMRouter
        from src.report.data_fetcher import ReportDataFetcher
        from src.report.generator import ReportGenerator

        # ── 1. 활성 계좌 로드 ─────────────────────────────────────────
        accounts = await SchedulerFactory._load_active_accounts(session_factory)

        # 레거시 호환: DB 계좌 없고 KIS_APP_KEY 설정 시 "default" 합성
        synthetic_account: Account | None = None
        if not accounts and settings.KIS_APP_KEY:
            synthetic_account = SchedulerFactory._synthesize_default_account(settings)
            accounts = [synthetic_account]
            logger.info("scheduler_factory.legacy_default_account_synthesized")

        if not accounts:
            logger.warning("scheduler_factory.no_active_accounts")

        # ── 2. BrokerRegistry 생성 + 계좌별 브로커 등록 ───────────────
        registry = BrokerRegistry(cache=cache, use_mock=settings.USE_MOCK_BROKER)
        registered_accounts: list[Account] = []

        for account in accounts:
            try:
                if synthetic_account is not None and account is synthetic_account:
                    # 합성 계좌: env var에서 직접 credential 생성
                    creds = AccountCredentials(
                        account_id="default",
                        app_key=settings.KIS_APP_KEY,
                        app_secret=settings.KIS_APP_SECRET,
                        account_no=settings.KIS_ACCOUNT_NO,
                        account_prod=settings.KIS_ACCOUNT_PROD,
                        is_paper=settings.KIS_IS_PAPER,
                        hts_id=settings.KIS_HTS_ID,
                    )
                else:
                    creds = SchedulerFactory._decrypt_credentials(
                        account,
                        settings.ACCOUNT_ENCRYPTION_KEY,
                    )
                await registry.register(creds)
                registered_accounts.append(account)
            except Exception:
                logger.exception(
                    "scheduler_factory.account_register_failed",
                    account_id=account.id,
                )

        # ── 3. 공유 의존성 1회 생성 ───────────────────────────────────
        cost_tracker = CostTracker(session_factory, settings)
        llm_router = LLMRouter(
            session_factory=session_factory,
            settings=settings,
            cost_tracker=cost_tracker,
            cache=cache,
        )

        from src.agent.orchestrator import PipelineOrchestrator

        recorder = DecisionRecorder(session_factory)
        tool_ctx = ToolContext(session_factory=session_factory, settings=settings)
        tool_registry = ToolRegistry(tool_ctx)

        orchestrator = PipelineOrchestrator(
            market_analyst=MarketAnalyst(llm_router, recorder, tool_registry),
            stock_analyst=StockAnalyst(llm_router, recorder, tool_registry),
            risk_manager=RiskManager(llm_router, recorder, tool_registry),
            trader=Trader(llm_router, recorder, tool_registry),
            recorder=recorder,
        )

        web_verifier = WebSearchVerifier(
            llm_router=llm_router,
            recorder=recorder,
            settings=settings,
        )
        approval_manager = ApprovalManager(
            telegram_bot=telegram_bot,
            recorder=recorder,
            session_factory=session_factory,
            cache=cache,
            settings=settings,
        )
        await approval_manager.initialize()

        data_fetcher = ReportDataFetcher(session_factory)
        generator = ReportGenerator(
            data_fetcher=data_fetcher,
            cost_tracker=cost_tracker,
            settings=settings,
        )

        # ── 4. DataProvider — 첫 번째 실제 브로커 사용 ────────────────
        provider: DataProvider | None = None
        if not settings.USE_MOCK_BROKER and registered_accounts:
            from src.data.providers.kis_provider import KISDataProvider

            first_broker = registry.get(registered_accounts[0].id)
            provider = KISDataProvider(
                client=first_broker,  # type: ignore[arg-type]
                cache=cache,
                session_factory=session_factory,
                settings=settings,
            )

        # ── 5. 계좌별 AccountContext 생성 ─────────────────────────────
        contexts: list[AccountContext] = []
        for account in registered_accounts:
            try:
                broker = registry.get(account.id)
                ctx = SchedulerFactory._build_account_context(
                    account=account,
                    broker=broker,
                    orchestrator=orchestrator,
                    recorder=recorder,
                    web_verifier=web_verifier,
                    approval_manager=approval_manager,
                    cost_tracker=cost_tracker,
                    telegram_bot=telegram_bot,
                    session_factory=session_factory,
                    cache=cache,
                    settings=settings,
                )
                contexts.append(ctx)
            except Exception:
                logger.exception(
                    "scheduler_factory.account_context_build_failed",
                    account_id=account.id,
                )

        # ── 6. SchedulerEngine 생성 + 작업 등록 ──────────────────────
        engine = SchedulerEngine(
            session_factory=session_factory,
            settings=settings,
        )

        watchlist_symbols = await SchedulerFactory._get_watchlist_symbols(
            session_factory,
        )

        # 공통 작업 등록
        SchedulerFactory._register_common_jobs(
            engine,
            provider=provider,
            watchlist_symbols=watchlist_symbols,
            generator=generator,
            telegram_bot=telegram_bot,
            settings=settings,
        )

        # 계좌별 작업 등록
        for ctx in contexts:
            SchedulerFactory._register_account_jobs(
                engine,
                ctx,
                orchestrator=orchestrator,
                watchlist_symbols=watchlist_symbols,
                generator=generator,
                telegram_bot=telegram_bot,
                settings=settings,
            )

        logger.info(
            "scheduler_factory.created",
            account_count=len(contexts),
            job_count=len(engine._job_fns),
            watchlist_count=len(watchlist_symbols),
        )

        return engine, registry

    # ── Account Loading ──────────────────────────────────────────────

    @staticmethod
    async def _load_active_accounts(
        session_factory: async_sessionmaker[AsyncSession],
    ) -> list[Account]:
        """DB에서 활성 계좌 목록 로드. 에러 시 빈 리스트."""
        try:
            async with session_factory() as session:
                result = await session.execute(
                    select(Account).where(Account.is_active.is_(True)).order_by(Account.id),
                )
                return list(result.scalars().all())
        except Exception:
            logger.warning(
                "scheduler_factory.load_accounts_failed",
                exc_info=True,
            )
            return []

    @staticmethod
    def _synthesize_default_account(settings: Settings) -> Account:
        """env var 기반 레거시 "default" Account 객체 생성 (DB 저장 안 함)."""
        return Account(
            id="default",
            nickname="default",
            kis_app_key_enc="",  # 합성 계좌는 암호화 불필요 (env var 직접 사용)
            kis_app_secret_enc="",
            kis_account_no=settings.KIS_ACCOUNT_NO,
            kis_account_prod=settings.KIS_ACCOUNT_PROD,
            kis_is_paper=settings.KIS_IS_PAPER,
            kis_hts_id=settings.KIS_HTS_ID,
            strategy_type="position",
            investment_prompt="",
            risk_overrides=None,
            is_active=True,
        )

    @staticmethod
    def _decrypt_credentials(
        account: Account,
        encryption_key: str,
    ) -> AccountCredentials:
        """Account ORM → AccountCredentials 변환 (복호화)."""
        return AccountCredentials(
            account_id=account.id,
            app_key=AccountCrypto.decrypt(account.kis_app_key_enc, encryption_key),
            app_secret=AccountCrypto.decrypt(account.kis_app_secret_enc, encryption_key),
            account_no=account.kis_account_no,
            account_prod=account.kis_account_prod,
            is_paper=account.kis_is_paper,
            hts_id=account.kis_hts_id,
        )

    @staticmethod
    def _make_account_label(account: Account) -> str:
        """계좌 표시 라벨 생성: "닉네임 (뒤4자리)" 또는 "뒤4자리"."""
        acct_no = account.kis_account_no
        last4 = acct_no[-4:] if len(acct_no) >= 4 else acct_no
        if account.nickname and account.nickname != account.id:
            return f"{account.nickname} ({last4})"
        return last4

    # ── Account Context Building ─────────────────────────────────────

    @staticmethod
    def _build_account_context(
        *,
        account: Account,
        broker: BrokerInterface,
        orchestrator: PipelineOrchestrator,
        recorder: object,
        web_verifier: object,
        approval_manager: object,
        cost_tracker: object,
        telegram_bot: TelegramBot,
        session_factory: async_sessionmaker[AsyncSession],
        cache: RedisCache,
        settings: Settings,
    ) -> AccountContext:
        """계좌별 서비스 인스턴스를 조립하여 AccountContext를 반환한다."""
        from src.execution.executor import OrderExecutor
        from src.execution.exit_executor import ExitExecutionService
        from src.scheduler.monitor import TradingMonitor
        from src.strategy.exit_checker import ExitConditionChecker
        from src.strategy.portfolio_state import PortfolioStateService
        from src.strategy.position_manager import PositionManager
        from src.strategy.registry import _apply_risk_overrides
        from src.strategy.risk_manager import AlgoRiskManager

        account_id = account.id
        account_label = SchedulerFactory._make_account_label(account)
        strategy_type = StrategyType(account.strategy_type)

        # risk_overrides 적용
        acct_settings = settings
        if account.risk_overrides:
            acct_settings = _apply_risk_overrides(settings, account.risk_overrides)

        # 계좌별 서비스 인스턴스
        position_manager = PositionManager(session_factory)
        portfolio_service = PortfolioStateService(
            broker=broker,
            session_factory=session_factory,
            cache=cache,
            account_id=account_id,
        )
        exit_checker = ExitConditionChecker(
            max_drawdown_pct=acct_settings.MAX_DRAWDOWN_PCT,
        )
        algo_risk_manager = AlgoRiskManager(
            portfolio_service=portfolio_service,
            session_factory=session_factory,
            settings=acct_settings,
        )
        order_executor = OrderExecutor(
            broker=broker,
            web_verifier=web_verifier,
            approval_manager=approval_manager,
            risk_manager=algo_risk_manager,
            position_manager=position_manager,
            portfolio_service=portfolio_service,
            recorder=recorder,
            telegram_bot=telegram_bot,
            session_factory=session_factory,
            settings=acct_settings,
        )
        exit_service = ExitExecutionService(
            order_executor=order_executor,
            position_manager=position_manager,
            portfolio_service=portfolio_service,
            recorder=recorder,
            telegram_bot=telegram_bot,
        )
        monitor = TradingMonitor(
            portfolio_state_service=portfolio_service,
            position_manager=position_manager,
            cost_tracker=cost_tracker,
            telegram_bot=telegram_bot,
            broker=broker,
            cache=cache,
            settings=acct_settings,
            account_id=account_id,
            account_label=account_label,
        )

        # KISAuth — KISClient일 때만
        auth: KISAuth | None = None
        if not settings.USE_MOCK_BROKER and hasattr(broker, "_auth"):
            auth = broker._auth  # type: ignore[attr-defined]

        return AccountContext(
            account_id=account_id,
            nickname=account.nickname,
            account_no=account.kis_account_no,
            broker=broker,
            auth=auth,
            strategy_type=strategy_type,
            portfolio_service=portfolio_service,
            position_manager=position_manager,
            exit_checker=exit_checker,
            exit_service=exit_service,
            order_executor=order_executor,
            monitor=monitor,
            investment_prompt=account.investment_prompt,
            account_label=account_label,
        )

    # ── Job Registration ─────────────────────────────────────────────

    @staticmethod
    def _register_common_jobs(
        engine: SchedulerEngine,
        *,
        provider: DataProvider | None,
        watchlist_symbols: list[str],
        generator: ReportGenerator,
        telegram_bot: TelegramBot,
        settings: Settings,
    ) -> None:
        """공통 작업 등록 (계좌 수에 무관하게 1회씩)."""
        s = settings

        # market_data_collect — InMemoryBroker일 때 스킵
        if provider is not None:
            md_h, md_m = SchedulerEngine._parse_time(s.MARKET_DATA_COLLECTION_TIME)
            engine.register_job(
                "market_data_collect",
                partial(job_market_data_collect, provider=provider, symbols=watchlist_symbols),
                CronTrigger(hour=md_h, minute=md_m, timezone="UTC"),
            )

        # weekly_report (통합 리포트, account_id 없음)
        wr_h, wr_m = SchedulerEngine._parse_time(s.WEEKLY_REPORT_TIME)
        wr_day = SchedulerEngine._parse_day_of_week(s.WEEKLY_REPORT_DAY)
        engine.register_job(
            "weekly_report",
            partial(job_weekly_report, generator=generator, telegram_bot=telegram_bot),
            CronTrigger(day_of_week=wr_day, hour=wr_h, minute=wr_m, timezone="UTC"),
        )

        # monthly_report (통합 리포트)
        mr_h, mr_m = SchedulerEngine._parse_time(s.MONTHLY_REPORT_TIME)
        engine.register_job(
            "monthly_report",
            partial(job_monthly_report, generator=generator, telegram_bot=telegram_bot),
            CronTrigger(day=s.MONTHLY_REPORT_DAY, hour=mr_h, minute=mr_m, timezone="UTC"),
        )

        # llm_cost_report
        lc_h, lc_m = SchedulerEngine._parse_time(s.LLM_COST_REPORT_TIME)
        lc_day = SchedulerEngine._parse_day_of_week(s.LLM_COST_REPORT_DAY)
        engine.register_job(
            "llm_cost_report",
            partial(job_llm_cost_report, generator=generator, telegram_bot=telegram_bot),
            CronTrigger(day_of_week=lc_day, hour=lc_h, minute=lc_m, timezone="UTC"),
        )

    @staticmethod
    def _register_account_jobs(
        engine: SchedulerEngine,
        ctx: AccountContext,
        *,
        orchestrator: PipelineOrchestrator,
        watchlist_symbols: list[str],
        generator: ReportGenerator,
        telegram_bot: TelegramBot,
        settings: Settings,
    ) -> None:
        """계좌별 작업 등록. 작업 이름: ``{job_type}:{account_id}``."""
        s = settings
        aid = ctx.account_id

        # token_refresh:{account_id} — mock broker면 스킵
        if ctx.auth is not None:
            tr_h, tr_m = SchedulerEngine._parse_time(s.TOKEN_REFRESH_TIME)
            engine.register_job(
                f"token_refresh:{aid}",
                partial(job_token_refresh, auth=ctx.auth, account_id=aid),
                CronTrigger(hour=tr_h, minute=tr_m, timezone="UTC"),
            )

        # swing_analysis:{account_id} — swing 전략 계좌만
        if ctx.strategy_type == StrategyType.SWING:
            sw_h, sw_m = SchedulerEngine._parse_time(s.SWING_ANALYSIS_TIME)
            engine.register_job(
                f"swing_analysis:{aid}",
                partial(
                    job_swing_analysis,
                    orchestrator=orchestrator,
                    symbols=watchlist_symbols,
                    account_id=aid,
                    investment_prompt=ctx.investment_prompt,
                ),
                CronTrigger(hour=sw_h, minute=sw_m, timezone="UTC"),
            )

        # position_analysis:{account_id} — position 전략 계좌만
        if ctx.strategy_type == StrategyType.POSITION:
            pa_h, pa_m = SchedulerEngine._parse_time(s.POSITION_ANALYSIS_TIME)
            pa_days = SchedulerEngine._parse_day_of_week(s.POSITION_ANALYSIS_DAYS)
            engine.register_job(
                f"position_analysis:{aid}",
                partial(
                    job_position_analysis,
                    orchestrator=orchestrator,
                    position_manager=ctx.position_manager,
                    account_id=aid,
                    investment_prompt=ctx.investment_prompt,
                ),
                CronTrigger(
                    day_of_week=pa_days,
                    hour=pa_h,
                    minute=pa_m,
                    timezone="UTC",
                ),
            )

        # stop_loss_check:{account_id} — 모든 계좌
        engine.register_job(
            f"stop_loss_check:{aid}",
            partial(
                job_stop_loss_check,
                exit_checker=ctx.exit_checker,
                exit_service=ctx.exit_service,
                position_manager=ctx.position_manager,
                portfolio_service=ctx.portfolio_service,
                broker=ctx.broker,
                monitor=ctx.monitor,
                account_id=aid,
                account_label=ctx.account_label,
            ),
            IntervalTrigger(minutes=s.STOP_LOSS_CHECK_INTERVAL_MIN),
        )

        # daily_report:{account_id} — 모든 계좌
        dr_h, dr_m = SchedulerEngine._parse_time(s.DAILY_REPORT_TIME)
        engine.register_job(
            f"daily_report:{aid}",
            partial(
                job_daily_report,
                generator=generator,
                telegram_bot=telegram_bot,
                portfolio_service=ctx.portfolio_service,
                account_id=aid,
                account_label=ctx.account_label,
            ),
            CronTrigger(hour=dr_h, minute=dr_m, timezone="UTC"),
        )

    # ── Watchlist ────────────────────────────────────────────────────

    @staticmethod
    async def _get_watchlist_symbols(
        session_factory: async_sessionmaker[AsyncSession],
    ) -> list[str]:
        """stock_master 테이블에서 감시 종목 목록 조회.

        비어있으면 빈 리스트 (분석 작업 gracefully 스킵).
        """
        try:
            async with session_factory() as session:
                result = await session.execute(
                    select(StockMaster.symbol).order_by(StockMaster.symbol),
                )
                symbols = [row[0] for row in result.all()]
                logger.info(
                    "scheduler_factory.watchlist_loaded",
                    count=len(symbols),
                )
                return symbols
        except Exception:
            logger.warning(
                "scheduler_factory.watchlist_load_failed",
                exc_info=True,
            )
            return []
