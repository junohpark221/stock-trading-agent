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

    # ── Phase 2: Market Data ──────────────────────────────────────────
    MARKET_DATA_CACHE_TTL: int = 60
    MARKET_OPEN_TIME: str = "09:00"
    MARKET_CLOSE_TIME: str = "15:30"

    # ── Phase 3: LLM Providers (GPT-First) ────────────────────────────
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""
    LLM_MONTHLY_BUDGET_USD: Decimal = Decimal("100.00")
    LLM_DEFAULT_PROVIDER: Literal["openai", "anthropic", "google"] = "openai"

    # ── Phase 4: Trading Strategy ─────────────────────────────────────
    MAX_POSITION_SIZE_KRW: int = 1_000_000
    MAX_PORTFOLIO_POSITIONS: int = 5
    STOP_LOSS_PERCENT: float = 3.0
    TAKE_PROFIT_PERCENT: float = 5.0
    DAILY_LOSS_LIMIT_KRW: int = 500_000

    # ── Phase 5: Risk Management + Notifications ──────────────────────
    RISK_CHECK_ENABLED: bool = True
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
