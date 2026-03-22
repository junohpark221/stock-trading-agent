"""Application configuration via pydantic-settings.

Settings are loaded from environment variables and .env file.
Use ``get_settings()`` for a cached singleton instance.
"""

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, grouped by phase."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # ── Phase 0: Core Infrastructure ──────────────────────────────────
    ENV: Literal["development", "production"] = "development"
    DATABASE_URL: str
    REDIS_URL: str
    LOG_LEVEL: str = "DEBUG"

    # ── Phase 1: KIS Broker ───────────────────────────────────────────
    KIS_APP_KEY: str = ""
    KIS_APP_SECRET: str = ""
    KIS_ACCOUNT_NO: str = ""
    KIS_ACCOUNT_PROD: str = "01"
    KIS_IS_PAPER: bool = True
    KIS_HTS_ID: str = ""
    KIS_BASE_URL: str = ""                 # 빈 값이면 KIS_IS_PAPER로 자동 결정
    KIS_RATE_LIMIT_INTERVAL: float = 0.5   # paper=0.5s, prod=0.05s
    KIS_TOKEN_REDIS_TTL: int = 82800       # 23시간 (토큰 유효 24시간, 1시간 여유)
    KIS_OHLCV_CACHE_TTL: int = 300         # 5분
    KIS_PRICE_CACHE_TTL: int = 10          # 10초

    # ── Phase 2: Market Data ──────────────────────────────────────────
    MARKET_DATA_CACHE_TTL: int = 60
    MARKET_OPEN_TIME: str = "09:00"
    MARKET_CLOSE_TIME: str = "15:30"

    # ── Phase 2: External Data Sources ──────────────────────────────
    DART_API_KEY: str = ""
    ECOS_API_KEY: str = ""
    FRED_API_KEY: str = ""
    NAVER_CLIENT_ID: str = ""
    NAVER_CLIENT_SECRET: str = ""

    # ── Phase 2: Cache TTLs ────────────────────────────────────────
    DART_CACHE_TTL: int = 86400       # 24시간 (재무제표는 자주 안 바뀜)
    ECOS_CACHE_TTL: int = 86400       # 24시간
    FRED_CACHE_TTL: int = 86400       # 24시간
    NEWS_CACHE_TTL: int = 3600        # 1시간

    # ── Phase 3: LLM Providers (GPT-First) ────────────────────────────
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""
    LLM_MONTHLY_BUDGET_USD: Decimal = Decimal("100.00")
    LLM_DEFAULT_PROVIDER: Literal["openai", "anthropic", "google"] = "openai"
    LLM_ESCALATION_CONFIDENCE_THRESHOLD: Decimal = Decimal("0.60")
    LLM_RESPONSE_CACHE_TTL: int = 1800    # 30분
    LLM_CONFIG_CACHE_TTL: int = 300       # 5분
    LLM_BUDGET_WARNING_PCT: int = 80      # 월간 예산 경고 (%)
    LLM_MAX_RETRIES: int = 3              # API 호출 최대 재시도
    LLM_REQUEST_TIMEOUT: int = 120        # API 요청 타임아웃 (초)

    # ── Phase 4: Trading Strategy ─────────────────────────────────────
    MAX_POSITION_SIZE_KRW: int = 1_000_000
    MAX_PORTFOLIO_POSITIONS: int = 5
    STOP_LOSS_PERCENT: float = 3.0
    TAKE_PROFIT_PERCENT: float = 5.0
    DAILY_LOSS_LIMIT_KRW: int = 500_000

    # ── Phase 4: Risk Management + Position Sizing ────────────────────
    RISK_CHECK_ENABLED: bool = True
    RISK_PER_TRADE_PCT: float = 2.0          # 1건당 리스크 비율 (총 자산 대비 %)
    MAX_POSITION_PCT: float = 10.0           # 단일 종목 최대 비중 (%)
    SECTOR_CONCENTRATION_PCT: float = 30.0   # 섹터 최대 집중도 (%)
    MAX_DRAWDOWN_PCT: float = 10.0           # 최대 허용 낙폭 (%)
    DAILY_LOSS_LIMIT_PCT: float = 3.0        # 일일 최대 손실률 (%)
    CORRELATION_THRESHOLD: float = 0.7       # 상관계수 임계치 (0~1)
    MAX_DAILY_TRADES: int = 5                # 일일 최대 거래 횟수

    # ── Phase 5: Notifications ────────────────────────────────────────
    HUMAN_APPROVAL_REQUIRED: bool = True
    HUMAN_APPROVAL_TIMEOUT_SEC: int = 300
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # ── Phase 6: Scheduling ───────────────────────────────────────────
    SCHEDULER_ENABLED: bool = True
    PRE_MARKET_ANALYSIS_TIME: str = "08:30"
    TRADING_SCAN_INTERVAL_MIN: int = 30

    # ── Phase 7: Monitoring ───────────────────────────────────────────
    ALERT_TELEGRAM_ENABLED: bool = True
    ALERT_EMAIL_TO: str = ""

    # ── Phase 8: Production (AWS) ─────────────────────────────────────
    AWS_REGION: str = "ap-northeast-2"
    AWS_ECS_CLUSTER: str = ""
    AWS_SECRET_NAME: str = ""
    SENTRY_DSN: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings singleton."""
    return Settings()  # type: ignore[call-arg]
