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
    job_cleanup_expired_memories,
    job_daily_report,
    job_execution_drain,
    job_llm_cost_report,
    job_market_data_collect,
    job_monthly_report,
    job_news_collect,
    job_position_decision,
    job_pre_open_prep,
    job_reconcile_open_orders,
    job_reconcile_positions,
    job_stop_loss_check,
    job_swing_decision,
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
    from src.data.providers.naver_provider import NaverProvider
    from src.execution.approval import ApprovalManager
    from src.execution.decision_queue import TradeDecisionQueueManager
    from src.execution.execution_stream import ExecutionStreamManager
    from src.execution.executor import OrderExecutor
    from src.execution.exit_coordinator import ExitCoordinator
    from src.execution.exit_executor import ExitExecutionService
    from src.execution.reconciler import OrderReconciler, PositionReconciler
    from src.execution.stoploss_stream import StopLossStreamService
    from src.notification.telegram import TelegramBot
    from src.report.generator import ReportGenerator
    from src.scheduler.monitor import TradingMonitor
    from src.strategy.base import Strategy
    from src.strategy.batch_allocator import BatchBudgetAllocator
    from src.strategy.exit_checker import ExitConditionChecker
    from src.strategy.memory_manager import AgentMemoryManager
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
    risk_tolerance: str
    account_label: str  # "닉네임 (뒤4자리)"
    allocator: BatchBudgetAllocator | None = None
    strategy: Strategy | None = None


# ── SchedulerRuntime ──────────────────────────────────────────────────


