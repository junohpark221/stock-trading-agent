"""KIS OAuth2 token management with Redis caching.

Handles:
- Token issuance via ``POST /oauth2/tokenP``
- Redis-based caching (TTL = 23 hours, token valid for 24 hours)
- KIS API header construction with TR ID auto-conversion (paper trading)

Usage::

    auth = KISAuth(settings=settings, cache=cache, session=session)
    token = await auth.get_token()
    headers = auth.build_headers(token, tr_id="TTTC0012U")

Multi-account usage (Phase 8)::

    auth = KISAuth(
        cache=cache, session=session,
        account_id="acct-1", app_key="KEY", app_secret="SECRET",
    )
"""

from __future__ import annotations

import aiohttp
import structlog

from src.config import Settings
from src.core.exceptions import AuthError, ConfigurationError
from src.data.cache import RedisCache

logger = structlog.get_logger(__name__)

_KIS_PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"
_KIS_PROD_BASE_URL = "https://openapi.koreainvestment.com:9443"

_CACHE_NAMESPACE = "kis"


class KISAuth:
    """KIS OAuth2 token manager with Redis caching.

    Supports two creation paths:

    1. **Legacy (Settings-based)**: ``KISAuth(settings=s, cache=c, session=ss)``
    2. **Direct credentials**: ``KISAuth(cache=c, session=ss, account_id="x", app_key="K", app_secret="S")``

    Args:
        settings: Application settings. If provided, credentials are extracted from it.
        cache: RedisCache instance for token storage.
        session: aiohttp session for HTTP requests (shared with KISClient).
        account_id: Account identifier for cache key namespacing (default "default").
        app_key: KIS API app key (used when settings is None).
        app_secret: KIS API app secret (used when settings is None).
        is_paper: Paper trading flag (used when settings is None).
        base_url: KIS API base URL override (used when settings is None).
        token_ttl: Token cache TTL in seconds (used when settings is None).

    Raises:
        ConfigurationError: If ``app_key`` or ``app_secret`` is empty.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        cache: RedisCache,
        session: aiohttp.ClientSession,
        account_id: str = "default",
        app_key: str = "",
        app_secret: str = "",
        is_paper: bool = True,
        base_url: str = "",
        token_ttl: int = 82800,
    ) -> None:
        if settings is not None:
            effective_app_key = settings.KIS_APP_KEY
            effective_app_secret = settings.KIS_APP_SECRET
            effective_is_paper = settings.KIS_IS_PAPER
            effective_token_ttl = settings.KIS_TOKEN_REDIS_TTL
            raw_base_url = settings.KIS_BASE_URL
        else:
            effective_app_key = app_key
            effective_app_secret = app_secret
            effective_is_paper = is_paper
            effective_token_ttl = token_ttl
            raw_base_url = base_url

        if not effective_app_key:
            raise ConfigurationError("KIS_APP_KEY is required")
        if not effective_app_secret:
            raise ConfigurationError("KIS_APP_SECRET is required")

        self._account_id = account_id
        self._cache = cache
        self._session = session

        self._app_key = effective_app_key
        self._app_secret = effective_app_secret
        self._is_paper = effective_is_paper
        self._token_ttl = effective_token_ttl

        # Base URL: explicit setting takes priority, else derive from is_paper
        if raw_base_url:
            self._base_url = raw_base_url.rstrip("/")
        else:
            self._base_url = (
                _KIS_PAPER_BASE_URL if self._is_paper else _KIS_PROD_BASE_URL
            )

    # ── Properties ─────────────────────────────────────────────────────

    @property
    def _cache_key_token(self) -> str:
        """Per-account token cache key: ``{account_id}:token``."""
        return f"{self._account_id}:token"

    # ── Public API ────────────────────────────────────────────────────

    async def get_token(self) -> str:
        """Return a valid access token, using cache when available."""
        cached = await self._cache.get(_CACHE_NAMESPACE, self._cache_key_token)
        if cached is not None:
            logger.debug("kis_token_cache_hit", account_id=self._account_id)
            return cached
        logger.info("kis_token_cache_miss", account_id=self._account_id)
        return await self._issue_token()

    async def refresh_token(self) -> str:
        """Invalidate the cached token and issue a new one."""
        await self._cache.delete(_CACHE_NAMESPACE, self._cache_key_token)
        logger.info("kis_token_refresh", account_id=self._account_id)
        return await self._issue_token()

    def build_headers(
        self, token: str, tr_id: str, *, tr_cont: str = ""
    ) -> dict[str, str]:
        """Build KIS API request headers.

        TR ID auto-conversion for paper trading:
        - First character ``T``, ``J``, or ``C`` → replaced with ``V``
        - ``F``-prefixed TR IDs (조회) → no conversion (same for paper/prod)
        - Production mode → no conversion
        """
        converted_tr_id = self._convert_tr_id(tr_id)
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "text/plain",
            "charset": "utf-8",
            "User-Agent": "stock-trading-agent",
            "authorization": f"Bearer {token}",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
            "tr_id": converted_tr_id,
            "custtype": "P",
            "tr_cont": tr_cont,
        }

    # ── Private ───────────────────────────────────────────────────────

    async def _issue_token(self) -> str:
        """Request a new OAuth token from KIS and cache it."""
        url = f"{self._base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }

        try:
            async with self._session.post(url, json=body) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise AuthError(
                        f"KIS token request failed (HTTP {resp.status}): {text}"
                    )
                data = await resp.json()
        except aiohttp.ClientError as exc:
            raise AuthError(f"KIS token request network error: {exc}") from exc

        access_token = data.get("access_token")
        if not access_token:
            raise AuthError(f"KIS token response missing access_token: {data}")

        await self._cache.set(
            _CACHE_NAMESPACE, self._cache_key_token, access_token, ttl=self._token_ttl
        )
        logger.info("kis_token_issued", ttl=self._token_ttl)
        return access_token

    def _convert_tr_id(self, tr_id: str) -> str:
        """Convert TR ID for paper trading (T/J/C → V)."""
        if not self._is_paper or not tr_id:
            return tr_id
        first = tr_id[0]
        if first in ("T", "J", "C"):
            return "V" + tr_id[1:]
        return tr_id
