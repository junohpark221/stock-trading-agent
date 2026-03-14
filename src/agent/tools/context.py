"""ToolContext — dependency bundle for agent tool functions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import Settings

if TYPE_CHECKING:
    from src.analysis.fundamental.analyzer import FundamentalAnalyzer
    from src.analysis.sentiment.analyzer import KeywordSentimentAnalyzer
    from src.data.providers.dart_provider import DartProvider
    from src.data.providers.ecos_provider import EcosProvider
    from src.data.providers.fred_provider import FredProvider
    from src.data.providers.kis_provider import KISDataProvider
    from src.data.providers.naver_provider import NaverProvider


@dataclass
class ToolContext:
    """Dependency bundle injected into every agent tool function.

    Providers are externally injected and may be ``None`` when unavailable.
    Analyzers are created lazily on first access (they only need session_factory).
    """

    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings

    # External data providers (optional — may not be configured)
    dart_provider: DartProvider | None = None
    naver_provider: NaverProvider | None = None
    ecos_provider: EcosProvider | None = None
    fred_provider: FredProvider | None = None
    kis_provider: KISDataProvider | None = None

    # Private cached analyzers
    _fundamental_analyzer: FundamentalAnalyzer | None = field(
        default=None, init=False, repr=False
    )
    _sentiment_analyzer: KeywordSentimentAnalyzer | None = field(
        default=None, init=False, repr=False
    )

    @property
    def fundamental_analyzer(self) -> FundamentalAnalyzer:
        """Lazily create :class:`FundamentalAnalyzer`."""
        if self._fundamental_analyzer is None:
            from src.analysis.fundamental.analyzer import FundamentalAnalyzer

            self._fundamental_analyzer = FundamentalAnalyzer(self.session_factory)
        return self._fundamental_analyzer

    @property
    def sentiment_analyzer(self) -> KeywordSentimentAnalyzer:
        """Lazily create :class:`KeywordSentimentAnalyzer`."""
        if self._sentiment_analyzer is None:
            from src.analysis.sentiment.analyzer import KeywordSentimentAnalyzer

            self._sentiment_analyzer = KeywordSentimentAnalyzer(self.session_factory)
        return self._sentiment_analyzer
