"""Custom exception hierarchy for the stock trading agent.

Exception tree:
    TradingAgentError (base)
    ├── ConfigurationError
    ├── DatabaseError
    ├── CacheError
    ├── DataProviderError
    │   └── ExternalAPIError
    ├── BrokerError
    │   ├── AuthError
    │   │   └── TokenExpiredError
    │   ├── APIError
    │   │   ├── RateLimitError
    │   │   └── KISResponseError
    │   ├── OrderError
    │   └── InsufficientFundsError
    ├── RiskLimitError
    ├── LLMError
    │   ├── ProviderError
    │   └── BudgetExceededError
    └── ApprovalError
        └── ApprovalTimeoutError
"""


class TradingAgentError(Exception):
    """Base exception for all trading agent errors."""


# --- Configuration ---


class ConfigurationError(TradingAgentError):
    """Invalid or missing configuration."""


# --- Database ---


class DatabaseError(TradingAgentError):
    """Database connection or query error."""


# --- Cache ---


class CacheError(TradingAgentError):
    """Redis 캐시 연산 실패 (연결, 직렬화, 타임아웃)."""


# --- Data Provider ---


class DataProviderError(TradingAgentError):
    """외부 데이터 소스 공통 에러 (DART, ECOS, FRED, Naver)."""


class ExternalAPIError(DataProviderError):
    """외부 API 호출 실패 (네트워크, 인증, 응답 오류)."""


# --- Broker ---


class BrokerError(TradingAgentError):
    """Base exception for broker-related errors."""


class AuthError(BrokerError):
    """Broker authentication failure (token expired, invalid credentials)."""


class TokenExpiredError(AuthError):
    """KIS OAuth token expired (msg_cd: EGW00123, EGW00121)."""


class APIError(BrokerError):
    """Broker API call failure (network, rate limit, unexpected response)."""


class RateLimitError(APIError):
    """KIS API rate limit exceeded (msg_cd: EGW00201)."""


class KISResponseError(APIError):
    """KIS API 응답 오류 (rt_cd != "0")."""

    def __init__(self, msg_cd: str, msg1: str, tr_id: str = ""):
        self.msg_cd = msg_cd
        self.msg1 = msg1
        self.tr_id = tr_id
        super().__init__(f"KIS API error [{msg_cd}]: {msg1} (tr_id={tr_id})")


class OrderError(BrokerError):
    """Order submission or execution failure."""


class InsufficientFundsError(BrokerError):
    """Account balance insufficient for the requested order."""


# --- Risk ---


class RiskLimitError(TradingAgentError):
    """Risk limit exceeded (position size, daily loss, portfolio concentration)."""


# --- LLM ---


class LLMError(TradingAgentError):
    """Base exception for LLM-related errors."""


class ProviderError(LLMError):
    """LLM provider API failure (timeout, rate limit, model unavailable)."""


class BudgetExceededError(LLMError):
    """Monthly LLM budget exceeded."""


# --- Approval ---


class ApprovalError(TradingAgentError):
    """Trade approval process failure."""


class ApprovalTimeoutError(ApprovalError):
    """Human approval not received within the configured timeout."""
