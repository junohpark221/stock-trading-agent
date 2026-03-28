"""Strategy Registry — 전략 등록/조회/팩토리.

@register_strategy 데코레이터로 StrategyType → 클래스 매핑을 등록하고,
StrategyFactory.create()로 계좌별 전략 인스턴스를 생성한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.core.enums import StrategyType

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.agent.decision_recorder import DecisionRecorder
    from src.agent.orchestrator import PipelineOrchestrator
    from src.broker.base import BrokerInterface
    from src.config import Settings
    from src.data.cache import RedisCache
    from src.strategy.base import Strategy

# ── Registry ────────────────────────────────────────────────────────────

_STRATEGY_REGISTRY: dict[StrategyType, type[Strategy]] = {}


def register_strategy(strategy_type: StrategyType):
    """클래스 데코레이터: 전략을 레지스트리에 등록한다.

    Example::

        @register_strategy(StrategyType.POSITION)
        class PositionTradingStrategy(Strategy):
            ...
    """

    def decorator(cls: type[Strategy]) -> type[Strategy]:
        if strategy_type in _STRATEGY_REGISTRY:
            raise ValueError(
                f"Strategy type {strategy_type!r} already registered "
                f"by {_STRATEGY_REGISTRY[strategy_type].__name__}"
            )
        _STRATEGY_REGISTRY[strategy_type] = cls
        return cls

    return decorator


def get_strategy_class(strategy_type: StrategyType) -> type[Strategy]:
    """등록된 전략 클래스를 조회한다. 미등록 시 KeyError."""
    try:
        return _STRATEGY_REGISTRY[strategy_type]
    except KeyError:
        available = ", ".join(sorted(str(k) for k in _STRATEGY_REGISTRY))
        raise KeyError(
            f"No strategy registered for {strategy_type!r}. "
            f"Available: [{available}]"
        ) from None


# ── StrategyCommonDeps ──────────────────────────────────────────────────


@dataclass(frozen=True)
class StrategyCommonDeps:
    """계좌 간 공유되는 의존성 번들.

    Orchestrator, Recorder 등 계좌와 무관하게 공유 가능한 객체를 묶는다.
    계좌별 서비스(PortfolioStateService, AlgoRiskManager 등)는
    StrategyFactory.create() 내에서 생성된다.
    """

    orchestrator: PipelineOrchestrator
    recorder: DecisionRecorder
    broker: BrokerInterface
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    cache: RedisCache


# ── Risk override whitelist ─────────────────────────────────────────────

_ALLOWED_RISK_KEYS: frozenset[str] = frozenset(
    {
        "RISK_PER_TRADE_PCT",
        "MAX_POSITION_PCT",
        "SECTOR_CONCENTRATION_PCT",
        "MAX_DRAWDOWN_PCT",
        "DAILY_LOSS_LIMIT_PCT",
        "DAILY_LOSS_LIMIT_KRW",
        "CORRELATION_THRESHOLD",
        "MAX_DAILY_TRADES",
        "MAX_PORTFOLIO_POSITIONS",
        "MAX_POSITION_SIZE_KRW",
    }
)


def _apply_risk_overrides(
    base_settings: Settings, overrides: dict[str, object]
) -> Settings:
    """허용된 리스크 필드만 override하여 Settings 사본을 반환한다.

    ``model_copy(update=...)`` (Pydantic v2)로 불변 사본을 생성한다.
    """
    bad_keys = set(overrides) - _ALLOWED_RISK_KEYS
    if bad_keys:
        raise ValueError(f"Invalid risk override keys: {sorted(bad_keys)}")
    return base_settings.model_copy(update=overrides)


# ── StrategyFactory ─────────────────────────────────────────────────────


class StrategyFactory:
    """등록된 전략 클래스를 조회하고, 계좌별 의존성을 조립하여 인스턴스를 생성한다."""

    @staticmethod
    def create(
        strategy_type: StrategyType,
        deps: StrategyCommonDeps,
        *,
        account_id: str = "default",
        investment_prompt: str = "",
        risk_overrides: dict[str, object] | None = None,
    ) -> Strategy:
        """계좌별 전략 인스턴스를 생성한다.

        Parameters
        ----------
        strategy_type:
            등록된 전략 유형 (POSITION, SWING 등).
        deps:
            공유 의존성 번들.
        account_id:
            계좌 식별자. 기본 ``"default"``.
        investment_prompt:
            계좌별 투자 철학 프롬프트 (LLM system message에 주입).
        risk_overrides:
            계좌별 리스크 설정 오버라이드 (허용 키만 적용).
        """
        from src.strategy.portfolio_state import PortfolioStateService
        from src.strategy.position_manager import PositionManager
        from src.strategy.risk_manager import AlgoRiskManager

        settings = deps.settings
        if risk_overrides:
            settings = _apply_risk_overrides(deps.settings, risk_overrides)

        portfolio_service = PortfolioStateService(
            broker=deps.broker,
            session_factory=deps.session_factory,
            cache=deps.cache,
            account_id=account_id,
        )
        risk_manager = AlgoRiskManager(
            portfolio_service=portfolio_service,
            session_factory=deps.session_factory,
            settings=settings,
        )
        position_manager = PositionManager(deps.session_factory)

        cls = get_strategy_class(strategy_type)
        return cls(
            orchestrator=deps.orchestrator,
            risk_manager=risk_manager,
            portfolio_service=portfolio_service,
            broker=deps.broker,
            recorder=deps.recorder,
            position_manager=position_manager,
            session_factory=deps.session_factory,
            settings=settings,
            account_id=account_id,
            investment_prompt=investment_prompt,
        )
