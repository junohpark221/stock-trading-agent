"""KIS REST API client implementing BrokerInterface.

Handles:
- Rate-limited HTTP requests with semaphore + sleep
- Automatic token refresh on expiry (1 retry)
- Paginated balance/OHLCV queries
- .mst.zip stock master parsing (KOSPI/KOSDAQ)

Usage::

    async with KISClient(settings=settings, cache=cache) as client:
        price = await client.get_price("005930")
        balance = await client.get_balance()
"""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import aiohttp
import structlog

from src.broker.base import BrokerInterface
from src.broker.credentials import AccountCredentials
from src.broker.kis.auth import KISAuth
from src.broker.kis.models import (
    KISBalanceOutput1,
    KISBalanceOutput2,
    KISBaseResponse,
    KISDailyChartOutput,
    KISOrderOutput,
    KISPriceOutput,
    _to_decimal,
    _to_int,
)
from src.config import Settings
from src.core.enums import (
    MarketType,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.core.exceptions import (
    APIError,
    BrokerError,
    InsufficientFundsError,
    KISResponseError,
    RateLimitError,
    TokenExpiredError,
)
from src.core.models import (
    OHLCV,
    AccountBalance,
    OrderRequest,
    OrderResult,
    Position,
    PriceInfo,
    StockInfo,
)
from src.data.cache import RedisCache

logger = structlog.get_logger(__name__)

_KIS_PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"
_KIS_PROD_BASE_URL = "https://openapi.koreainvestment.com:9443"

# .mst download URLs (no auth required)
_MST_KOSPI_URL = "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip"
_MST_KOSDAQ_URL = "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip"

_CODE_PATTERN = re.compile(r"^\d{6}$")

# Pagination safety limits
_MAX_OHLCV_PAGES = 20
_MAX_BALANCE_PAGES = 10


class KISClient(BrokerInterface):
    """KIS REST API client with rate limiting and automatic retry.

    Args:
        settings: Application settings (KIS credentials, rate limit config).
        cache: RedisCache instance for token caching.
    """

    def __init__(self, *, settings: Settings, cache: RedisCache) -> None:
        self._settings: Settings | None = settings
        self._cache = cache
        self._session: aiohttp.ClientSession | None = None
        self._auth: KISAuth | None = None
        self._credentials: AccountCredentials | None = None
        self._token_ttl: int = settings.KIS_TOKEN_REDIS_TTL

        # Rate limiting
        self._semaphore = asyncio.Semaphore(1)
        self._rate_limit_interval = settings.KIS_RATE_LIMIT_INTERVAL
        self._max_rate_limit_retries = settings.KIS_RATE_LIMIT_MAX_RETRIES
        self._rate_limit_backoff_base = settings.KIS_RATE_LIMIT_BACKOFF_BASE

        # Base URL
        if settings.KIS_BASE_URL:
            self._base_url = settings.KIS_BASE_URL.rstrip("/")
        else:
            self._base_url = (
                _KIS_PAPER_BASE_URL if settings.KIS_IS_PAPER else _KIS_PROD_BASE_URL
            )

        # Account
        self._cano = settings.KIS_ACCOUNT_NO[:8] if settings.KIS_ACCOUNT_NO else ""
        self._acnt_prdt_cd = settings.KIS_ACCOUNT_PROD

    @classmethod
    def from_credentials(
        cls,
        credentials: AccountCredentials,
        cache: RedisCache,
        *,
        rate_limit_interval: float | None = None,
        token_ttl: int = 82800,
    ) -> KISClient:
        """Create a KISClient from per-account credentials (no Settings needed).

        WARNING: This bypasses __init__. Any new attribute added to __init__
        must also be set here.

        Args:
            credentials: Decrypted account credentials.
            cache: RedisCache instance for token caching.
            rate_limit_interval: Seconds between API calls.
                None → auto-detect (paper=0.5s, prod=0.05s).
            token_ttl: OAuth token cache TTL in seconds.
        """
        instance = cls.__new__(cls)
        instance._settings = None
        instance._cache = cache
        instance._session = None
        instance._auth = None
        instance._credentials = credentials
        instance._token_ttl = token_ttl

        # Rate limiting
        instance._semaphore = asyncio.Semaphore(1)
        if rate_limit_interval is not None:
            instance._rate_limit_interval = rate_limit_interval
        else:
            instance._rate_limit_interval = 0.5 if credentials.is_paper else 0.05
        instance._max_rate_limit_retries = 3
        instance._rate_limit_backoff_base = 1.0

        # Base URL
        instance._base_url = (
            _KIS_PAPER_BASE_URL if credentials.is_paper else _KIS_PROD_BASE_URL
        )

        # Account
        instance._cano = credentials.account_no[:8] if credentials.account_no else ""
        instance._acnt_prdt_cd = credentials.account_prod

        return instance

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Create HTTP session and initialize auth."""
        self._session = aiohttp.ClientSession()

        if self._settings is not None:
            # Legacy path — create KISAuth from Settings
            self._auth = KISAuth(
                settings=self._settings,
                cache=self._cache,
                session=self._session,
            )
        else:
            # from_credentials path — create KISAuth with direct params
            creds = self._credentials
            assert creds is not None  # noqa: S101
            self._auth = KISAuth(
                cache=self._cache,
                session=self._session,
                account_id=creds.account_id,
                app_key=creds.app_key,
                app_secret=creds.app_secret,
                is_paper=creds.is_paper,
                token_ttl=self._token_ttl,
            )

        # Validate credentials by fetching a token
        await self._auth.get_token()
        logger.info("kis_client_connected", base_url=self._base_url)

    async def disconnect(self) -> None:
        """Close HTTP session and release resources."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._auth = None
        logger.info("kis_client_disconnected")

    # ── Internal: HTTP request with rate limit + retry ────────────────

    async def _request(
        self,
        method: str,
        path: str,
        tr_id: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, str] | None = None,
        tr_cont: str = "",
    ) -> dict[str, Any]:
        """Send a rate-limited request to KIS, with 1 token-refresh retry."""
        return await self._do_request(
            method, path, tr_id,
            params=params, body=body, tr_cont=tr_cont, is_retry=False,
        )

    async def _do_request(
        self,
        method: str,
        path: str,
        tr_id: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, str] | None = None,
        tr_cont: str = "",
        is_retry: bool = False,
        rate_limit_retry: int = 0,
    ) -> dict[str, Any]:
        """Execute a single KIS API call inside the rate-limit semaphore."""
        if not self._session or not self._auth:
            raise BrokerError("KISClient not connected. Call connect() first.")

        async with self._semaphore:
            token = await self._auth.get_token()
            headers = self._auth.build_headers(token, tr_id, tr_cont=tr_cont)
            url = f"{self._base_url}{path}"

            try:
                if method.upper() == "GET":
                    resp = await self._session.get(url, headers=headers, params=params)
                else:
                    resp = await self._session.post(url, headers=headers, json=body)

                # Rate limit sleep inside semaphore to guarantee interval
                await asyncio.sleep(self._rate_limit_interval)

            except aiohttp.ClientError as exc:
                raise APIError(f"KIS request failed: {exc}") from exc

        # Parse response outside semaphore
        try:
            data: dict[str, Any] = await resp.json()
        except Exception as exc:
            text = await resp.text()
            raise APIError(f"KIS response JSON parse error: {text[:200]}") from exc

        # Inject tr_cont from response header for pagination
        data["_tr_cont"] = resp.headers.get("tr_cont", "")

        # Validate response
        base = KISBaseResponse.model_validate(data)
        if base.is_ok:
            return data

        # Error handling
        return await self._handle_error(
            base, data, method, path, tr_id,
            params=params, body=body, tr_cont=tr_cont,
            is_retry=is_retry, rate_limit_retry=rate_limit_retry,
        )

    async def _handle_error(
        self,
        base: KISBaseResponse,
        data: dict[str, Any],
        method: str,
        path: str,
        tr_id: str,
        *,
        params: dict[str, str] | None,
        body: dict[str, str] | None,
        tr_cont: str,
        is_retry: bool,
        rate_limit_retry: int = 0,
    ) -> dict[str, Any]:
        """Map KIS error codes to exceptions or retry on token expiry."""
        msg_cd = base.msg_cd
        msg1 = base.msg1

        # Token expired → refresh + 1 retry
        if msg_cd in ("EGW00123", "EGW00121"):
            if is_retry:
                raise TokenExpiredError(f"Token expired after retry: {msg1}")
            logger.warning("kis_token_expired_retrying", msg_cd=msg_cd)
            assert self._auth is not None  # noqa: S101
            await self._auth.refresh_token()
            return await self._do_request(
                method, path, tr_id,
                params=params, body=body, tr_cont=tr_cont,
                is_retry=True, rate_limit_retry=rate_limit_retry,
            )

        # Rate limit — exponential backoff retry
        if msg_cd == "EGW00201":
            if rate_limit_retry >= self._max_rate_limit_retries:
                raise RateLimitError(
                    f"KIS rate limit after {rate_limit_retry} retries: {msg1}"
                )
            wait = self._rate_limit_backoff_base * (2 ** rate_limit_retry)
            logger.warning(
                "kis_rate_limit_retry",
                retry=rate_limit_retry + 1,
                max_retries=self._max_rate_limit_retries,
                wait_seconds=wait,
            )
            await asyncio.sleep(wait)
            return await self._do_request(
                method, path, tr_id,
                params=params, body=body, tr_cont=tr_cont,
                is_retry=is_retry, rate_limit_retry=rate_limit_retry + 1,
            )

        # Insufficient funds
        if msg_cd == "APBK0013":
            raise InsufficientFundsError(f"Insufficient funds: {msg1}")

        # Generic KIS error
        raise KISResponseError(msg_cd=msg_cd, msg1=msg1, tr_id=tr_id)

    # ── Market Data ───────────────────────────────────────────────────

    async def get_price(self, symbol: str) -> PriceInfo:
        """Fetch current price snapshot for *symbol*."""
        data = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
            },
        )
        output = KISPriceOutput.model_validate(data.get("output", {}))
        return output.to_domain(symbol)

    async def get_daily_ohlcv(
        self, symbol: str, *, period_days: int = 100
    ) -> list[OHLCV]:
        """Fetch daily OHLCV bars with date-window pagination.

        KIS returns max 100 bars per request (newest first).
        When exactly 100 bars are returned, we move the end_date window
        back and request the next page.
        """
        today = date.today()
        start_date = today - timedelta(days=period_days)
        end_date = today

        all_bars: list[OHLCV] = []
        seen: set[tuple[str, date]] = set()

        for _ in range(_MAX_OHLCV_PAGES):
            data = await self._request(
                "GET",
                "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                "FHKST03010100",
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": symbol,
                    "FID_INPUT_DATE_1": start_date.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": end_date.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "0",  # 수정주가 반영
                },
            )
            items = data.get("output2", [])
            if not items:
                break

            page_count = 0
            oldest_date: date | None = None
            for raw in items:
                chart = KISDailyChartOutput.model_validate(raw)
                if not chart.stck_bsop_date:
                    continue
                bar = chart.to_domain(symbol)
                key = (symbol, bar.date)
                if key not in seen:
                    seen.add(key)
                    all_bars.append(bar)
                    page_count += 1
                if oldest_date is None or bar.date < oldest_date:
                    oldest_date = bar.date

            # Less than 100 items → no more pages
            if page_count < 100 or oldest_date is None:
                break

            # Move window back: oldest_date - 1 day
            end_date = oldest_date - timedelta(days=1)
            if end_date < start_date:
                break

        # Sort ascending by date
        all_bars.sort(key=lambda b: b.date)
        return all_bars

    async def get_stock_master(self) -> list[StockInfo]:
        """Download and parse KOSPI + KOSDAQ .mst.zip master files."""
        stocks: list[StockInfo] = []
        stocks.extend(await self._parse_mst(_MST_KOSPI_URL, MarketType.KOSPI, 228))
        stocks.extend(await self._parse_mst(_MST_KOSDAQ_URL, MarketType.KOSDAQ, 222))
        logger.info("kis_stock_master_loaded", total=len(stocks))
        return stocks

    async def _parse_mst(
        self, url: str, market_type: MarketType, part2_len: int
    ) -> list[StockInfo]:
        """Download a .mst.zip and parse fixed-width records."""
        if not self._session:
            raise BrokerError("KISClient not connected. Call connect() first.")

        try:
            async with self._session.get(url, ssl=False) as resp:
                if resp.status != 200:
                    logger.error("kis_mst_download_failed", url=url, status=resp.status)
                    return []
                raw_bytes = await resp.read()
        except aiohttp.ClientError as exc:
            logger.error("kis_mst_download_error", url=url, error=str(exc))
            return []

        stocks: list[StockInfo] = []
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                for name in zf.namelist():
                    content = zf.read(name).decode("cp949", errors="replace")
                    for line in content.splitlines():
                        if len(line) <= part2_len:
                            continue
                        part1 = line[:-part2_len]
                        # part1: short_code(9) + standard_code(12) + korean_name(rest)
                        if len(part1) < 21:
                            continue
                        short_code = part1[:9].strip()
                        korean_name = part1[21:].strip()
                        # Only keep 6-digit numeric codes
                        if not _CODE_PATTERN.match(short_code):
                            continue
                        stocks.append(
                            StockInfo(
                                symbol=short_code,
                                name=korean_name,
                                market_type=market_type,
                            )
                        )
        except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
            logger.error("kis_mst_parse_error", url=url, error=str(exc))

        return stocks

    # ── Trading ───────────────────────────────────────────────────────

    async def place_order(self, order: OrderRequest) -> OrderResult:
        """Submit a buy/sell order to KIS.

        - Buy:  TR_ID = TTTC0012U
        - Sell: TR_ID = TTTC0011U
        - ORD_DVSN: "00" = limit, "01" = market
        - Market orders: ORD_UNPR = "0"
        - Sell orders: SLL_TYPE = "01"
        """
        is_buy = order.side == OrderSide.BUY
        tr_id = "TTTC0012U" if is_buy else "TTTC0011U"
        ord_dvsn = "00" if order.order_type == OrderType.LIMIT else "01"
        ord_unpr = "0" if order.order_type == OrderType.MARKET else str(order.price or 0)

        body: dict[str, str] = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "PDNO": order.symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(order.quantity),
            "ORD_UNPR": ord_unpr,
        }
        if not is_buy:
            body["SLL_TYPE"] = "01"

        data = await self._request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id,
            body=body,
        )
        kis_out = KISOrderOutput.model_validate(data.get("output", {}))

        return OrderResult(
            order_id=kis_out.ODNO,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            price=order.price or Decimal(0),
            status=OrderStatus.SUBMITTED,
            timestamp=datetime.now(),
        )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order (not implemented in MVP)."""
        raise NotImplementedError("cancel_order is not supported in MVP scope")

    # ── Account ───────────────────────────────────────────────────────

    async def get_balance(self) -> AccountBalance:
        """Fetch account balance with paginated position data."""
        positions, summary = await self._fetch_balance_pages()

        cash = _to_decimal(summary.dnca_tot_amt)
        invested = _to_decimal(summary.pchs_amt_smtl_amt)
        unrealized = _to_decimal(summary.evlu_pfls_smtl_amt)

        return AccountBalance(
            total_assets=_to_decimal(summary.tot_evlu_amt),
            cash=cash,
            invested=invested,
            unrealized_pnl=unrealized,
            daily_pnl=_to_decimal(summary.thdt_sll_amt) - _to_decimal(summary.thdt_buy_amt),
            positions_count=len([p for p in positions if _to_int(p.hldg_qty) > 0]),
            timestamp=datetime.now(),
        )

    async def get_positions(self) -> list[Position]:
        """Fetch all open positions."""
        raw_positions, _ = await self._fetch_balance_pages()
        positions: list[Position] = []
        for item in raw_positions:
            if _to_int(item.hldg_qty) > 0:
                positions.append(item.to_domain())
        return positions

    async def _fetch_balance_pages(
        self,
    ) -> tuple[list[KISBalanceOutput1], KISBalanceOutput2]:
        """Paginate TTTC8434R to collect all positions + account summary.

        Pagination uses ``tr_cont`` header:
        - "M" or "F" → more pages available
        - "D" or "" → last page

        Context keys (``ctx_area_fk100``, ``ctx_area_nk100``) carry forward
        from the response body to the next request params.
        """
        all_positions: list[KISBalanceOutput1] = []
        summary: KISBalanceOutput2 | None = None
        tr_cont_req = ""
        ctx_fk = ""
        ctx_nk = ""

        for _page in range(_MAX_BALANCE_PAGES):
            params: dict[str, str] = {
                "CANO": self._cano,
                "ACNT_PRDT_CD": self._acnt_prdt_cd,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": ctx_fk,
                "CTX_AREA_NK100": ctx_nk,
            }

            data = await self._request(
                "GET",
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                "TTTC8434R",
                params=params,
                tr_cont=tr_cont_req,
            )

            # Parse positions
            for raw in data.get("output1", []):
                item = KISBalanceOutput1.model_validate(raw)
                all_positions.append(item)

            # Parse account summary (first page only)
            if summary is None:
                output2_list = data.get("output2", [])
                if output2_list:
                    summary = KISBalanceOutput2.model_validate(output2_list[0])

            # Check pagination
            resp_tr_cont = data.get("_tr_cont", "")
            if resp_tr_cont in ("M", "F"):
                tr_cont_req = "N"
                ctx_fk = data.get("ctx_area_fk100", "")
                ctx_nk = data.get("ctx_area_nk100", "")
            else:
                break

        if summary is None:
            summary = KISBalanceOutput2()

        return all_positions, summary
