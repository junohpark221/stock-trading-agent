"""Scheduler dependency factory — assembles all service instances for jobs.

orders.py의 _build_executor() 패턴을 참조하되,
한 번 조립된 인스턴스를 SchedulerEngine의 전체 생명주기 동안 재사용.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

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

    from src.broker.base import BrokerInterface
    from src.broker.kis.auth import KISAuth
    from src.config import Settings
    from src.data.cache import RedisCache
    from src.data.providers.base import DataProvider
    from src.notification.telegram import TelegramBot

logger = structlog.get_logger(__name__)


async def _noop() -> None:
    """No-op async function — InMemoryBroker에서 token refresh 대체."""
    logger.debug("scheduler.noop_job")


class SchedulerFactory:
    """스케줄러 의존성 팩토리.

    전체 서비스 그래프를 한 번 조립하고 SchedulerEngine 생명주기 동안 재사용.
    """

    @staticmethod
    async def create_scheduler(
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        cache: RedisCache,
        telegram_bot: TelegramBot,
    ) -> tuple[SchedulerEngine, BrokerInterface]:
        """서비스 그래프 조립 → SchedulerEngine 반환.

        Returns:
            (scheduler_engine, broker) — broker는 main.py shutdown에서 disconnect 필요.
        """
        from src.agent.agents.market_analyst import MarketAnalyst
        from src.agent.agents.risk_manager import RiskManager
        from src.agent.agents.stock_analyst import StockAnalyst
        from src.agent.agents.trader import Trader
        from src.agent.decision_recorder import DecisionRecorder
        from src.agent.tools.context import ToolContext
        from src.agent.tools.registry import ToolRegistry
        from src.execution.approval import ApprovalManager
        from src.execution.executor import OrderExecutor
        from src.execution.exit_executor import ExitExecutionService
        from src.execution.web_verify import WebSearchVerifier
        from src.llm.cost_tracker import CostTracker
        from src.llm.router import LLMRouter
        from src.report.data_fetcher import ReportDataFetcher
        from src.report.generator import ReportGenerator
        from src.scheduler.monitor import TradingMonitor
        from src.strategy.exit_checker import ExitConditionChecker
        from src.strategy.portfolio_state import PortfolioStateService
        from src.strategy.position_manager import PositionManager
        from src.strategy.risk_manager import AlgoRiskManager

        # 1. Broker — USE_MOCK_BROKER 분기
        if settings.USE_MOCK_BROKER:
            from src.broker.mock.client import InMemoryBroker

            broker: BrokerInterface = InMemoryBroker()
        else:
            from src.broker.kis.client import KISClient

            broker = KISClient(settings=settings, cache=cache)

        # 2. Broker connect (KISClient: aiohttp session + KISAuth 생성)
        await broker.connect()

        # 3. KISAuth — KISClient일 때만 (InMemoryBroker는 토큰 불필요)
        auth: KISAuth | None = None
        if not settings.USE_MOCK_BROKER:
            auth = broker._auth  # type: ignore[attr-defined]  # KISClient internal

        # 4. DataProvider — KISClient일 때만 (InMemoryBroker는 시세 수집 불가)
        provider: DataProvider | None = None
        if not settings.USE_MOCK_BROKER:
            from src.data.providers.kis_provider import KISDataProvider

            provider = KISDataProvider(
                client=broker,  # type: ignore[arg-type]
                cache=cache,
                session_factory=session_factory,
                settings=settings,
            )

        # 5~6. LLM 인프라
        cost_tracker = CostTracker(session_factory, settings)
        llm_router = LLMRouter(
            session_factory=session_factory,
            settings=settings,
            cost_tracker=cost_tracker,
            cache=cache,
        )

        # 7~10. Agent pipeline
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

        # 11~13. Strategy components
        position_manager = PositionManager(session_factory)
        portfolio_service = PortfolioStateService(
            broker=broker, session_factory=session_factory, cache=cache,
        )
        exit_checker = ExitConditionChecker(
            max_drawdown_pct=settings.MAX_DRAWDOWN_PCT,
        )

        # 14~16. Execution components
        web_verifier = WebSearchVerifier(
            llm_router=llm_router, recorder=recorder, settings=settings,
        )
        approval_manager = ApprovalManager(
            telegram_bot=telegram_bot,
            recorder=recorder,
            session_factory=session_factory,
            cache=cache,
            settings=settings,
        )
        await approval_manager.initialize()

        algo_risk_manager = AlgoRiskManager(
            portfolio_service=portfolio_service,
            session_factory=session_factory,
            settings=settings,
        )

        # 17~18. OrderExecutor + ExitExecutionService
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
            settings=settings,
        )
        exit_service = ExitExecutionService(
            order_executor=order_executor,
            position_manager=position_manager,
            portfolio_service=portfolio_service,
            recorder=recorder,
            telegram_bot=telegram_bot,
        )

        # 19~21. Report + Monitor
        data_fetcher = ReportDataFetcher(session_factory)
        generator = ReportGenerator(
            data_fetcher=data_fetcher,
            cost_tracker=cost_tracker,
            settings=settings,
        )
        monitor = TradingMonitor(
            portfolio_state_service=portfolio_service,
            position_manager=position_manager,
            cost_tracker=cost_tracker,
            telegram_bot=telegram_bot,
            broker=broker,
            cache=cache,
            settings=settings,
        )

        # 22. SchedulerEngine
        engine = SchedulerEngine(
            session_factory=session_factory, settings=settings,
        )

        # 23~25. Watchlist → closures → register
        watchlist_symbols = await SchedulerFactory._get_watchlist_symbols(
            session_factory,
        )
        closures = SchedulerFactory._build_job_closures(
            auth=auth,
            provider=provider,
            orchestrator=orchestrator,
            position_manager=position_manager,
            portfolio_service=portfolio_service,
            exit_checker=exit_checker,
            exit_service=exit_service,
            broker=broker,
            monitor=monitor,
            generator=generator,
            telegram_bot=telegram_bot,
            watchlist_symbols=watchlist_symbols,
        )
        engine.register_jobs(**closures)

        logger.info(
            "scheduler_factory.created",
            broker_type=type(broker).__name__,
            watchlist_count=len(watchlist_symbols),
        )

        return engine, broker

    @staticmethod
    def _build_job_closures(
        *,
        auth: KISAuth | None,
        provider: DataProvider | None,
        orchestrator: object,
        position_manager: object,
        portfolio_service: object,
        exit_checker: object,
        exit_service: object,
        broker: BrokerInterface,
        monitor: object,
        generator: object,
        telegram_bot: TelegramBot,
        watchlist_symbols: list[str],
    ) -> dict[str, Callable[[], Awaitable[None]]]:
        """각 job 함수를 의존성과 바인딩한 클로저 딕셔너리 반환.

        functools.partial로 keyword 인자를 고정하여
        SchedulerEngine.register_jobs()에 인자 없는 callable을 전달.
        """
        # token_refresh — InMemoryBroker일 때 no-op
        token_refresh_fn = (
            partial(job_token_refresh, auth=auth) if auth is not None else _noop
        )

        # market_data_collect — InMemoryBroker일 때 no-op
        market_data_collect_fn = (
            partial(job_market_data_collect, provider=provider, symbols=watchlist_symbols)
            if provider is not None
            else _noop
        )

        return {
            "token_refresh_fn": token_refresh_fn,
            "market_data_collect_fn": market_data_collect_fn,
            "swing_analysis_fn": partial(
                job_swing_analysis,
                orchestrator=orchestrator,
                symbols=watchlist_symbols,
            ),
            "position_analysis_fn": partial(
                job_position_analysis,
                orchestrator=orchestrator,
                position_manager=position_manager,
            ),
            "stop_loss_check_fn": partial(
                job_stop_loss_check,
                exit_checker=exit_checker,
                exit_service=exit_service,
                position_manager=position_manager,
                portfolio_service=portfolio_service,
                broker=broker,
                monitor=monitor,
            ),
            "daily_report_fn": partial(
                job_daily_report,
                generator=generator,
                telegram_bot=telegram_bot,
                portfolio_service=portfolio_service,
            ),
            "weekly_report_fn": partial(
                job_weekly_report,
                generator=generator,
                telegram_bot=telegram_bot,
            ),
            "monthly_report_fn": partial(
                job_monthly_report,
                generator=generator,
                telegram_bot=telegram_bot,
            ),
            "llm_cost_report_fn": partial(
                job_llm_cost_report,
                generator=generator,
                telegram_bot=telegram_bot,
            ),
        }

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
