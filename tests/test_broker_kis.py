"""Unit tests for KIS models, KISClient, and InMemoryBroker.

All HTTP calls are mocked — no external dependencies.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from src.broker.kis.auth import KISAuth
from src.broker.kis.client import KISClient
from src.broker.kis.models import (
    KISBalanceOutput1,
    KISBalanceOutput2,
    KISBalanceRlzPlOutput2,
    KISBaseResponse,
    KISDailyChartOutput,
    KISPriceOutput,
    _to_decimal,
    _to_int,
)
from src.broker.mock.client import InMemoryBroker
from src.core.enums import (
    MarketType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
)
from src.core.exceptions import (
    APIError,
    BrokerError,
    InsufficientFundsError,
    KISResponseError,
    OrderError,
    RateLimitError,
    TokenExpiredError,
)
from src.core.models import OHLCV, OrderRequest, PriceInfo, StockInfo
from src.data.cache import RedisCache

from conftest import AsyncContextManagerMock, make_settings, mock_aiohttp_response


# ── Helpers ───────────────────────────────────────────────────────────


def _make_kis_client(settings=None, cache=None):
    """KISClient with mocked _session/_auth (skip connect())."""
    s = settings or make_settings()
    c = cache or MagicMock(spec=RedisCache)
    client = KISClient(settings=s, cache=c)
    client._session = MagicMock(spec=aiohttp.ClientSession)
    client._session.closed = False
    client._auth = MagicMock(spec=KISAuth)
    client._auth.get_token = AsyncMock(return_value="test_token")
    client._auth.build_headers = MagicMock(return_value={"tr_id": "TEST"})
    return client


def _build_mst_zip(lines: list[str], encoding: str = "cp949") -> bytes:
    """Build an in-memory .mst.zip with given lines."""
    content = "\n".join(lines).encode(encoding)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("test.mst", content)
    return buf.getvalue()


def _make_ok_response(output=None, output2=None, tr_cont=""):
    """Build a KIS OK response dict."""
    data = {"rt_cd": "0", "msg_cd": "0000", "msg1": "정상처리"}
    if output is not None:
        data["output"] = output
    if output2 is not None:
        data["output2"] = output2
    return mock_aiohttp_response(json_data=data, headers={"tr_cont": tr_cont})


# ═══════════════════════════════════════════════════════════════════════
# 2.1 KIS Models
# ═══════════════════════════════════════════════════════════════════════


class TestToDecimal:
    def test_valid_decimal(self):
        assert _to_decimal("72000.50") == Decimal("72000.50")

    def test_valid_integer(self):
        assert _to_decimal("100") == Decimal("100")

    def test_empty_string(self):
        assert _to_decimal("") == Decimal(0)

    def test_whitespace_only(self):
        assert _to_decimal("  ") == Decimal(0)

    def test_invalid_string(self):
        assert _to_decimal("abc") == Decimal(0)

    def test_negative(self):
        assert _to_decimal("-500") == Decimal("-500")

    def test_strips_whitespace(self):
        assert _to_decimal(" 123 ") == Decimal("123")


class TestToInt:
    def test_valid(self):
        assert _to_int("42") == 42

    def test_empty(self):
        assert _to_int("") == 0

    def test_whitespace(self):
        assert _to_int("   ") == 0

    def test_invalid(self):
        assert _to_int("abc") == 0

    def test_float_string(self):
        assert _to_int("3.14") == 0

    def test_negative(self):
        assert _to_int("-10") == -10

    def test_strips(self):
        assert _to_int(" 99 ") == 99


class TestKISBaseResponse:
    def test_is_ok_true(self):
        r = KISBaseResponse(rt_cd="0")
        assert r.is_ok is True

    def test_is_ok_false(self):
        r = KISBaseResponse(rt_cd="1")
        assert r.is_ok is False

    def test_extra_fields_ignored(self):
        r = KISBaseResponse(rt_cd="0", unknown_field="hello")
        assert r.is_ok is True


class TestKISPriceOutput:
    def test_to_domain_positive_sign(self):
        out = KISPriceOutput(
            stck_prpr="72000", prdy_vrss="500", prdy_vrss_sign="2",
            prdy_ctrt="0.70", acml_vol="15000000",
        )
        pi = out.to_domain("005930")
        assert pi.change_price == Decimal("500")
        assert pi.change_percent == Decimal("0.70")

    def test_to_domain_negative_sign_4(self):
        out = KISPriceOutput(
            stck_prpr="71000", prdy_vrss="500", prdy_vrss_sign="4",
            prdy_ctrt="0.70",
        )
        pi = out.to_domain("005930")
        assert pi.change_price == Decimal("-500")
        assert pi.change_percent == Decimal("-0.70")

    def test_to_domain_negative_sign_5(self):
        out = KISPriceOutput(
            stck_prpr="71000", prdy_vrss="1000", prdy_vrss_sign="5",
            prdy_ctrt="1.40",
        )
        pi = out.to_domain("005930")
        assert pi.change_price == Decimal("-1000")

    def test_to_domain_flat_sign_3(self):
        out = KISPriceOutput(
            stck_prpr="72000", prdy_vrss="0", prdy_vrss_sign="3",
            prdy_ctrt="0",
        )
        pi = out.to_domain("005930")
        assert pi.change_price == Decimal(0)

    def test_to_domain_returns_price_info(self):
        out = KISPriceOutput(
            stck_prpr="72000", prdy_vrss="500", prdy_vrss_sign="2",
            prdy_ctrt="0.70", acml_vol="15000000", stck_hgpr="72500",
            stck_lwpr="71000", stck_sdpr="71500",
        )
        pi = out.to_domain("005930")
        assert pi.symbol == "005930"
        assert pi.current_price == Decimal("72000")
        assert pi.previous_close == Decimal("71500")
        assert pi.high == Decimal("72500")
        assert pi.low == Decimal("71000")
        assert pi.volume == 15000000
        assert pi.timestamp is not None


class TestKISDailyChartOutput:
    def test_to_domain_date_parsing(self):
        out = KISDailyChartOutput(stck_bsop_date="20260301", stck_clpr="100")
        bar = out.to_domain("005930")
        assert bar.date == date(2026, 3, 1)

    def test_to_domain_fields_mapped(self):
        out = KISDailyChartOutput(
            stck_bsop_date="20260301", stck_clpr="72000",
            stck_oprc="71500", stck_hgpr="72500", stck_lwpr="71000",
            acml_vol="15000000", acml_tr_pbmn="1080000000000",
        )
        bar = out.to_domain("005930")
        assert bar.open == Decimal("71500")
        assert bar.high == Decimal("72500")
        assert bar.low == Decimal("71000")
        assert bar.close == Decimal("72000")
        assert bar.volume == 15000000
        assert bar.value == Decimal("1080000000000")

    def test_to_domain_zero_values(self):
        out = KISDailyChartOutput(stck_bsop_date="20260301")
        bar = out.to_domain("005930")
        assert bar.open == Decimal(0)
        assert bar.volume == 0


class TestKISBalanceOutput1:
    def test_to_domain_open(self):
        out = KISBalanceOutput1(pdno="005930", hldg_qty="10", prpr="72000")
        pos = out.to_domain()
        assert pos.status == PositionStatus.OPEN
        assert pos.quantity == 10

    def test_to_domain_closed(self):
        out = KISBalanceOutput1(pdno="005930", hldg_qty="0")
        pos = out.to_domain()
        assert pos.status == PositionStatus.CLOSED

    def test_to_domain_field_mapping(self):
        out = KISBalanceOutput1(
            pdno="005930", hldg_qty="10", pchs_avg_pric="70000",
            prpr="72000", evlu_amt="720000", evlu_pfls_amt="20000",
            evlu_pfls_rt="2.86",
        )
        pos = out.to_domain()
        assert pos.symbol == "005930"
        assert pos.average_cost == Decimal("70000")
        assert pos.current_price == Decimal("72000")
        assert pos.market_value == Decimal("720000")


class TestKISBalanceRlzPlOutput2:
    """TTTC8494R output2(실현손익 요약) 파싱."""

    def test_parses_realized_fields(self):
        out = KISBalanceRlzPlOutput2(rlzt_pfls="123456", rlzt_erng_rt="2.34")
        assert _to_decimal(out.rlzt_pfls) == Decimal("123456")
        assert _to_decimal(out.rlzt_erng_rt) == Decimal("2.34")

    def test_defaults_empty(self):
        out = KISBalanceRlzPlOutput2()
        assert _to_decimal(out.rlzt_pfls) == Decimal(0)
        assert _to_decimal(out.rlzt_erng_rt) == Decimal(0)

    def test_ignores_extra_fields(self):
        out = KISBalanceRlzPlOutput2.model_validate(
            {"rlzt_pfls": "-5000", "rlzt_erng_rt": "-1.2", "dnca_tot_amt": "999"}
        )
        assert _to_decimal(out.rlzt_pfls) == Decimal("-5000")


# ═══════════════════════════════════════════════════════════════════════
# 2.2 KISClient
# ═══════════════════════════════════════════════════════════════════════


class TestKISClientLifecycle:
    @pytest.mark.asyncio
    async def test_connect_creates_session_and_auth(self):
        client = KISClient(settings=make_settings(), cache=MagicMock(spec=RedisCache))
        with (
            patch("src.broker.kis.client.aiohttp.ClientSession") as mock_sess_cls,
            patch("src.broker.kis.client.KISAuth") as mock_auth_cls,
        ):
            mock_session = MagicMock()
            mock_sess_cls.return_value = mock_session
            mock_auth = MagicMock()
            mock_auth.get_token = AsyncMock(return_value="token")
            mock_auth_cls.return_value = mock_auth

            await client.connect()

            mock_sess_cls.assert_called_once()
            mock_auth_cls.assert_called_once()
            mock_auth.get_token.assert_awaited_once()
            assert client._session is mock_session

    @pytest.mark.asyncio
    async def test_disconnect_closes_session(self):
        client = _make_kis_client()
        session = client._session
        session.closed = False
        session.close = AsyncMock()

        await client.disconnect()

        session.close.assert_awaited_once()
        assert client._session is None
        assert client._auth is None

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self):
        client = KISClient(settings=make_settings(), cache=MagicMock(spec=RedisCache))
        await client.disconnect()  # no error

    @pytest.mark.asyncio
    async def test_context_manager(self):
        with (
            patch("src.broker.kis.client.aiohttp.ClientSession") as mock_sess_cls,
            patch("src.broker.kis.client.KISAuth") as mock_auth_cls,
        ):
            mock_session = MagicMock()
            mock_session.closed = False
            mock_session.close = AsyncMock()
            mock_sess_cls.return_value = mock_session
            mock_auth = MagicMock()
            mock_auth.get_token = AsyncMock(return_value="token")
            mock_auth_cls.return_value = mock_auth

            client = KISClient(settings=make_settings(), cache=MagicMock(spec=RedisCache))
            async with client:
                assert client._session is not None
            assert client._session is None


class TestKISClientRequest:
    @pytest.mark.asyncio
    async def test_get_request_success(self):
        client = _make_kis_client()
        resp = _make_ok_response(output={"stck_prpr": "72000"})
        client._session.get = AsyncMock(return_value=resp)

        data = await client._request("GET", "/test", "FHKST01010100", params={"k": "v"})
        assert data["output"]["stck_prpr"] == "72000"
        client._session.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_post_request_success(self):
        client = _make_kis_client()
        resp = _make_ok_response(output={"ODNO": "123"})
        client._session.post = AsyncMock(return_value=resp)

        data = await client._request("POST", "/test", "TTTC0012U", body={"k": "v"})
        assert data["output"]["ODNO"] == "123"
        client._session.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_not_connected_raises(self):
        client = _make_kis_client()
        client._session = None
        with pytest.raises(BrokerError, match="not connected"):
            await client._request("GET", "/test", "TEST")

    @pytest.mark.asyncio
    async def test_network_error_raises_api_error(self):
        client = _make_kis_client()
        client._session.get = AsyncMock(side_effect=aiohttp.ClientError("fail"))
        with pytest.raises(APIError, match="KIS request failed"):
            await client._request("GET", "/test", "TEST")

    @pytest.mark.asyncio
    async def test_json_parse_error(self):
        client = _make_kis_client()
        resp = MagicMock()
        resp.json = AsyncMock(side_effect=ValueError("bad json"))
        resp.text = AsyncMock(return_value="not json")
        resp.headers = {"tr_cont": ""}
        client._session.get = AsyncMock(return_value=resp)
        with pytest.raises(APIError, match="JSON parse error"):
            await client._request("GET", "/test", "TEST")

    @pytest.mark.asyncio
    async def test_rate_limit_sleep(self):
        client = _make_kis_client(settings=make_settings(KIS_RATE_LIMIT_INTERVAL=0.1))
        resp = _make_ok_response()
        client._session.get = AsyncMock(return_value=resp)

        with patch("src.broker.kis.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await client._request("GET", "/test", "TEST")
            mock_sleep.assert_awaited_once_with(0.1)

    @pytest.mark.asyncio
    async def test_tr_cont_header_injected(self):
        client = _make_kis_client()
        resp = _make_ok_response(tr_cont="M")
        client._session.get = AsyncMock(return_value=resp)

        data = await client._request("GET", "/test", "TEST")
        assert data["_tr_cont"] == "M"


class TestKISClientHandleError:
    @pytest.mark.asyncio
    async def test_token_expired_egw00123_retries(self):
        client = _make_kis_client()
        err_resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "expired"},
            headers={"tr_cont": ""},
        )
        ok_resp = _make_ok_response(output={"result": "ok"})
        client._session.get = AsyncMock(side_effect=[err_resp, ok_resp])
        client._auth.refresh_token = AsyncMock()

        data = await client._request("GET", "/test", "TEST")
        assert data["output"]["result"] == "ok"
        client._auth.refresh_token.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_token_expired_egw00121_retries(self):
        client = _make_kis_client()
        err_resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "EGW00121", "msg1": "expired"},
            headers={"tr_cont": ""},
        )
        ok_resp = _make_ok_response(output={"result": "ok"})
        client._session.get = AsyncMock(side_effect=[err_resp, ok_resp])
        client._auth.refresh_token = AsyncMock()

        data = await client._request("GET", "/test", "TEST")
        client._auth.refresh_token.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_token_expired_on_retry_raises(self):
        client = _make_kis_client()
        err_resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "expired"},
            headers={"tr_cont": ""},
        )
        # Both calls return error
        client._session.get = AsyncMock(side_effect=[err_resp, err_resp])
        client._auth.refresh_token = AsyncMock()

        with pytest.raises(TokenExpiredError):
            await client._request("GET", "/test", "TEST")

    @pytest.mark.asyncio
    async def test_rate_limit_egw00201_exhausts_retries(self):
        """max_retries(3)회 재시도 후에도 rate limit이면 RateLimitError 발생."""
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "rate limit"},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)
        with patch("src.broker.kis.client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RateLimitError, match="after 3 retries"):
                await client._request("GET", "/test", "TEST")
        # 최초 1회 + 재시도 3회 = 총 4회 호출
        assert client._session.get.await_count == 4

    @pytest.mark.asyncio
    async def test_rate_limit_retry_then_success(self):
        """1회 rate limit 후 2회째 성공하면 정상 반환."""
        client = _make_kis_client()
        err_resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "rate limit"},
            headers={"tr_cont": ""},
        )
        ok_resp = _make_ok_response(output={"result": "ok"})
        client._session.get = AsyncMock(side_effect=[err_resp, ok_resp])
        with patch("src.broker.kis.client.asyncio.sleep", new_callable=AsyncMock):
            data = await client._request("GET", "/test", "TEST")
        assert data["output"]["result"] == "ok"
        assert client._session.get.await_count == 2

    @pytest.mark.asyncio
    async def test_rate_limit_backoff_sleep(self):
        """재시도 시 지수 백오프로 sleep이 호출되는지 확인."""
        client = _make_kis_client()
        client._rate_limit_backoff_base = 1.0
        client._max_rate_limit_retries = 3
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "rate limit"},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)
        with patch("src.broker.kis.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(RateLimitError):
                await client._request("GET", "/test", "TEST")
        # 일반 rate limit sleep(semaphore 내) 4회 + backoff sleep 3회
        backoff_calls = [c.args[0] for c in mock_sleep.await_args_list]
        # backoff: 1.0, 2.0, 4.0 이 포함되어야 함
        assert 1.0 in backoff_calls
        assert 2.0 in backoff_calls
        assert 4.0 in backoff_calls

    @pytest.mark.asyncio
    async def test_insufficient_funds_apbk0013(self):
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "APBK0013", "msg1": "no funds"},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)
        with pytest.raises(InsufficientFundsError):
            await client._request("GET", "/test", "TEST")

    @pytest.mark.asyncio
    async def test_generic_error(self):
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "1", "msg_cd": "UNKNOWN", "msg1": "something"},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)
        with pytest.raises(KISResponseError) as exc_info:
            await client._request("GET", "/test", "TEST")
        assert exc_info.value.msg_cd == "UNKNOWN"
        assert exc_info.value.msg1 == "something"


class TestKISClientGetPrice:
    @pytest.mark.asyncio
    async def test_returns_price_info(self):
        client = _make_kis_client()
        resp = _make_ok_response(output={
            "stck_prpr": "72000", "prdy_vrss": "500", "prdy_vrss_sign": "2",
            "prdy_ctrt": "0.70", "acml_vol": "15000000", "stck_hgpr": "72500",
            "stck_lwpr": "71000", "stck_sdpr": "71500",
        })
        client._session.get = AsyncMock(return_value=resp)

        pi = await client.get_price("005930")
        assert pi.symbol == "005930"
        assert pi.current_price == Decimal("72000")

    @pytest.mark.asyncio
    async def test_empty_output(self):
        client = _make_kis_client()
        resp = _make_ok_response(output={})
        client._session.get = AsyncMock(return_value=resp)

        pi = await client.get_price("005930")
        assert pi.current_price == Decimal(0)


class TestKISClientGetDailyOHLCV:
    def _make_items(self, count, start_date_str="20260201"):
        """Generate count items with sequential dates."""
        items = []
        y, m, d = int(start_date_str[:4]), int(start_date_str[4:6]), int(start_date_str[6:8])
        for i in range(count):
            dt = date(y, m, d) - __import__("datetime").timedelta(days=i)
            items.append({
                "stck_bsop_date": dt.strftime("%Y%m%d"),
                "stck_clpr": "72000", "stck_oprc": "71500",
                "stck_hgpr": "72500", "stck_lwpr": "71000",
                "acml_vol": "15000000",
            })
        return items

    @pytest.mark.asyncio
    async def test_single_page_under_100(self):
        client = _make_kis_client()
        items = self._make_items(50)
        resp = _make_ok_response(output2=items)
        client._session.get = AsyncMock(return_value=resp)

        bars = await client.get_daily_ohlcv("005930", period_days=100)
        assert len(bars) == 50

    @pytest.mark.asyncio
    async def test_pagination_two_pages(self):
        client = _make_kis_client()
        page1 = self._make_items(100, "20260301")
        page2 = self._make_items(30, "20251122")

        resp1 = _make_ok_response(output2=page1)
        resp2 = _make_ok_response(output2=page2)
        client._session.get = AsyncMock(side_effect=[resp1, resp2])

        bars = await client.get_daily_ohlcv("005930", period_days=365)
        # 100 + 30 = 130, minus any date overlap between pages
        assert len(bars) >= 129
        assert client._session.get.await_count == 2

    @pytest.mark.asyncio
    async def test_empty_result(self):
        client = _make_kis_client()
        resp = _make_ok_response(output2=[])
        client._session.get = AsyncMock(return_value=resp)

        bars = await client.get_daily_ohlcv("005930")
        assert bars == []

    @pytest.mark.asyncio
    async def test_dedup_same_date(self):
        client = _make_kis_client()
        items = [
            {"stck_bsop_date": "20260301", "stck_clpr": "72000", "stck_oprc": "71500",
             "stck_hgpr": "72500", "stck_lwpr": "71000", "acml_vol": "15000000"},
            {"stck_bsop_date": "20260301", "stck_clpr": "72100", "stck_oprc": "71600",
             "stck_hgpr": "72600", "stck_lwpr": "71100", "acml_vol": "15000001"},
        ]
        resp = _make_ok_response(output2=items)
        client._session.get = AsyncMock(return_value=resp)

        bars = await client.get_daily_ohlcv("005930")
        assert len(bars) == 1

    @pytest.mark.asyncio
    async def test_skips_empty_bsop_date(self):
        client = _make_kis_client()
        items = [
            {"stck_bsop_date": "", "stck_clpr": "72000"},
            {"stck_bsop_date": "20260301", "stck_clpr": "72000", "stck_oprc": "71500",
             "stck_hgpr": "72500", "stck_lwpr": "71000", "acml_vol": "15000000"},
        ]
        resp = _make_ok_response(output2=items)
        client._session.get = AsyncMock(return_value=resp)

        bars = await client.get_daily_ohlcv("005930")
        assert len(bars) == 1

    @pytest.mark.asyncio
    async def test_max_pages_safety(self):
        client = _make_kis_client()
        # Always return 100 items with unique dates
        call_count = 0

        async def _side_effect(*args, **kwargs):
            nonlocal call_count
            items = self._make_items(100, f"2026{(call_count + 1):02d}01")
            call_count += 1
            return _make_ok_response(output2=items)

        client._session.get = AsyncMock(side_effect=_side_effect)

        bars = await client.get_daily_ohlcv("005930", period_days=10000)
        assert client._session.get.await_count <= 20


class TestKISClientGetStockMaster:
    @pytest.mark.asyncio
    async def test_combines_kospi_kosdaq(self):
        client = _make_kis_client()
        kospi = [StockInfo(symbol="005930", name="삼성전자", market_type=MarketType.KOSPI)]
        kosdaq = [StockInfo(symbol="035720", name="카카오", market_type=MarketType.KOSDAQ)]

        with patch.object(client, "_parse_mst", new_callable=AsyncMock, side_effect=[kospi, kosdaq]):
            result = await client.get_stock_master()

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_empty_on_failure(self):
        client = _make_kis_client()
        with patch.object(client, "_parse_mst", new_callable=AsyncMock, return_value=[]):
            result = await client.get_stock_master()
        assert result == []

    @pytest.mark.asyncio
    async def test_correct_params(self):
        client = _make_kis_client()
        with patch.object(client, "_parse_mst", new_callable=AsyncMock, return_value=[]) as mock:
            await client.get_stock_master()
            calls = mock.call_args_list
            # KOSPI part2_len=228, KOSDAQ part2_len=222
            assert calls[0].args[2] == 228
            assert calls[1].args[2] == 222


class TestKISClientParseMst:
    @pytest.mark.asyncio
    async def test_valid_zip(self):
        client = _make_kis_client()
        # part1: short_code(9) + standard_code(12) + korean_name
        # part2: part2_len characters
        part2_len = 228
        short_code = "005930   "  # 9 chars
        standard_code = "KR7005930003"  # 12 chars
        korean_name = "삼성전자"
        part1 = short_code + standard_code + korean_name
        part2 = "X" * part2_len
        line = part1 + part2

        zip_bytes = _build_mst_zip([line])
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=zip_bytes)
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        result = await client._parse_mst("http://test.zip", MarketType.KOSPI, part2_len)
        assert len(result) == 1
        assert result[0].symbol == "005930"
        assert result[0].name == "삼성전자"

    @pytest.mark.asyncio
    async def test_not_connected(self):
        client = _make_kis_client()
        client._session = None
        with pytest.raises(BrokerError, match="not connected"):
            await client._parse_mst("http://test.zip", MarketType.KOSPI, 228)

    @pytest.mark.asyncio
    async def test_non_200_returns_empty(self):
        client = _make_kis_client()
        resp = MagicMock()
        resp.status = 404
        resp.read = AsyncMock(return_value=b"")
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        result = await client._parse_mst("http://test.zip", MarketType.KOSPI, 228)
        assert result == []

    @pytest.mark.asyncio
    async def test_network_error_returns_empty(self):
        client = _make_kis_client()
        client._session.get = MagicMock(side_effect=aiohttp.ClientError("fail"))
        result = await client._parse_mst("http://test.zip", MarketType.KOSPI, 228)
        assert result == []

    @pytest.mark.asyncio
    async def test_filters_non_6digit(self):
        client = _make_kis_client()
        part2_len = 228
        # 9-digit code should be filtered
        line1 = "123456789" + "KR7005930003" + "테스트" + "X" * part2_len
        # Valid 6-digit
        line2 = "005930   " + "KR7005930003" + "삼성전자" + "X" * part2_len

        zip_bytes = _build_mst_zip([line1, line2])
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=zip_bytes)
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        result = await client._parse_mst("http://test.zip", MarketType.KOSPI, part2_len)
        assert len(result) == 1
        assert result[0].symbol == "005930"

    @pytest.mark.asyncio
    async def test_short_lines_skipped(self):
        client = _make_kis_client()
        part2_len = 228
        short_line = "X" * 10  # Too short

        zip_bytes = _build_mst_zip([short_line])
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=zip_bytes)
        client._session.get = MagicMock(return_value=AsyncContextManagerMock(resp))

        result = await client._parse_mst("http://test.zip", MarketType.KOSPI, part2_len)
        assert result == []


class TestKISClientPlaceOrder:
    @pytest.mark.asyncio
    async def test_buy_market(self):
        client = _make_kis_client()
        resp = _make_ok_response(output={"ODNO": "12345", "KRX_FWDG_ORD_ORGNO": "", "ORD_TMD": "100000"})
        client._session.post = AsyncMock(return_value=resp)

        order = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        result = await client.place_order(order)

        call_kwargs = client._session.post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["ORD_DVSN"] == "01"
        assert body["ORD_UNPR"] == "0"
        assert result.order_id == "12345"

    @pytest.mark.asyncio
    async def test_sell_limit(self):
        client = _make_kis_client()
        resp = _make_ok_response(output={"ODNO": "67890", "KRX_FWDG_ORD_ORGNO": "", "ORD_TMD": "100000"})
        client._session.post = AsyncMock(return_value=resp)

        order = OrderRequest(
            symbol="005930", side=OrderSide.SELL,
            order_type=OrderType.LIMIT, quantity=5, price=Decimal("75000"),
        )
        result = await client.place_order(order)

        call_kwargs = client._session.post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["ORD_DVSN"] == "00"
        assert "SLL_TYPE" in body
        assert result.side == OrderSide.SELL

    def _cancelable_row(self, **overrides):
        row = {
            "odno": "12345",
            "ord_gno_brno": "00950",
            "ord_dvsn_cd": "00",
            "psbl_qty": "10",
            "ord_unpr": "75000",
            "sll_buy_dvsn_cd": "02",
            "pdno": "005930",
        }
        row.update(overrides)
        return row

    @pytest.mark.asyncio
    async def test_cancel_order_success_builds_cancel_body(self):
        client = _make_kis_client()
        client._is_paper = lambda: False  # type: ignore[method-assign]
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"output": [self._cancelable_row()], "_tr_cont": ""},
                {"output": {}, "_tr_cont": ""},
            ]
        )

        assert await client.cancel_order("12345") is True
        assert client._request.await_count == 2

        # 1번째: 정정취소가능주문조회(GET)
        first = client._request.call_args_list[0]
        assert first.args[0] == "GET"
        assert "inquire-psbl-rvsecncl" in first.args[1]
        assert first.args[2] == "TTTC0084R"

        # 2번째: 정정취소(POST) — body 검증
        second = client._request.call_args_list[1]
        assert second.args[0] == "POST"
        assert "order-rvsecncl" in second.args[1]
        assert second.args[2] == "TTTC0013U"  # 실전
        body = second.kwargs["body"]
        assert body["RVSE_CNCL_DVSN_CD"] == "02"
        assert body["QTY_ALL_ORD_YN"] == "Y"
        assert body["EXCG_ID_DVSN_CD"] == "KRX"
        assert body["KRX_FWDG_ORD_ORGNO"] == "00950"
        assert body["ORGN_ODNO"] == "12345"
        assert body["ORD_DVSN"] == "00"
        assert body["ORD_QTY"] == "10"
        assert body["ORD_UNPR"] == "75000"

    @pytest.mark.asyncio
    async def test_cancel_order_paper_uses_demo_tr_id(self):
        client = _make_kis_client()
        client._is_paper = lambda: True  # type: ignore[method-assign]
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"output": [self._cancelable_row()], "_tr_cont": ""},
                {"output": {}, "_tr_cont": ""},
            ]
        )

        assert await client.cancel_order("12345") is True
        assert client._request.call_args_list[1].args[2] == "VTTC0013U"

    @pytest.mark.asyncio
    async def test_cancel_order_not_in_cancelable_list_returns_false(self):
        client = _make_kis_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            return_value={"output": [self._cancelable_row(odno="99999")], "_tr_cont": ""}
        )

        assert await client.cancel_order("12345") is False
        # 취소 TR(POST)은 호출되지 않음 — 조회 1회만
        assert client._request.await_count == 1

    @pytest.mark.asyncio
    async def test_cancel_order_no_psbl_qty_returns_false(self):
        client = _make_kis_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "output": [self._cancelable_row(psbl_qty="0")],
                "_tr_cont": "",
            }
        )

        assert await client.cancel_order("12345") is False
        assert client._request.await_count == 1

    @pytest.mark.asyncio
    async def test_cancel_order_kis_rejection_returns_false(self):
        client = _make_kis_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"output": [self._cancelable_row()], "_tr_cont": ""},
                KISResponseError(msg_cd="APBK1234", msg1="취소 불가", tr_id="TTTC0013U"),
            ]
        )

        # 업무 거부는 삼켜서 False 반환(호출측 reconcile에 위임)
        assert await client.cancel_order("12345") is False

    @pytest.mark.asyncio
    async def test_cancel_order_infra_error_propagates(self):
        client = _make_kis_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"output": [self._cancelable_row()], "_tr_cont": ""},
                RateLimitError("rate limited"),
            ]
        )

        # 인프라 예외는 전파(오취소 방지)
        with pytest.raises(RateLimitError):
            await client.cancel_order("12345")

    @pytest.mark.asyncio
    async def test_fetch_cancelable_orders_paginates(self):
        client = _make_kis_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {
                    "output": [self._cancelable_row(odno="A")],
                    "_tr_cont": "M",
                    "ctx_area_fk100": "FK",
                    "ctx_area_nk100": "NK",
                },
                {"output": [self._cancelable_row(odno="B")], "_tr_cont": ""},
            ]
        )

        rows = await client._fetch_cancelable_orders()
        assert [r.odno for r in rows] == ["A", "B"]
        # 2번째 호출에 연속조회 컨텍스트가 실렸는지
        second = client._request.call_args_list[1]
        assert second.kwargs["params"]["CTX_AREA_FK100"] == "FK"
        assert second.kwargs["params"]["CTX_AREA_NK100"] == "NK"
        assert second.kwargs["tr_cont"] == "N"

    @pytest.mark.asyncio
    async def test_order_result_fields(self):
        client = _make_kis_client()
        resp = _make_ok_response(output={"ODNO": "99999", "KRX_FWDG_ORD_ORGNO": "ORG", "ORD_TMD": "143000"})
        client._session.post = AsyncMock(return_value=resp)

        order = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        result = await client.place_order(order)
        assert result.order_id == "99999"
        assert result.status == OrderStatus.SUBMITTED
        assert result.symbol == "005930"
        assert result.quantity == 10


class TestKISClientGetBalance:
    @pytest.mark.asyncio
    async def test_balance_fields(self):
        client = _make_kis_client()
        # 1st GET = 잔고(TTTC8434R), 2nd GET = 실현손익(TTTC8494R).
        # 당일 매수만 한 날(thdt_buy_amt>0, thdt_sll_amt=0)이라도 daily_pnl은
        # 순매매현금흐름이 아니라 실현손익(rlzt_pfls)을 따라야 한다 — F-18 회귀 가드.
        balance_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [
                    {"pdno": "005930", "hldg_qty": "10", "pchs_avg_pric": "70000",
                     "prpr": "72000", "evlu_amt": "720000", "evlu_pfls_amt": "20000",
                     "pchs_amt": "700000", "evlu_pfls_rt": "2.86", "prdt_name": "삼성전자"},
                ],
                "output2": [
                    {"dnca_tot_amt": "50000000", "tot_evlu_amt": "50720000",
                     "nass_amt": "50720000", "thdt_buy_amt": "700000",
                     "thdt_sll_amt": "0", "pchs_amt_smtl_amt": "700000",
                     "evlu_amt_smtl_amt": "720000", "evlu_pfls_smtl_amt": "20000"},
                ],
            },
            headers={"tr_cont": ""},
        )
        rlzpl_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [],
                "output2": [{"rlzt_pfls": "0", "rlzt_erng_rt": "0"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(side_effect=[balance_resp, rlzpl_resp])

        bal = await client.get_balance()
        assert bal.cash == Decimal("50000000")
        assert bal.invested == Decimal("700000")
        assert bal.unrealized_pnl == Decimal("20000")
        # 매수만 한 날이라도 daily_pnl은 음수가 아니다(실현손익 0).
        assert bal.daily_pnl == Decimal(0)
        assert bal.daily_pnl >= Decimal(0)

    @pytest.mark.asyncio
    async def test_daily_pnl_uses_realized_pnl(self):
        """daily_pnl/daily_pnl_pct/realized_pnl이 실현손익 TR 값을 따른다."""
        client = _make_kis_client()
        balance_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [],
                "output2": [
                    {"dnca_tot_amt": "50000000", "tot_evlu_amt": "50000000",
                     "thdt_buy_amt": "0", "thdt_sll_amt": "0"},
                ],
            },
            headers={"tr_cont": ""},
        )
        rlzpl_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [],
                "output2": [{"rlzt_pfls": "150000", "rlzt_erng_rt": "1.5"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(side_effect=[balance_resp, rlzpl_resp])

        bal = await client.get_balance()
        assert bal.daily_pnl == Decimal("150000")
        assert bal.daily_pnl_pct == Decimal("1.5")
        assert bal.realized_pnl == Decimal("150000")

    @pytest.mark.asyncio
    async def test_positions_count_active_only(self):
        client = _make_kis_client()
        balance_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [
                    {"pdno": "005930", "hldg_qty": "10", "prpr": "72000"},
                    {"pdno": "000660", "hldg_qty": "0", "prpr": "185000"},
                ],
                "output2": [{"dnca_tot_amt": "50000000", "tot_evlu_amt": "50720000"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=balance_resp)

        bal = await client.get_balance()
        assert bal.positions_count == 1

    @pytest.mark.asyncio
    async def test_empty_balance(self):
        client = _make_kis_client()
        balance_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [], "output2": [],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=balance_resp)

        bal = await client.get_balance()
        assert bal.cash == Decimal(0)
        assert bal.positions_count == 0


class TestKISClientGetBuyableCash:
    """get_buyable_cash (TTTC8908R): 미수없는매수금액 조회."""

    @pytest.mark.asyncio
    async def test_returns_nrcvb_buy_amt(self):
        """정상 응답에서 nrcvb_buy_amt(미수없는매수금액)을 파싱한다."""
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output": {
                    "ord_psbl_cash": "25000000",        # 미수 포함 한도
                    "nrcvb_buy_amt": "10000000",        # 미수 없는 금액 (target)
                    "max_buy_amt": "25000000",
                    "max_buy_qty": "347",
                    "nrcvb_buy_qty": "138",
                },
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        buyable = await client.get_buyable_cash("005930", Decimal("72000"))
        assert buyable == Decimal("10000000")

    @pytest.mark.asyncio
    async def test_empty_symbol_returns_zero(self):
        """symbol이 비어있으면 네트워크 호출 없이 0 반환."""
        client = _make_kis_client()
        client._session.get = AsyncMock()

        buyable = await client.get_buyable_cash("", Decimal("72000"))
        assert buyable == Decimal(0)
        client._session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_price_returns_zero(self):
        """price가 0 이하이면 네트워크 호출 없이 0 반환."""
        client = _make_kis_client()
        client._session.get = AsyncMock()

        buyable = await client.get_buyable_cash("005930", Decimal(0))
        assert buyable == Decimal(0)
        client._session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_output_returns_zero(self):
        """output 필드 부재 시 0 반환 (방어적 처리)."""
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "0", "msg_cd": "0000", "msg1": "ok"},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        buyable = await client.get_buyable_cash("005930", Decimal("72000"))
        assert buyable == Decimal(0)


class TestKISClientFetchDailyRealizedPnl:
    """_fetch_daily_realized_pnl (TTTC8494R): 당일 실현손익/실현수익률 조회."""

    @pytest.mark.asyncio
    async def test_parses_realized_pnl(self):
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [],
                "output2": [{"rlzt_pfls": "-25000", "rlzt_erng_rt": "-0.8"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        pnl, rate = await client._fetch_daily_realized_pnl()
        assert pnl == Decimal("-25000")
        assert rate == Decimal("-0.8")

    @pytest.mark.asyncio
    async def test_empty_output2_returns_zero(self):
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                       "output1": [], "output2": []},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        pnl, rate = await client._fetch_daily_realized_pnl()
        assert pnl == Decimal(0)
        assert rate == Decimal(0)

    @pytest.mark.asyncio
    async def test_kis_error_falls_back_to_zero(self):
        """모의 미지원 등 KISResponseError 발생 시 (0, 0) 폴백."""
        client = _make_kis_client()
        client._session.get = AsyncMock(
            side_effect=KISResponseError(msg_cd="EGW00999", msg1="미지원", tr_id="VTTC8494R")
        )

        pnl, rate = await client._fetch_daily_realized_pnl()
        assert pnl == Decimal(0)
        assert rate == Decimal(0)

    @pytest.mark.asyncio
    async def test_real_uses_live_tr_id(self):
        client = _make_kis_client()
        client._is_paper = lambda: False
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                       "output1": [], "output2": [{"rlzt_pfls": "0", "rlzt_erng_rt": "0"}]},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        await client._fetch_daily_realized_pnl()
        # _do_request → build_headers(token, tr_id, tr_cont=...): tr_id는 두 번째 위치 인자.
        assert client._auth.build_headers.call_args.args[1] == "TTTC8494R"

    @pytest.mark.asyncio
    async def test_paper_uses_demo_tr_id(self):
        client = _make_kis_client()
        client._is_paper = lambda: True
        resp = mock_aiohttp_response(
            json_data={"rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                       "output1": [], "output2": [{"rlzt_pfls": "0", "rlzt_erng_rt": "0"}]},
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        await client._fetch_daily_realized_pnl()
        assert client._auth.build_headers.call_args.args[1] == "VTTC8494R"


class TestKISClientGetPositions:
    @pytest.mark.asyncio
    async def test_filters_zero_qty(self):
        client = _make_kis_client()
        balance_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [
                    {"pdno": "005930", "hldg_qty": "10", "prpr": "72000",
                     "pchs_avg_pric": "70000", "evlu_amt": "720000",
                     "evlu_pfls_amt": "20000", "evlu_pfls_rt": "2.86"},
                    {"pdno": "000660", "hldg_qty": "0", "prpr": "185000"},
                ],
                "output2": [{"dnca_tot_amt": "50000000"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=balance_resp)

        positions = await client.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "005930"

    @pytest.mark.asyncio
    async def test_empty(self):
        client = _make_kis_client()
        balance_resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [
                    {"pdno": "005930", "hldg_qty": "0"},
                ],
                "output2": [{"dnca_tot_amt": "50000000"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=balance_resp)

        positions = await client.get_positions()
        assert positions == []


class TestKISClientFetchBalancePages:
    @pytest.mark.asyncio
    async def test_single_page(self):
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [{"pdno": "005930", "hldg_qty": "10"}],
                "output2": [{"dnca_tot_amt": "50000000"}],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        positions, summary = await client._fetch_balance_pages()
        assert len(positions) == 1
        assert client._session.get.await_count == 1

    @pytest.mark.asyncio
    async def test_multi_page_m_cont(self):
        client = _make_kis_client()
        resp1 = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [{"pdno": "005930", "hldg_qty": "10"}],
                "output2": [{"dnca_tot_amt": "50000000"}],
                "ctx_area_fk100": "FK", "ctx_area_nk100": "NK",
            },
            headers={"tr_cont": "M"},
        )
        resp2 = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [{"pdno": "000660", "hldg_qty": "5"}],
                "output2": [],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(side_effect=[resp1, resp2])

        positions, summary = await client._fetch_balance_pages()
        assert len(positions) == 2
        assert client._session.get.await_count == 2

    @pytest.mark.asyncio
    async def test_context_keys_forwarded(self):
        client = _make_kis_client()
        resp1 = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [], "output2": [{"dnca_tot_amt": "0"}],
                "ctx_area_fk100": "FKVAL", "ctx_area_nk100": "NKVAL",
            },
            headers={"tr_cont": "M"},
        )
        resp2 = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [], "output2": [],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(side_effect=[resp1, resp2])

        await client._fetch_balance_pages()

        # Check second call's params include context keys
        second_call = client._session.get.call_args_list[1]
        params = second_call.kwargs.get("params") or second_call[1].get("params", {})
        assert params.get("CTX_AREA_FK100") == "FKVAL"
        assert params.get("CTX_AREA_NK100") == "NKVAL"

    @pytest.mark.asyncio
    async def test_no_output2_default(self):
        client = _make_kis_client()
        resp = mock_aiohttp_response(
            json_data={
                "rt_cd": "0", "msg_cd": "0000", "msg1": "ok",
                "output1": [],
            },
            headers={"tr_cont": ""},
        )
        client._session.get = AsyncMock(return_value=resp)

        positions, summary = await client._fetch_balance_pages()
        assert isinstance(summary, KISBalanceOutput2)
        assert summary.dnca_tot_amt == ""


# ═══════════════════════════════════════════════════════════════════════
# 2.3 InMemoryBroker
# ═══════════════════════════════════════════════════════════════════════


class TestInMemoryBrokerLifecycle:
    @pytest.mark.asyncio
    async def test_connect(self):
        broker = InMemoryBroker()
        await broker.connect()
        assert broker._connected is True

    @pytest.mark.asyncio
    async def test_disconnect(self):
        broker = InMemoryBroker()
        await broker.connect()
        await broker.disconnect()
        assert broker._connected is False

    @pytest.mark.asyncio
    async def test_defaults(self):
        broker = InMemoryBroker()
        assert len(broker._stocks) == 3
        assert len(broker._prices) == 3
        assert broker._cash == Decimal("100_000_000")
        assert len(broker._positions) == 0
        assert len(broker._orders) == 0


class TestInMemoryBrokerGetPrice:
    @pytest.mark.asyncio
    async def test_known_symbol(self):
        broker = InMemoryBroker()
        price = await broker.get_price("005930")
        assert price.current_price == Decimal("72000")

    @pytest.mark.asyncio
    async def test_unknown_raises(self):
        broker = InMemoryBroker()
        with pytest.raises(OrderError, match="No price data"):
            await broker.get_price("999999")


class TestInMemoryBrokerGetDailyOHLCV:
    @pytest.mark.asyncio
    async def test_stored_data(self):
        bars = [
            OHLCV(symbol="005930", date=date(2026, 1, i + 1),
                   open=Decimal("72000"), high=Decimal("73000"),
                   low=Decimal("71000"), close=Decimal("72500"), volume=1000)
            for i in range(10)
        ]
        broker = InMemoryBroker(ohlcv_data={"005930": bars})
        result = await broker.get_daily_ohlcv("005930", period_days=5)
        assert len(result) == 5

    @pytest.mark.asyncio
    async def test_synthetic_known_price(self):
        broker = InMemoryBroker()
        result = await broker.get_daily_ohlcv("005930", period_days=10)
        assert len(result) == 10
        assert result[0].close == Decimal("72000")

    @pytest.mark.asyncio
    async def test_synthetic_unknown_price(self):
        broker = InMemoryBroker()
        result = await broker.get_daily_ohlcv("999999", period_days=5)
        assert len(result) == 5
        assert result[0].close == Decimal("50000")


class TestInMemoryBrokerPlaceOrder:
    @pytest.mark.asyncio
    async def test_buy_market_new(self):
        broker = InMemoryBroker()
        order = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        result = await broker.place_order(order)

        assert result.status == OrderStatus.FILLED
        assert broker._cash == Decimal("100_000_000") - Decimal("72000") * 10
        assert "005930" in broker._positions

    @pytest.mark.asyncio
    async def test_buy_existing_position(self):
        broker = InMemoryBroker()
        order1 = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        await broker.place_order(order1)

        order2 = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        await broker.place_order(order2)

        pos = broker._positions["005930"]
        assert pos.quantity == 20
        assert pos.average_cost == Decimal("72000")  # same price, same avg

    @pytest.mark.asyncio
    async def test_buy_insufficient_funds(self):
        broker = InMemoryBroker(initial_cash=Decimal("100"))
        order = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        with pytest.raises(InsufficientFundsError):
            await broker.place_order(order)

    @pytest.mark.asyncio
    async def test_buy_limit(self):
        broker = InMemoryBroker()
        order = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=10, price=Decimal("71000"),
        )
        result = await broker.place_order(order)
        assert result.price == Decimal("71000")
        pos = broker._positions["005930"]
        assert pos.average_cost == Decimal("71000")

    @pytest.mark.asyncio
    async def test_sell_full_close(self):
        broker = InMemoryBroker()
        buy = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=10, price=Decimal("70000"),
        )
        await broker.place_order(buy)
        initial_cash = broker._cash

        sell = OrderRequest(
            symbol="005930", side=OrderSide.SELL,
            order_type=OrderType.MARKET, quantity=10,
        )
        result = await broker.place_order(sell)

        assert result.status == OrderStatus.FILLED
        pos = broker._positions["005930"]
        assert pos.status == PositionStatus.CLOSED
        assert broker._realized_pnl == (Decimal("72000") - Decimal("70000")) * 10
        assert broker._cash == initial_cash + Decimal("72000") * 10

    @pytest.mark.asyncio
    async def test_sell_partial(self):
        broker = InMemoryBroker()
        buy = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        await broker.place_order(buy)

        sell = OrderRequest(
            symbol="005930", side=OrderSide.SELL,
            order_type=OrderType.MARKET, quantity=5,
        )
        await broker.place_order(sell)

        pos = broker._positions["005930"]
        assert pos.status == PositionStatus.PARTIALLY_CLOSED
        assert pos.quantity == 5

    @pytest.mark.asyncio
    async def test_sell_no_position(self):
        broker = InMemoryBroker()
        order = OrderRequest(
            symbol="005930", side=OrderSide.SELL,
            order_type=OrderType.MARKET, quantity=10,
        )
        with pytest.raises(OrderError, match="No position"):
            await broker.place_order(order)

    @pytest.mark.asyncio
    async def test_sell_excess_qty(self):
        broker = InMemoryBroker()
        buy = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=5,
        )
        await broker.place_order(buy)

        sell = OrderRequest(
            symbol="005930", side=OrderSide.SELL,
            order_type=OrderType.MARKET, quantity=10,
        )
        with pytest.raises(OrderError, match="Insufficient quantity"):
            await broker.place_order(sell)


class TestInMemoryBrokerCancelOrder:
    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self):
        broker = InMemoryBroker()
        assert await broker.cancel_order("NO_SUCH_ID") is False

    @pytest.mark.asyncio
    async def test_cancel_filled(self):
        broker = InMemoryBroker()
        order = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=1,
        )
        result = await broker.place_order(order)
        assert await broker.cancel_order(result.order_id) is False


class TestInMemoryBrokerBalance:
    @pytest.mark.asyncio
    async def test_initial(self):
        broker = InMemoryBroker()
        bal = await broker.get_balance()
        assert bal.total_assets == Decimal("100_000_000")
        assert bal.cash == Decimal("100_000_000")
        assert bal.invested == Decimal(0)

    @pytest.mark.asyncio
    async def test_after_buy(self):
        broker = InMemoryBroker()
        order = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10,
        )
        await broker.place_order(order)

        bal = await broker.get_balance()
        assert bal.cash == Decimal("100_000_000") - Decimal("72000") * 10
        assert bal.invested == Decimal("72000") * 10

    @pytest.mark.asyncio
    async def test_realized_pnl(self):
        broker = InMemoryBroker()
        buy = OrderRequest(
            symbol="005930", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=10, price=Decimal("70000"),
        )
        await broker.place_order(buy)
        sell = OrderRequest(
            symbol="005930", side=OrderSide.SELL,
            order_type=OrderType.MARKET, quantity=10,
        )
        await broker.place_order(sell)

        bal = await broker.get_balance()
        assert bal.realized_pnl == (Decimal("72000") - Decimal("70000")) * 10
