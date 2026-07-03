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
from typing import Any, ClassVar

import aiohttp
import structlog

from src.broker.base import BrokerInterface
from src.broker.credentials import AccountCredentials
from src.broker.kis.auth import KISAuth
from src.broker.kis.models import (
    KISBalanceOutput1,
    KISBalanceOutput2,
    KISBalanceRlzPlOutput2,
    KISBaseResponse,
    KISDailyChartOutput,
    KISOrderCcldOutput,
    KISOrderOutput,
    KISPriceOutput,
    KISPsblOrderOutput,
    KISPsblSellOutput,
    KISRvseCnclPsblOutput,
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
# 업종코드 마스터 (지수업종 코드 → 업종명 매핑)
_MST_IDXCODE_URL = "https://new.real.download.dws.co.kr/common/master/idxcode.mst.zip"

# 종목 .mst part2 내 지수업종중분류 코드 위치 (그룹코드2 + 시총규모1 + 지수업종대분류4 = 오프셋 7).
# KOSPI(part2=228)·KOSDAQ(part2=222) 모두 선두 필드 배치가 같아 오프셋 동일.
_SECTOR_MID_OFFSET = 7
_SECTOR_MID_LEN = 4

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

    # Global rate limiter shared across all KISClient instances.
    # KIS enforces rate limits per App Key / IP, so per-instance semaphores
    # alone cannot prevent violations when multiple accounts run concurrently.
    _global_semaphore: ClassVar[asyncio.Semaphore] = asyncio.Semaphore(1)
    _global_rate_interval: ClassVar[float] = 0.05

    def __init__(self, *, settings: Settings, cache: RedisCache) -> None:
        self._settings: Settings | None = settings
        self._cache = cache
        self._session: aiohttp.ClientSession | None = None
        self._auth: KISAuth | None = None
        self._credentials: AccountCredentials | None = None
        self._token_ttl: int = settings.KIS_TOKEN_REDIS_TTL

        # Rate limiting — take the slowest (max) interval across all instances so
        # a shared App Key/IP limit is never overrun. Last-writer-wins would be
        # instance-creation-order dependent and could pick a too-fast value (F-20).
        KISClient._global_rate_interval = max(
            KISClient._global_rate_interval, settings.KIS_RATE_LIMIT_INTERVAL
        )
        self._ledger_rate_interval = settings.KIS_LEDGER_RATE_LIMIT_INTERVAL
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
        if rate_limit_interval is not None:
            resolved_interval = rate_limit_interval
        else:
            resolved_interval = 0.5 if credentials.is_paper else 0.05
        # Take the slowest (max) interval across instances (see __init__, F-20).
        KISClient._global_rate_interval = max(
            KISClient._global_rate_interval, resolved_interval
        )
        # 원장 TR 전용 최소 간격 (paper는 느린 페이스, prod는 보수적 5건/초)
        instance._ledger_rate_interval = 0.5 if credentials.is_paper else 0.2
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
        """Execute a single KIS API call inside the global rate-limit semaphore.

        The global semaphore serializes ALL KIS API calls across every
        KISClient instance in this process, preventing rate-limit violations
        when multiple accounts fire requests concurrently.
        """
        if not self._session or not self._auth:
            raise BrokerError("KISClient not connected. Call connect() first.")

        async with KISClient._global_semaphore:
            token = await self._auth.get_token()
            headers = self._auth.build_headers(token, tr_id, tr_cont=tr_cont)
            url = f"{self._base_url}{path}"

            try:
                if method.upper() == "GET":
                    resp = await self._session.get(url, headers=headers, params=params)
                else:
                    resp = await self._session.post(url, headers=headers, json=body)

                # Rate limit sleep inside semaphore to guarantee interval
                await asyncio.sleep(KISClient._global_rate_interval)

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

        # Rate limit — exponential backoff retry.
        # EGW00201 = API gateway per-second limit, EGW00215 = ledger server
        # (원장) per-second limit. The ledger limit is separate and tighter but
        # recovers the same way, so both share the backoff retry path (F-20).
        if msg_cd in ("EGW00201", "EGW00215"):
            if rate_limit_retry >= self._max_rate_limit_retries:
                raise RateLimitError(
                    f"KIS rate limit [{msg_cd}] after {rate_limit_retry} retries: {msg1}"
                )
            wait = self._rate_limit_backoff_base * (2 ** rate_limit_retry)
            logger.warning(
                "kis_rate_limit_retry",
                msg_cd=msg_cd,
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
        """Download and parse KOSPI + KOSDAQ .mst.zip master files.

        지수업종 코드→업종명 매핑(``idxcode.mst.zip``)을 1회 받아 각 종목의
        지수업종중분류(``part2[7:11]``)를 업종명으로 해석해 ``StockInfo.sector``에 채운다.
        """
        sector_map = await self.get_industry_code_map()
        stocks: list[StockInfo] = []
        stocks.extend(
            await self._parse_mst(
                _MST_KOSPI_URL, MarketType.KOSPI, 228, sector_map=sector_map
            )
        )
        stocks.extend(
            await self._parse_mst(
                _MST_KOSDAQ_URL, MarketType.KOSDAQ, 222, sector_map=sector_map
            )
        )
        logger.info(
            "kis_stock_master_loaded", total=len(stocks), sectors=len(sector_map)
        )
        return stocks

    async def get_industry_code_map(self) -> dict[str, str]:
        """Download ``idxcode.mst.zip`` and return ``{지수업종코드: 업종명}``.

        실패(미연결/non-200/네트워크/파싱)는 빈 dict로 graceful 폴백 — 매핑이 없으면
        호출측(``_parse_mst``)이 원시 코드를 그대로 sector로 저장한다.
        """
        if not self._session:
            raise BrokerError("KISClient not connected. Call connect() first.")

        try:
            async with self._session.get(_MST_IDXCODE_URL, ssl=False) as resp:
                if resp.status != 200:
                    logger.error(
                        "kis_idxcode_download_failed",
                        url=_MST_IDXCODE_URL,
                        status=resp.status,
                    )
                    return {}
                raw_bytes = await resp.read()
        except aiohttp.ClientError as exc:
            logger.error("kis_idxcode_download_error", error=str(exc))
            return {}

        code_map: dict[str, str] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                for name in zf.namelist():
                    content = zf.read(name).decode("cp949", errors="replace")
                    for line in content.splitlines():
                        # 레코드: idx_div(1) + idx_code(4) + idx_name(...)
                        if len(line) < 5:
                            continue
                        code = line[1:5].strip()
                        idx_name = line[5:].strip()
                        if code and idx_name:
                            code_map[code] = idx_name
        except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
            logger.error("kis_idxcode_parse_error", error=str(exc))
            return {}

        return code_map

    async def _parse_mst(
        self,
        url: str,
        market_type: MarketType,
        part2_len: int,
        *,
        sector_map: dict[str, str] | None = None,
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
                        # part2: 고정폭 구간 — 지수업종중분류 코드 → 업종명 해석
                        part2 = line[-part2_len:]
                        mid_code = part2[
                            _SECTOR_MID_OFFSET : _SECTOR_MID_OFFSET + _SECTOR_MID_LEN
                        ].strip()
                        sector = ""
                        if mid_code:
                            # 명칭 매핑 우선, 미스/맵부재 시 원시 코드 폴백("기타" collapse 방지)
                            sector = (sector_map or {}).get(mid_code) or mid_code
                        stocks.append(
                            StockInfo(
                                symbol=short_code,
                                name=korean_name,
                                market_type=market_type,
                                sector=sector,
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

    async def _fetch_cancelable_orders(self) -> list[KISRvseCnclPsblOutput]:
        """Paginate TTTC0084R to collect all cancelable/amendable orders.

        주식정정취소가능주문조회. 정정취소 TR 호출에 필요한
        ``KRX_FWDG_ORD_ORGNO``(=``ord_gno_brno``)·``psbl_qty``·``ord_dvsn_cd``를
        원주문번호(``odno``)로 역조회하기 위해 사용한다.

        Pagination mirrors :meth:`_fetch_balance_pages` (``tr_cont`` + ``ctx_area_*``).
        """
        all_orders: list[KISRvseCnclPsblOutput] = []
        tr_cont_req = ""
        ctx_fk = ""
        ctx_nk = ""

        for _page in range(_MAX_BALANCE_PAGES):
            params: dict[str, str] = {
                "CANO": self._cano,
                "ACNT_PRDT_CD": self._acnt_prdt_cd,
                "INQR_DVSN_1": "0",   # 0: 주문
                "INQR_DVSN_2": "0",   # 0: 전체(매도+매수)
                "CTX_AREA_FK100": ctx_fk,
                "CTX_AREA_NK100": ctx_nk,
            }

            data = await self._request(
                "GET",
                "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
                "TTTC0084R",
                params=params,
                tr_cont=tr_cont_req,
            )

            for raw in data.get("output", []):
                all_orders.append(KISRvseCnclPsblOutput.model_validate(raw))

            resp_tr_cont = data.get("_tr_cont", "")
            if resp_tr_cont in ("M", "F"):
                tr_cont_req = "N"
                ctx_fk = data.get("ctx_area_fk100", "")
                ctx_nk = data.get("ctx_area_nk100", "")
            else:
                break

        return all_orders

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order via KIS 정정취소 (``order-rvsecncl``).

        경로 A — 취소 시점에 정정취소가능주문조회로 ``order_id``(원주문번호)를 매칭해
        ``KRX_FWDG_ORD_ORGNO``·``ORD_DVSN``·취소가능수량·단가를 얻어 전량 취소한다.
        ``KRX_FWDG_ORD_ORGNO``를 영속화하지 않아도 되므로 추상 시그니처를 유지한다.

        Returns:
            ``True``  — 취소 TR 접수 성공.
            ``False`` — 취소 대상 아님(이미 체결/취소/만료로 가능목록에 없거나
                        ``psbl_qty<=0``), 또는 KIS 업무 거부(``KISResponseError``).

        Raises:
            인프라성 예외(``APIError``/``RateLimitError``/``TokenExpiredError``)는
            삼키지 않고 그대로 전파한다 — 살아있는 주문을 잘못 취소로 오기록하지
            않기 위함(Safety-First).
        """
        cancelable = await self._fetch_cancelable_orders()
        target = next((o for o in cancelable if o.odno == order_id), None)
        if target is None or _to_int(target.psbl_qty) <= 0:
            logger.info(
                "kis_cancel_order_not_cancelable",
                order_id=order_id,
                reason="not_in_cancelable_list" if target is None else "no_psbl_qty",
            )
            return False

        body: dict[str, str] = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "KRX_FWDG_ORD_ORGNO": target.ord_gno_brno,
            "ORGN_ODNO": order_id,
            "ORD_DVSN": target.ord_dvsn_cd,
            "RVSE_CNCL_DVSN_CD": "02",   # 02: 취소
            "ORD_QTY": target.psbl_qty,
            "ORD_UNPR": target.ord_unpr,
            "QTY_ALL_ORD_YN": "Y",       # 잔량 전부 취소
            "EXCG_ID_DVSN_CD": "KRX",
        }
        tr_id = "VTTC0013U" if self._is_paper() else "TTTC0013U"

        try:
            await self._request(
                "POST",
                "/uapi/domestic-stock/v1/trading/order-rvsecncl",
                tr_id,
                body=body,
            )
        except KISResponseError as exc:
            # 업무 거부(예: 직전 체결 레이스로 취소 불가) — 오취소 방지 차원에서
            # False만 반환하고 호출측이 reconcile로 실제 상태를 확정하게 둔다.
            logger.warning(
                "kis_cancel_order_rejected",
                order_id=order_id,
                msg_cd=exc.msg_cd,
                msg1=exc.msg1,
            )
            return False

        logger.info(
            "kis_cancel_order_submitted",
            order_id=order_id,
            quantity=target.psbl_qty,
            symbol=target.pdno,
        )
        return True

    async def get_order_status(
        self, broker_order_id: str, *, order_date: date | None = None
    ) -> OrderResult:
        """Query the latest status of a previously submitted order.

        Uses KIS 주식일별주문체결조회:
        - 실전 TR_ID = TTTC0081R
        - 모의 TR_ID = VTTC0081R
        - 엔드포인트: ``/uapi/domestic-stock/v1/trading/inquire-daily-ccld``

        Returns an ``OrderResult`` whose ``status`` reflects:
        - FILLED: 총 체결수량 == 주문수량
        - PARTIALLY_FILLED: 0 < 총 체결수량 < 주문수량
        - CANCELLED: cncl_yn == 'Y'
        - REJECTED: rjct_qty > 0 또는 상태 코드
        - SUBMITTED: 그 외(미체결 잔량만 존재)
        """
        target_date = order_date or date.today()
        date_str = target_date.strftime("%Y%m%d")
        is_paper = self._is_paper()
        tr_id = "VTTC0081R" if is_paper else "TTTC0081R"

        params: dict[str, str] = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "INQR_STRT_DT": date_str,
            "INQR_END_DT": date_str,
            "SLL_BUY_DVSN_CD": "00",   # 전체
            "CCLD_DVSN": "00",         # 전체 (체결+미체결)
            "INQR_DVSN": "00",          # 역순
            "INQR_DVSN_3": "00",        # 전체
            "INQR_DVSN_1": "",
            "PDNO": "",
            "ORD_GNO_BRNO": "",
            "ODNO": broker_order_id,
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
            "EXCG_ID_DVSN_CD": "KRX",
        }

        data = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            tr_id,
            params=params,
        )
        raw_output1 = data.get("output1") or []
        rows = [KISOrderCcldOutput.model_validate(r) for r in raw_output1]
        matched = next((r for r in rows if r.odno == broker_order_id), None)

        if matched is None:
            # 주문번호 조회 실패 — 아직 접수 반영 전이거나 다른 날짜
            return OrderResult(
                order_id=broker_order_id,
                symbol="",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=0,
                price=Decimal(0),
                status=OrderStatus.SUBMITTED,
                filled_quantity=0,
                filled_price=None,
                commission=Decimal(0),
                timestamp=datetime.now(),
            )

        ord_qty = _to_int(matched.ord_qty)
        ccld_qty = _to_int(matched.tot_ccld_qty)
        rjct_qty = _to_int(matched.rjct_qty)
        avg_price = _to_decimal(matched.avg_prvs)
        is_cancelled = matched.cncl_yn.upper() == "Y"
        side = OrderSide.SELL if matched.sll_buy_dvsn_cd == "01" else OrderSide.BUY

        if is_cancelled:
            status = OrderStatus.CANCELLED
        elif rjct_qty > 0 and ccld_qty == 0:
            status = OrderStatus.REJECTED
        elif ord_qty > 0 and ccld_qty >= ord_qty:
            status = OrderStatus.FILLED
        elif ccld_qty > 0:
            status = OrderStatus.PARTIALLY_FILLED
        else:
            status = OrderStatus.SUBMITTED

        return OrderResult(
            order_id=broker_order_id,
            symbol=matched.pdno,
            side=side,
            order_type=OrderType.LIMIT,  # inquire API doesn't expose order_type cleanly
            quantity=ord_qty,
            price=_to_decimal(matched.ord_unpr),
            status=status,
            filled_quantity=ccld_qty,
            filled_price=avg_price if ccld_qty > 0 else None,
            commission=Decimal(0),  # KIS 별도 조회 필요 — 현재 단계에서는 0
            timestamp=datetime.now(),
        )

    def _is_paper(self) -> bool:
        """Determine whether this client is connected to paper or live."""
        if self._credentials is not None:
            return self._credentials.is_paper
        return self._base_url == _KIS_PAPER_BASE_URL

    async def get_buyable_cash(self, symbol: str, price: Decimal) -> Decimal:
        """미수없는매수금액(``nrcvb_buy_amt``)을 조회한다.

        KIS 매수가능조회 — TR ``TTTC8908R`` (실전) / ``VTTC8908R`` (모의).
        엔드포인트: ``/uapi/domestic-stock/v1/trading/inquire-psbl-order``.

        ``dnca_tot_amt``(예수금총액)은 D+2 정산 전 당일 매수분을 반영하지
        않아 "가용 현금"으로 쓸 수 없다. 이 API는 KIS가 산정한 실제
        미수 없는 매수 한도를 반환한다.

        Args:
            symbol: 종목 코드 (PDNO). 빈 문자열이면 0 반환.
            price: 주문 예정 단가. 양수여야 함.

        Returns:
            미수 없이 매수 가능한 KRW 금액. 응답이 비어있으면 0.
        """
        if not symbol or price <= Decimal(0):
            return Decimal(0)

        is_paper = self._is_paper()
        tr_id = "VTTC8908R" if is_paper else "TTTC8908R"

        params: dict[str, str] = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "PDNO": symbol,
            "ORD_UNPR": str(int(price)),
            # 지정가(00) 기준 현금 한도 조회. 수량 산정이 목적이 아닌
            # 금액 산정이므로 종목증거금률 영향이 적다.
            "ORD_DVSN": "00",
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        }

        data = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            tr_id,
            params=params,
        )
        raw_output = data.get("output")
        if not raw_output:
            return Decimal(0)

        output = KISPsblOrderOutput.model_validate(raw_output)
        return _to_decimal(output.nrcvb_buy_amt)

    async def get_sellable_quantity(self, symbol: str) -> int | None:
        """매도가능수량(``ord_psbl_qty``)을 조회한다(F-12 매도 preflight).

        KIS 매도가능수량조회 — TR ``TTTC8408R``(실전).
        엔드포인트: ``/uapi/domestic-stock/v1/trading/inquire-psbl-sell``.

        보유수량(``hldg_qty``)만으로는 미체결 매도주문·결제미수로 줄어든
        실제 매도가능수량을 알 수 없다. 이 API는 KIS가 산정한 주문가능수량을
        반환하므로, 매도 발주 전 과매도(KIS 거부)를 사전에 막을 수 있다.

        모의계좌(``VTTC8408R``)는 본 TR 지원이 불확실하므로 모의이거나 조회가
        실패하면 ``None``(=preflight 정보 없음 → 클램프 미적용)을 반환한다.
        호출부는 ``None``을 "차단하지 않음"으로 해석해야 한다(긴급 손절 스트랜딩 방지).

        Args:
            symbol: 종목 코드 (PDNO). 빈 문자열이면 None.

        Returns:
            매도가능수량(주). 조회 불가/실패/모의 미지원이면 ``None``.
        """
        if not symbol:
            return None

        is_paper = self._is_paper()
        tr_id = "VTTC8408R" if is_paper else "TTTC8408R"

        params: dict[str, str] = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "PDNO": symbol,
        }

        try:
            data = await self._request(
                "GET",
                "/uapi/domestic-stock/v1/trading/inquire-psbl-sell",
                tr_id,
                params=params,
            )
        except (KISResponseError, APIError, BrokerError) as exc:
            logger.warning(
                "kis.sellable_quantity_fetch_failed",
                tr_id=tr_id,
                symbol=symbol,
                error=str(exc),
            )
            return None

        raw_output = data.get("output")
        if isinstance(raw_output, list):
            raw_output = raw_output[0] if raw_output else None
        if not raw_output:
            return None

        output = KISPsblSellOutput.model_validate(raw_output)
        return _to_int(output.ord_psbl_qty)

    # ── Account ───────────────────────────────────────────────────────

    async def get_balance(self) -> AccountBalance:
        """Fetch account balance with paginated position data."""
        positions, summary = await self._fetch_balance_pages()

        cash = _to_decimal(summary.dnca_tot_amt)
        invested = _to_decimal(summary.pchs_amt_smtl_amt)
        unrealized = _to_decimal(summary.evlu_pfls_smtl_amt)
        daily_pnl, daily_pnl_pct = await self._fetch_daily_realized_pnl()

        return AccountBalance(
            total_assets=_to_decimal(summary.tot_evlu_amt),
            cash=cash,
            invested=invested,
            unrealized_pnl=unrealized,
            realized_pnl=daily_pnl,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
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

    async def get_balance_and_positions(self) -> tuple[AccountBalance, list[Position]]:
        """Fetch balance and positions in a single ``_fetch_balance_pages()`` call.

        Avoids the duplicate API round-trip that happens when
        ``get_balance()`` and ``get_positions()`` are called separately.
        """
        raw_positions, summary = await self._fetch_balance_pages()

        # Build AccountBalance from summary
        cash = _to_decimal(summary.dnca_tot_amt)
        invested = _to_decimal(summary.pchs_amt_smtl_amt)
        unrealized = _to_decimal(summary.evlu_pfls_smtl_amt)
        daily_pnl, daily_pnl_pct = await self._fetch_daily_realized_pnl()

        balance = AccountBalance(
            total_assets=_to_decimal(summary.tot_evlu_amt),
            cash=cash,
            invested=invested,
            unrealized_pnl=unrealized,
            realized_pnl=daily_pnl,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            positions_count=len([p for p in raw_positions if _to_int(p.hldg_qty) > 0]),
            timestamp=datetime.now(),
        )

        # Build Position list
        positions = [
            item.to_domain()
            for item in raw_positions
            if _to_int(item.hldg_qty) > 0
        ]

        return balance, positions

    async def _ledger_pace(self) -> None:
        """원장(잔고·실현손익) TR 사이에 원장 전용 최소 간격을 둔다.

        게이트웨이 초당한도(``_global_rate_interval``, ``_do_request`` 내 sleep)와 별개로,
        원장 서버 초당한도(EGW00215)는 더 빡빡하다. 잔고 페이지 연사·잔고→실현손익 연사가
        원장 한도를 넘지 않도록 원장 TR burst에만 추가 간격을 둔다. (F-20)
        """
        await asyncio.sleep(self._ledger_rate_interval)

    async def _fetch_daily_realized_pnl(self) -> tuple[Decimal, Decimal]:
        """당일 실현손익·실현수익률을 조회한다.

        KIS 주식잔고조회_실현손익 — TR ``TTTC8494R`` (실전) / ``VTTC8494R`` (모의).
        엔드포인트: ``/uapi/domestic-stock/v1/trading/inquire-balance-rlz-pl``.

        ``daily_pnl``을 "당일 매도대금 − 당일 매수대금"(순 매매현금흐름)이 아니라
        **실제 당일 실현손익**으로 채우기 위한 보조 조회. ``PRCS_DVSN="01"``
        (전일매매 미포함)이라 당일 청산분 손익만 집계되며, ``output2`` 요약 1건만
        필요해 페이지네이션 없이 1회 요청한다.

        Returns:
            ``(실현손익, 실현수익률)``. 응답이 비어있거나 모의계좌 미지원 등으로
            조회에 실패하면 ``(0, 0)`` — 실현손익 조회 실패가 잔고 조회 전체를
            깨지 않게 하고, 한도 미발동(과차단보다 안전)으로 폴백한다.
        """
        # 잔고 페이지 burst 직후 원장 TR을 연사하지 않도록 원장 전용 간격을 둔다.
        await self._ledger_pace()

        is_paper = self._is_paper()
        tr_id = "VTTC8494R" if is_paper else "TTTC8494R"

        params: dict[str, str] = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",  # 전일매매 미포함 → 당일분만
            "COST_ICLD_YN": "N",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        try:
            data = await self._request(
                "GET",
                "/uapi/domestic-stock/v1/trading/inquire-balance-rlz-pl",
                tr_id,
                params=params,
            )
        except (KISResponseError, APIError, BrokerError) as exc:
            logger.warning(
                "kis.daily_realized_pnl_fetch_failed",
                tr_id=tr_id,
                error=str(exc),
            )
            return Decimal(0), Decimal(0)

        raw_output2 = data.get("output2")
        if isinstance(raw_output2, list):
            raw_output2 = raw_output2[0] if raw_output2 else None
        if not raw_output2:
            return Decimal(0), Decimal(0)

        summary = KISBalanceRlzPlOutput2.model_validate(raw_output2)
        return _to_decimal(summary.rlzt_pfls), _to_decimal(summary.rlzt_erng_rt)

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
                # 원장 페이지 연사가 원장 초당한도를 넘지 않도록 다음 페이지 전 간격을 둔다.
                await self._ledger_pace()
            else:
                break

        if summary is None:
            summary = KISBalanceOutput2()

        return all_positions, summary
