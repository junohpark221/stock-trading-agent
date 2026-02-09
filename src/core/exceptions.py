"""Custom exception hierarchy for the stock trading agent.

Exception tree:
    TradingAgentError (base)
    ├── ConfigurationError
    ├── DatabaseError
    ├── BrokerError
    │   ├── AuthError
    │   ├── APIError
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


# --- Broker ---


class BrokerError(TradingAgentError):
    """Base exception for broker-related errors."""


class AuthError(BrokerError):
    """Broker authentication failure (token expired, invalid credentials)."""


class APIError(BrokerError):
    """Broker API call failure (network, rate limit, unexpected response)."""


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
