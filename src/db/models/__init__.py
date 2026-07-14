"""ORM model package.

All model modules must be imported here so that Alembic autogenerate
can detect them via ``Base.metadata``.
"""

from src.db.models.account import Account, AccountCrypto
from src.db.models.analysis import Disclosure, EconomicIndicator, FinancialStatement, NewsArticle
from src.db.models.backtest import BacktestRun, BacktestTrade
from src.db.models.execution import (
    ApprovalRequestDB,
    Execution,
    Order,
    TradeDecisionQueue,
)
from src.db.models.investor_flow import (
    InvestorFlowDaily,
    MarketInvestorFlowDaily,
    ShortInterestDaily,
)
from src.db.models.llm import AgentModelConfigDB, DecisionLog, LLMUsage
from src.db.models.market_data import DailyOHLCV, StockMaster
from src.db.models.scheduler import JobExecution
from src.db.models.strategy import AgentMemory, PortfolioSnapshot, PositionRecord

__all__ = [
    "Account",
    "AccountCrypto",
    "AgentMemory",
    "AgentModelConfigDB",
    "ApprovalRequestDB",
    "BacktestRun",
    "BacktestTrade",
    "DailyOHLCV",
    "DecisionLog",
    "Disclosure",
    "EconomicIndicator",
    "Execution",
    "FinancialStatement",
    "InvestorFlowDaily",
    "JobExecution",
    "LLMUsage",
    "MarketInvestorFlowDaily",
    "NewsArticle",
    "Order",
    "PortfolioSnapshot",
    "PositionRecord",
    "ShortInterestDaily",
    "StockMaster",
    "TradeDecisionQueue",
]
