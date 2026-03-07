"""Data provider abstraction layer."""

from src.data.providers.ecos_provider import (
    DEFAULT_ECOS_INDICATORS,
    EcosIndicatorConfig,
    EcosProvider,
)
from src.data.providers.fred_provider import DEFAULT_FRED_SERIES, FredProvider
from src.data.providers.naver_provider import NaverProvider

__all__ = [
    "DEFAULT_ECOS_INDICATORS",
    "DEFAULT_FRED_SERIES",
    "EcosIndicatorConfig",
    "EcosProvider",
    "FredProvider",
    "NaverProvider",
]
