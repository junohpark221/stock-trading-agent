"""KIS 실시간 체결가 (H0STCNT0) WebSocket 클라이언트.

보유 종목의 실시간 체결가를 구독해 손절/트레일링 스톱을 초 단위로 트리거하기 위한
스트림. 체결통보(``KISExecutionStream``)와 동일한 접속/백오프 패턴을 따르되:

- TR ID는 ``H0STCNT0`` (실전/모의 공통).
- 페이로드는 **평문**(flag='0')이라 AES 복호화가 없다.
- 구독 키가 HTS ID가 아니라 **종목코드**이며, 보유 종목 변동에 맞춰 **동적 구독/해제**
  (``set_symbols``)를 지원한다. 접속당 KIS 등록 한도(~41종목) 내에서 운용한다.

상위 ``StopLossStreamService`` 가 단일 인스턴스를 소유하고 틱을 평가한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import aiohttp
import structlog
import websockets

from src.broker.kis.ws_codec import parse_price_payload, parse_subscription_response

if TYPE_CHECKING:
    from src.broker.kis.ws_codec import PriceTick
    from src.config import Settings

logger = structlog.get_logger(__name__)


PriceCallback = Callable[["PriceTick"], Awaitable[None]]

_TR_ID = "H0STCNT0"  # 국내주식 실시간체결가 (실전/모의 공통)


class KISPriceStream:
    """KIS 실시간 체결가(H0STCNT0) WebSocket 구독.

    Parameters
    ----------
    account_id: WS approval 발급에 쓰는 대표 계정 ID (로깅/라우팅용)
    app_key / app_secret: KIS OpenAPI 인증 정보 (approval_key 발급)
    is_paper: 실전/모의 여부 (URL 결정; TR ID는 공통)
    on_tick: 체결가 틱 콜백 (async)
    settings: Settings (URL, 백오프 설정)
    """

    def __init__(
        self,
        *,
        account_id: str,
        app_key: str,
        app_secret: str,
        is_paper: bool,
        on_tick: PriceCallback,
        settings: Settings,
    ) -> None:
        self._account_id = account_id
        self._app_key = app_key
        self._app_secret = app_secret
        self._is_paper = is_paper
        self._on_tick = on_tick
        self._settings = settings

        self._ws_url = settings.KIS_WS_URL_PAPER if is_paper else settings.KIS_WS_URL_LIVE
        self._approval_url = (
            settings.KIS_WS_APPROVAL_URL_PAPER if is_paper else settings.KIS_WS_APPROVAL_URL_LIVE
        )

        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

        # 동적 구독 상태 — desired(원하는 셋) vs subscribed(현재 전송한 셋)
        self._lock = asyncio.Lock()
        self._desired: set[str] = set()
        self._subscribed: set[str] = set()
        self._ws: object | None = None
        self._approval_key: str = ""

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Kick off the long-running recv loop as a background task."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        """Signal the recv loop to stop and wait for it to finish."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    # ── Dynamic subscription ──────────────────────────────────────────

    async def set_symbols(self, symbols: set[str]) -> None:
        """구독 종목 집합을 갱신. 접속 중이면 즉시 diff를 전송한다.

        미접속 시 desired만 갱신하고 다음 접속에서 일괄 구독한다.
        """
        async with self._lock:
            self._desired = set(symbols)
            ws = self._ws
            if ws is None:
                return
            to_add = self._desired - self._subscribed
            to_remove = self._subscribed - self._desired
            for sym in to_add:
                if await self._send_sub(ws, sym, subscribe=True):
                    self._subscribed.add(sym)
            for sym in to_remove:
                if await self._send_sub(ws, sym, subscribe=False):
                    self._subscribed.discard(sym)

    async def _send_sub(self, ws: object, symbol: str, *, subscribe: bool) -> bool:
        """단일 종목 구독('1')/해제('0') 프레임 전송. 성공 여부 반환."""
        msg = json.dumps({
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1" if subscribe else "0",
                "content-type": "utf-8",
            },
            "body": {
                "input": {"tr_id": _TR_ID, "tr_key": symbol},
            },
        })
        try:
            await ws.send(msg)  # type: ignore[attr-defined]
            return True
        except Exception:
            logger.warning(
                "kis_price_ws.send_sub_failed",
                account_id=self._account_id, symbol=symbol, subscribe=subscribe,
            )
            return False

    # ── Recv loop ─────────────────────────────────────────────────────

    async def _run_forever(self) -> None:
        """Connect + subscribe + recv with exponential backoff on failure."""
        backoff = 1.0
        backoff_max = float(self._settings.WS_RECONNECT_BACKOFF_MAX_SEC)
        while not self._stop_event.is_set():
            try:
                await self._run_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "kis_price_ws.recv_loop_error",
                    account_id=self._account_id, error=str(exc), backoff_sec=backoff,
                )
            if self._stop_event.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            backoff = min(backoff * 2, backoff_max)

    async def _run_once(self) -> None:
        """Single connect + (re)subscribe desired set + consume cycle."""
        approval_key = await self._fetch_approval_key()

        async with websockets.connect(self._ws_url, ping_interval=None) as ws:
            logger.info(
                "kis_price_ws.connecting",
                account_id=self._account_id, url=self._ws_url,
            )
            async with self._lock:
                self._ws = ws
                self._approval_key = approval_key
                self._subscribed = set()
                # 재접속 시 desired 전체를 일괄 재구독.
                for sym in self._desired:
                    if await self._send_sub(ws, sym, subscribe=True):
                        self._subscribed.add(sym)
            logger.info(
                "kis_price_ws.subscribed",
                account_id=self._account_id, symbols=len(self._subscribed),
            )

            try:
                while not self._stop_event.is_set():
                    try:
                        raw = await ws.recv()
                    except websockets.ConnectionClosed:
                        logger.info(
                            "kis_price_ws.connection_closed",
                            account_id=self._account_id,
                        )
                        return
                    await self._dispatch(raw, ws)
            finally:
                async with self._lock:
                    self._ws = None
                    self._subscribed = set()

    async def _fetch_approval_key(self) -> str:
        """POST /oauth2/Approval → approval_key (WS 전용)."""
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "secretkey": self._app_secret,
        }
        async with aiohttp.ClientSession() as session, session.post(
            self._approval_url,
            json=body,
            headers={"content-type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
        key = data.get("approval_key", "")
        if not key:
            raise RuntimeError(f"KIS WS approval_key empty: {data}")
        return key

    async def _dispatch(self, raw: str | bytes, ws: object) -> None:
        """Route one WS frame: realtime price, subscribe-ack, or PINGPONG."""
        if isinstance(raw, bytes):
            return

        if raw.startswith("0") or raw.startswith("1"):
            await self._handle_realtime(raw)
            return

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "kis_price_ws.unknown_frame",
                account_id=self._account_id, sample=raw[:120],
            )
            return

        header = payload.get("header", {}) or {}
        if header.get("tr_id", "") == "PINGPONG":
            try:
                await ws.pong(raw)  # type: ignore[attr-defined]
            except Exception:
                logger.debug("kis_price_ws.pong_failed", account_id=self._account_id)
            return

        parsed = parse_subscription_response(payload)
        if parsed.get("rt_cd") not in ("", "0"):
            logger.warning(
                "kis_price_ws.subscribe_error",
                account_id=self._account_id,
                msg_cd=parsed.get("msg_cd"), msg1=parsed.get("msg1"),
            )

    async def _handle_realtime(self, raw: str) -> None:
        """Parse `0|H0STCNT0|<count>|payload` 평문 프레임 → PriceTick 콜백."""
        parts = raw.split("|", 3)
        if len(parts) < 4:
            return
        _flag, trid, count_str, payload = parts
        if trid != _TR_ID:
            return

        try:
            count = int(count_str)
        except (TypeError, ValueError):
            count = 1

        tick = parse_price_payload(payload, count=count)
        if tick is None:
            return
        try:
            await self._on_tick(tick)
        except Exception:
            logger.exception(
                "kis_price_ws.on_tick_failed",
                account_id=self._account_id, symbol=tick.symbol,
            )


__all__ = ["KISPriceStream", "PriceCallback"]