@dataclass
class SchedulerRuntime:
    """실행 중 스케줄러 재구성에 필요한 재료 묶음 (B-10).

    ``create_scheduler``가 만든 공유 의존성·계좌 컨텍스트·watchlist 등을 보관해,
    백오피스에서 계좌 설정이 바뀌면 프로세스 재시작 없이 해당 계좌의 컨텍스트를
    재빌드하고 잡을 재등록(``reload_account``)할 수 있게 한다.

    ``contexts``는 등록 순서를 보존하는 리스트로, 리스트 인덱스가 곧
    ``account_index``(cron 시차 부여)다 — 재빌드 시 같은 인덱스를 재사용한다.
    """

    engine: SchedulerEngine
    registry: BrokerRegistry
    stoploss_stream: StopLossStreamService
    session_factory: async_sessionmaker[AsyncSession]
    cache: RedisCache
    settings: Settings
    telegram_bot: TelegramBot
    approval_manager: ApprovalManager
    orchestrator: PipelineOrchestrator
    recorder: object
    web_verifier: object
    cost_tracker: object
    memory_manager: AgentMemoryManager
    generator: ReportGenerator
    execution_stream: ExecutionStreamManager
    decision_queue: TradeDecisionQueueManager
    exit_coordinator: ExitCoordinator
    watchlist_symbols: list[str]
    contexts: list[AccountContext]
    naver_provider: NaverProvider | None = None

    async def reload_account(self, account_id: str) -> bool:
        """계좌 컨텍스트를 재빌드하고 해당 계좌 잡을 재등록한다.

        risk_overrides·투자철학·risk_tolerance·strategy_type 등 컨텍스트 파생값을
        새 DB 상태로 일관 갱신한다. 전략 타입이 바뀐 경우 옛 전략의 잡이 남지
        않도록 **해당 계좌의 기존 잡을 모두 제거한 뒤 새로 등록**한다(이벤트 루프
        양보 지점이 없어 스왑은 원자적). KIS 인증정보 변경은 registry의 브로커를
        재생성하지 않으므로 본 경로로 반영되지 않는다(재시작 필요).

        Returns:
            성공 시 True. 계좌가 startup 컨텍스트에 없거나(비활성/실패) 재빌드 중
            예외가 나면 False(이 경우 기존 컨텍스트·잡을 그대로 유지).
        """
        idx = next(
            (i for i, c in enumerate(self.contexts) if c.account_id == account_id),
            None,
        )
        if idx is None:
            logger.warning(
                "scheduler_runtime.reload_account_not_found",
                account_id=account_id,
            )
            return False

        try:
            account = await SchedulerFactory._load_account(
                self.session_factory, account_id
            )
            if account is None:
                logger.warning(
                    "scheduler_runtime.reload_account_inactive",
                    account_id=account_id,
                )
                return False

            broker = self.registry.get(account_id)
            new_ctx = SchedulerFactory._build_account_context(
                account=account,
                broker=broker,
                orchestrator=self.orchestrator,
                recorder=self.recorder,
                web_verifier=self.web_verifier,
                approval_manager=self.approval_manager,
                cost_tracker=self.cost_tracker,
                telegram_bot=self.telegram_bot,
                session_factory=self.session_factory,
                cache=self.cache,
                settings=self.settings,
                execution_stream=self.execution_stream,
                memory_manager=self.memory_manager,
            )

            # 잡 정합: 옛 계좌 잡을 모두 제거 후 새 컨텍스트로 재등록.
            # 잡 id 규칙은 ``{job_type}:{account_id}`` — 공통 잡은 접미사 없음.
            stale = [
                name
                for name in list(self.engine._job_fns)
                if name.endswith(f":{account_id}")
            ]
            for name in stale:
                self.engine.unregister_job(name)

            SchedulerFactory._register_account_jobs(
                self.engine,
                new_ctx,
                orchestrator=self.orchestrator,
                watchlist_symbols=self.watchlist_symbols,
                generator=self.generator,
                telegram_bot=self.telegram_bot,
                settings=self.settings,
                session_factory=self.session_factory,
                decision_queue=self.decision_queue,
                account_index=idx,
                coordinator=self.exit_coordinator,
            )

            # F-05 실시간 손절 청산 의존성도 새 컨텍스트로 갱신(재등록=덮어쓰기).
            self.stoploss_stream.register_account(
                new_ctx.account_id,
                exit_checker=new_ctx.exit_checker,
                exit_service=new_ctx.exit_service,
                position_manager=new_ctx.position_manager,
                account_label=new_ctx.account_label,
            )

            self.contexts[idx] = new_ctx
            logger.info(
                "scheduler_runtime.account_reloaded",
                account_id=account_id,
                strategy_type=new_ctx.strategy_type.value,
            )
            return True
        except Exception:
            logger.exception(
                "scheduler_runtime.reload_account_failed",
                account_id=account_id,
            )
            return False


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
        approval_manager: ApprovalManager,
    ) -> tuple[
        SchedulerEngine,
        BrokerRegistry,
        ExecutionStreamManager,
        StopLossStreamService,
        SchedulerRuntime,
    ]:
        """서비스 그래프 조립 → SchedulerEngine 반환.

        Returns:
            (scheduler_engine, broker_registry, execution_stream, stoploss_stream,
            scheduler_runtime) — main.py는
            - registry 를 shutdown 시 disconnect_all()
            - execution_stream / stoploss_stream 을 startup 시 start() / shutdown 시 stop()
            - scheduler_runtime 으로 백오피스 계좌 편집 시 ``reload_account`` 호출(B-10)
        """
        from src.agent.agents.market_analyst import MarketAnalyst
        from src.agent.agents.risk_manager import RiskManager
        from src.agent.agents.stock_analyst import StockAnalyst
        from src.agent.agents.trader import Trader
        from src.agent.decision_recorder import DecisionRecorder
        from src.agent.tools.context import ToolContext
        from src.agent.tools.registry import ToolRegistry
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
        registered_credentials: list[AccountCredentials] = []

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
                registered_credentials.append(creds)
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

        # 학습 메모리 매니저 — 에이전트(조회)·포지션 매니저(청산 기록)·cleanup 잡이 공유.
        from src.strategy.memory_manager import AgentMemoryManager

        memory_manager = AgentMemoryManager(session_factory)

        orchestrator = PipelineOrchestrator(
            market_analyst=MarketAnalyst(
                llm_router, recorder, tool_registry, memory_manager
            ),
            stock_analyst=StockAnalyst(
                llm_router, recorder, tool_registry, memory_manager
            ),
            risk_manager=RiskManager(
                llm_router, recorder, tool_registry, memory_manager
            ),
            trader=Trader(llm_router, recorder, tool_registry, memory_manager),
            recorder=recorder,
        )

        web_verifier = WebSearchVerifier(
            llm_router=llm_router,
            recorder=recorder,
            settings=settings,
            cache=cache,
        )

        # ApprovalManager는 main.py에서 생성한 전역 싱글톤을 주입받아 사용.
        # 여기서 새 인스턴스를 만들면 TelegramBot 콜백 핸들러가 덮어써져
        # 다른 호출부(예: /buy 커맨드, 백오피스 수동 주문)의 pending 승인이
        # 유실된다.

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

        # ── 4-0. NaverProvider — 뉴스 수집 잡용 (NAVER 키 있을 때만) ──
        naver_provider: NaverProvider | None = None
        if settings.NAVER_CLIENT_ID and settings.NAVER_CLIENT_SECRET:
            from src.data.providers.naver_provider import NaverProvider

            naver_provider = NaverProvider(
                cache=cache,
                session_factory=session_factory,
                settings=settings,
            )
            await naver_provider.initialize()
            logger.info("scheduler_factory.naver_provider_ready")

        # ── 4-1. 서버 시작 시 stock_master 동기화 ─────────────────────
        if provider is not None:
            try:
                count = await provider.sync_stock_master()
                logger.info("stock_master_sync_on_startup", upserted=count)
            except Exception:
                logger.exception("stock_master_sync_on_startup_failed")

        # ── 4-2. FillFinalizer + ExecutionStreamManager + OrderReconciler ─
        from src.execution.decision_queue import TradeDecisionQueueManager
        from src.execution.execution_stream import ExecutionStreamManager
        from src.execution.exit_coordinator import ExitCoordinator
        from src.execution.fill_finalizer import FillFinalizer
        from src.execution.reconciler import OrderReconciler, PositionReconciler
        from src.execution.stoploss_stream import StopLossStreamService
        from src.strategy.position_manager import PositionManager

        # 결정/실행 분리: 개장 전 결정 잡이 적재하고 개장 후 드레인이 소비하는 큐.
        decision_queue = TradeDecisionQueueManager(session_factory)

        # memory_manager는 위 에이전트 구성 시점에 이미 생성됨. 포지션 매니저에도
        # 주입해, 전량 청산 시 학습 메모리(record_trade_outcome)가 자동 기록되게 한다.
        shared_position_manager = PositionManager(
            session_factory, memory_manager=memory_manager
        )
        fill_finalizer = FillFinalizer(
            session_factory=session_factory,
            position_manager=shared_position_manager,
            telegram_bot=telegram_bot,
            settings=settings,
        )
        execution_stream = ExecutionStreamManager(
            settings=settings,
            session_factory=session_factory,
            fill_finalizer=fill_finalizer,
        )
        # F-05: 이중 청산 방지 코디네이터(폴링·WS 공유) + 실시간 손절 서비스.
        exit_coordinator = ExitCoordinator(ttl_sec=settings.EXIT_INFLIGHT_TTL_SEC)
        stoploss_stream = StopLossStreamService(
            settings=settings,
            position_manager=shared_position_manager,
            coordinator=exit_coordinator,
        )
        reconciler = OrderReconciler(
            broker_registry=registry,
            fill_finalizer=fill_finalizer,
        )
        position_reconciler = PositionReconciler(
            broker_registry=registry,
            session_factory=session_factory,
            position_manager=shared_position_manager,
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
                    execution_stream=execution_stream,
                    memory_manager=memory_manager,
                )
                contexts.append(ctx)
                # F-05: 실시간 손절 서비스에 계좌별 청산 의존성 등록.
                stoploss_stream.register_account(
                    ctx.account_id,
                    exit_checker=ctx.exit_checker,
                    exit_service=ctx.exit_service,
                    position_manager=ctx.position_manager,
                    account_label=ctx.account_label,
                )
            except Exception:
                logger.exception(
                    "scheduler_factory.account_context_build_failed",
                    account_id=account.id,
                )

        # ── 6. SchedulerEngine 생성 + 작업 등록 ──────────────────────
        engine = SchedulerEngine(
            session_factory=session_factory,
            settings=settings,
            telegram_bot=telegram_bot,
        )

        watchlist_symbols = await SchedulerFactory._get_watchlist_symbols(
            session_factory,
        )

        # 공통 작업 등록
        SchedulerFactory._register_common_jobs(
            engine,
            provider=provider,
            naver_provider=naver_provider,
            watchlist_symbols=watchlist_symbols,
            generator=generator,
            telegram_bot=telegram_bot,
            settings=settings,
            session_factory=session_factory,
            reconciler=reconciler,
            position_reconciler=position_reconciler,
            memory_manager=memory_manager,
        )

        # 계좌별 작업 등록 (account_index로 배치 시차 실행)
        for account_index, ctx in enumerate(contexts):
            SchedulerFactory._register_account_jobs(
                engine,
                ctx,
                orchestrator=orchestrator,
                watchlist_symbols=watchlist_symbols,
                generator=generator,
                telegram_bot=telegram_bot,
                settings=settings,
                session_factory=session_factory,
                decision_queue=decision_queue,
                account_index=account_index,
                coordinator=exit_coordinator,
            )

        # Attach credentials to the WS managers so main.py's lifespan can start
        # subscriptions after the scheduler factory returns.
        execution_stream_credentials: list[AccountCredentials] = registered_credentials
        execution_stream._pending_credentials = execution_stream_credentials  # type: ignore[attr-defined]
        stoploss_stream._pending_credentials = registered_credentials  # type: ignore[attr-defined]

        logger.info(
            "scheduler_factory.created",
            account_count=len(contexts),
            job_count=len(engine._job_fns),
            watchlist_count=len(watchlist_symbols),
        )

        # B-10: 실행 중 계좌 재반영용 재료 묶음.
        runtime = SchedulerRuntime(
            engine=engine,
            registry=registry,
            stoploss_stream=stoploss_stream,
            session_factory=session_factory,
            cache=cache,
            settings=settings,
            telegram_bot=telegram_bot,
            approval_manager=approval_manager,
            orchestrator=orchestrator,
            recorder=recorder,
            web_verifier=web_verifier,
            cost_tracker=cost_tracker,
            memory_manager=memory_manager,
            generator=generator,
            execution_stream=execution_stream,
            decision_queue=decision_queue,
            exit_coordinator=exit_coordinator,
            watchlist_symbols=watchlist_symbols,
            contexts=contexts,
            naver_provider=naver_provider,
        )

        return engine, registry, execution_stream, stoploss_stream, runtime

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
    async def _load_account(
        session_factory: async_sessionmaker[AsyncSession],
        account_id: str,
    ) -> Account | None:
        """단건 활성 계좌 로드 (reload_account용). 비활성/부재/에러 시 None."""
        try:
            async with session_factory() as session:
                result = await session.execute(
                    select(Account).where(
                        Account.id == account_id,
                        Account.is_active.is_(True),
                    ),
                )
                return result.scalar_one_or_none()
        except Exception:
            logger.warning(
                "scheduler_factory.load_account_failed",
                account_id=account_id,
                exc_info=True,
            )
            return None

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
            risk_tolerance="moderate",
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
        execution_stream: ExecutionStreamManager | None = None,
        memory_manager: AgentMemoryManager | None = None,
    ) -> AccountContext:
        """계좌별 서비스 인스턴스를 조립하여 AccountContext를 반환한다."""
        from src.execution.executor import OrderExecutor
        from src.execution.exit_executor import ExitExecutionService
        from src.scheduler.monitor import TradingMonitor
        from src.strategy.batch_allocator import BatchBudgetAllocator
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

        # 계좌별 서비스 인스턴스 (인라인 청산 경로의 close도 학습 메모리를 기록하도록
        # memory_manager 주입 — 비동기 reconcile 경로의 shared_position_manager와 동일)
        position_manager = PositionManager(
            session_factory, memory_manager=memory_manager
        )
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
            execution_stream=execution_stream,
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

        allocator = BatchBudgetAllocator(
            settings=acct_settings,
            portfolio_service=portfolio_service,
            recorder=recorder,  # type: ignore[arg-type]
        )

        # KISAuth — KISClient일 때만
        auth: KISAuth | None = None
        if not settings.USE_MOCK_BROKER and hasattr(broker, "_auth"):
            auth = broker._auth  # type: ignore[attr-defined]

        # scan_universe() 용 Strategy 인스턴스
        from src.strategy.registry import StrategyCommonDeps, StrategyFactory

        strategy: Strategy | None = None
        try:
            deps = StrategyCommonDeps(
                orchestrator=orchestrator,
                recorder=recorder,
                broker=broker,
                session_factory=session_factory,
                settings=acct_settings,
                cache=cache,
            )
            strategy = StrategyFactory.create(
                strategy_type,
                deps,
                account_id=account_id,
                investment_prompt=account.investment_prompt,
                risk_tolerance=getattr(account, "risk_tolerance", "moderate"),
                risk_overrides=account.risk_overrides,
            )
        except Exception:
            logger.warning(
                "scheduler_factory.strategy_create_failed",
                account_id=account_id,
                strategy_type=strategy_type.value,
                exc_info=True,
            )

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
            risk_tolerance=getattr(account, "risk_tolerance", "moderate"),
            account_label=account_label,
            allocator=allocator,
            strategy=strategy,
        )

    # ── Job Registration ─────────────────────────────────────────────

    @staticmethod
    def _register_common_jobs(
        engine: SchedulerEngine,
        *,
        provider: DataProvider | None,
        naver_provider: NaverProvider | None = None,
        watchlist_symbols: list[str],
        generator: ReportGenerator,
        telegram_bot: TelegramBot,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        reconciler: OrderReconciler | None = None,
        position_reconciler: PositionReconciler | None = None,
        memory_manager: AgentMemoryManager | None = None,
    ) -> None:
        """공통 작업 등록 (계좌 수에 무관하게 1회씩)."""
        s = settings

        # market_data_collect — InMemoryBroker일 때 스킵
        if provider is not None:
            md_h, md_m = SchedulerEngine._parse_time(s.MARKET_DATA_COLLECTION_TIME)
            engine.register_job(
                "market_data_collect",
                partial(job_market_data_collect, provider=provider, symbols=watchlist_symbols),
                CronTrigger(day_of_week="mon-fri", hour=md_h, minute=md_m, timezone="UTC"),
            )

        # news_collect — NAVER 키 있을 때만(NaverProvider 존재). 종목명 검색어로 수집(F-19).
        if naver_provider is not None:
            nc_h, nc_m = SchedulerEngine._parse_time(s.NEWS_COLLECTION_TIME)
            engine.register_job(
                "news_collect",
                partial(
                    job_news_collect,
                    provider=naver_provider,
                    symbols=watchlist_symbols,
                    session_factory=session_factory,
                ),
                CronTrigger(day_of_week="mon-fri", hour=nc_h, minute=nc_m, timezone="UTC"),
            )

        # pre_open_prep — 08:00 KST 개장 전 결측 백필 + 신선도 게이트 (평일, KST)
        po_h, po_m = SchedulerEngine._parse_time(s.PRE_OPEN_PREP_TIME)
        engine.register_job(
            "pre_open_prep",
            partial(
                job_pre_open_prep,
                provider=provider,
                session_factory=session_factory,
                settings=s,
                telegram_bot=telegram_bot,
            ),
            CronTrigger(
                day_of_week="mon-fri", hour=po_h, minute=po_m, timezone="Asia/Seoul"
            ),
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

        # reconcile_open_orders_midday — 12:00 KST 장중 sweep (WS 누락 복구)
        if reconciler is not None:
            rc_days = SchedulerEngine._parse_day_of_week(s.RECONCILE_DAYS)
            md_h, md_m = SchedulerEngine._parse_time(s.RECONCILE_MIDDAY_TIME)
            engine.register_job(
                "reconcile_open_orders_midday",
                partial(job_reconcile_open_orders, reconciler=reconciler, eod=False),
                CronTrigger(
                    day_of_week=rc_days, hour=md_h, minute=md_m,
                    timezone="Asia/Seoul",
                ),
            )

            # reconcile_open_orders_eod — 15:40 KST 장 마감 sweep + expire
            eod_h, eod_m = SchedulerEngine._parse_time(s.RECONCILE_EOD_TIME)
            engine.register_job(
                "reconcile_open_orders_eod",
                partial(job_reconcile_open_orders, reconciler=reconciler, eod=True),
                CronTrigger(
                    day_of_week=rc_days, hour=eod_h, minute=eod_m,
                    timezone="Asia/Seoul",
                ),
            )

        # reconcile_positions — 15:50 KST 브로커-DB 포지션 정합성 검증 (EOD 확정)
        if position_reconciler is not None:
            rc_days = SchedulerEngine._parse_day_of_week(s.RECONCILE_DAYS)
            engine.register_job(
                "reconcile_positions",
                partial(
                    job_reconcile_positions,
                    position_reconciler=position_reconciler,
                ),
                CronTrigger(
                    day_of_week=rc_days, hour=15, minute=50,
                    timezone="Asia/Seoul",
                ),
            )

            # sync_positions_intraday — 9:00~16:00 KST 매시 정각 양방향 동기화
            engine.register_job(
                "sync_positions_intraday",
                partial(
                    job_reconcile_positions,
                    position_reconciler=position_reconciler,
                ),
                CronTrigger(
                    day_of_week=rc_days, hour="9-16", minute=0,
                    timezone="Asia/Seoul",
                ),
            )

        # cleanup_expired_memories — 만료 학습 메모리 비활성화 (매일 1회, 장 무관)
        if memory_manager is not None:
            mc_h, mc_m = SchedulerEngine._parse_time(s.MEMORY_CLEANUP_TIME)
            engine.register_job(
                "cleanup_expired_memories",
                partial(job_cleanup_expired_memories, memory_manager=memory_manager),
                CronTrigger(hour=mc_h, minute=mc_m, timezone="Asia/Seoul"),
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
        session_factory: async_sessionmaker[AsyncSession],
        decision_queue: TradeDecisionQueueManager,
        account_index: int = 0,
        coordinator: ExitCoordinator | None = None,
    ) -> None:
        """계좌별 작업 등록. 작업 이름: ``{job_type}:{account_id}``.

        결정/실행 분리(2026-06-29): 분석=발주 모놀리식을 (개장 전 결정 → 개장 후
        실행 드레인)으로 나눈다. 결정 잡은 DECISION_TIME(08:30 KST)에 결정 큐에
        적재만 하고, execution_drain이 장중 당일가/갭 게이트 통과분만 발주한다.

        Args:
            account_index: 0-based index for staggering cron triggers.
                Each account offsets its batch jobs by ``account_index`` minutes
                to avoid simultaneous KIS API calls across accounts.
            coordinator: 이중 청산 방지 in-flight 가드(stop_loss_check에 주입).
        """
        s = settings
        aid = ctx.account_id

        # token_refresh:{account_id} — mock broker면 스킵. 개장 전(08:00 KST)으로
        # 이동해 세션 중 갱신을 피한다(시차 실행).
        if ctx.auth is not None:
            tr_h, tr_m = SchedulerEngine._parse_time(s.TOKEN_REFRESH_TIME)
            tr_m_offset = (tr_m + account_index) % 60
            tr_h_offset = tr_h + (tr_m + account_index) // 60
            engine.register_job(
                f"token_refresh:{aid}",
                partial(job_token_refresh, auth=ctx.auth, account_id=aid),
                CronTrigger(hour=tr_h_offset, minute=tr_m_offset, timezone="Asia/Seoul"),
            )

        # 결정 잡 — 개장 전 DECISION_TIME(08:30 KST) 결정 큐 적재(발주 안 함). 시차 실행.
        dc_h, dc_m = SchedulerEngine._parse_time(s.DECISION_TIME)
        dc_m_offset = (dc_m + account_index) % 60
        dc_h_offset = dc_h + (dc_m + account_index) // 60

        # swing_decision:{account_id} — swing 전략 계좌만 (평일)
        if ctx.strategy_type == StrategyType.SWING:
            sw_days = SchedulerEngine._parse_day_of_week(s.SWING_ANALYSIS_DAYS)
            engine.register_job(
                f"swing_decision:{aid}",
                partial(
                    job_swing_decision,
                    orchestrator=orchestrator,
                    symbols=watchlist_symbols,
                    queue=decision_queue,
                    session_factory=session_factory,
                    settings=s,
                    strategy=ctx.strategy,
                    account_id=aid,
                    investment_prompt=ctx.investment_prompt,
                    risk_tolerance=ctx.risk_tolerance,
                    account_label=ctx.account_label,
                    market_close=s.MARKET_CLOSE_TIME,
                    holidays=s.KR_HOLIDAYS,
                    telegram_bot=telegram_bot,
                ),
                CronTrigger(
                    day_of_week=sw_days,
                    hour=dc_h_offset,
                    minute=dc_m_offset,
                    timezone="Asia/Seoul",
                ),
            )

        # position_decision:{account_id} — position 전략 계좌만 (평일)
        if ctx.strategy_type == StrategyType.POSITION:
            pa_days = SchedulerEngine._parse_day_of_week(s.POSITION_ANALYSIS_DAYS)
            engine.register_job(
                f"position_decision:{aid}",
                partial(
                    job_position_decision,
                    orchestrator=orchestrator,
                    position_manager=ctx.position_manager,
                    queue=decision_queue,
                    session_factory=session_factory,
                    settings=s,
                    strategy=ctx.strategy,
                    account_id=aid,
                    investment_prompt=ctx.investment_prompt,
                    risk_tolerance=ctx.risk_tolerance,
                    account_label=ctx.account_label,
                    market_close=s.MARKET_CLOSE_TIME,
                    holidays=s.KR_HOLIDAYS,
                    telegram_bot=telegram_bot,
                ),
                CronTrigger(
                    day_of_week=pa_days,
                    hour=dc_h_offset,
                    minute=dc_m_offset,
                    timezone="Asia/Seoul",
                ),
            )

        # execution_drain:{account_id} — 모든 계좌. 장중 주기적으로 결정 큐 소비.
        # 시간 범위는 cron으로 잡고, 정확한 장 운영시간(공휴일 포함) 가드는 잡 내부에서.
        drain_start_h, _ = SchedulerEngine._parse_time(s.EXECUTION_DRAIN_START)
        drain_end_h, _ = SchedulerEngine._parse_time(s.EXECUTION_DRAIN_END)
        engine.register_job(
            f"execution_drain:{aid}",
            partial(
                job_execution_drain,
                queue=decision_queue,
                order_executor=ctx.order_executor,
                broker=ctx.broker,
                strategy_type=ctx.strategy_type.value,
                allocator=ctx.allocator,
                account_id=aid,
                account_label=ctx.account_label,
                market_open=s.MARKET_OPEN_TIME,
                market_close=s.MARKET_CLOSE_TIME,
                holidays=s.KR_HOLIDAYS,
                gap_guard_pct=s.EXECUTION_GAP_GUARD_PCT,
            ),
            CronTrigger(
                day_of_week="mon-fri",
                hour=f"{drain_start_h}-{drain_end_h}",
                minute=f"*/{s.EXECUTION_DRAIN_INTERVAL_MIN}",
                timezone="Asia/Seoul",
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
                market_open=s.MARKET_OPEN_TIME,
                market_close=s.MARKET_CLOSE_TIME,
                holidays=s.KR_HOLIDAYS,
                coordinator=coordinator,
            ),
            IntervalTrigger(minutes=s.STOP_LOSS_CHECK_INTERVAL_MIN),
        )

        # daily_report:{account_id} — 모든 계좌 (시차 실행)
        dr_h, dr_m = SchedulerEngine._parse_time(s.DAILY_REPORT_TIME)
        dr_m_offset = (dr_m + account_index) % 60
        dr_h_offset = dr_h + (dr_m + account_index) // 60
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
            CronTrigger(hour=dr_h_offset, minute=dr_m_offset, timezone="UTC"),
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
                    select(StockMaster.symbol)
                    .where(StockMaster.is_active.is_(True))
                    .order_by(StockMaster.symbol),
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
