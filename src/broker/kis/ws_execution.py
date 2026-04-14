"""KIS 실시간 체결통보 (H0STCNI0 / H0STCNI9) WebSocket 클라이언트.

계정 한 개에 대한 WS 구독을 관리한다. 책임:
- ``POST /oauth2/Approval`` 로 WS approval_key 발급
- WS connect → 구독 요청 (tr_id, tr_key=HTS ID) → AES key/iv 수신
- 페이로드(^-구분) 복호화 후 ExecutionEvent 생성, 콜백으로 전달
- PINGPONG keepalive 처리
- 단절 시 지수 백오프로 자동 재연결

상위 `ExecutionStreamManager`가 여러 계정에 대해 인스턴스를 관리한다.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Awaitable, Callable

import aiohttp
import structlog
import websockets

from src.broker.kis.ws_codec import (
    aes_cbc_base64_decrypt,
    parse_execution_payload,
    parse_subscription_response,
)

if TYPE_CHECKING:
    from src.config import Settings
    from src.core.models import ExecutionEvent

logger = structlog.get_logger(__name__)


ExecutionCallback = Callable[["ExecutionEvent"], Awaitable[None]]


class KISExecutionStream:
    """Per-account KIS 체결통보 WebSocket subscription.

    Parameters
    ----------
    account_id: 내부 계정 ID (로깅/라우팅용)
    app_key / app_secret: KIS OpenAPI 인증 정보
    hts_id: 체결통보 tr_key 로 사용되는 사용자 HTS ID
    is_paper: 실전/모의 여부 (URL 및 TR ID 결정)
    on_event: 체결/거부/취소 이벤트 콜백 (async)
    settings: Settings (URL, 백오프 설정)
    """

    def __init__(
        self,
        *,
        account_id: str,
        app_key: str,
        app_secret: str,
        hts_id: str,
        is_paper: bool,
        on_event: ExecutionCallback,
        settings: Settings,
    ) -> None:
        self._account_id = account_id
        self._app_key = app_key
        self._app_secret = app_secret
        self._hts_id = hts_id
        self._is_paper = is_paper
        self._on_event = on_event
        self._settings = settings

        self._tr_id = "H0STCNI9" if is_paper else "H0STCNI0"
        self._ws_url = settings.KIS_WS_URL_PAPER if is_paper else settings.KIS_WS_URL_LIVE
        self._approval_url = (
            settings.KIS_WS_APPROVAL_URL_PAPER if is_paper else settings.KIS_WS_APPROVAL_URL_LIVE
        )

        self._aes_key: str = ""
        self._aes_iv: str = ""
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

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
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # ── Approval key ──────────────────────────────────────────────────

    async def _fetch_approval_key(self) -> str:
        """POST /oauth2/Approval → approval_key (WS 전용)."""
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "secretkey": self._app_secret,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._approval_url,
                json=body,
                headers={"content-type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
        key = data.get("approval_key", "")
        if not key:
            raise RuntimeError(
                f"KIS WS approval_key empty: {data}",
            )
        return key

    # ── Recv loop ─────────────────────────────────────────────────────

    async def _run_forever(self) -> None:
        """Connect + subscribe + recv with exponential backoff on failure."""
        backoff = 1.0
        backoff_max = float(self._settings.WS_RECONNECT_BACKOFF_MAX_SEC)
        while not self._stop_event.is_set():
            try:
                await self._run_once()
                backoff = 1.0  # reset after clean disconnect
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "kis_ws.recv_loop_error",
                    account_id=self._account_id,
                    error=str(exc),
                    backoff_sec=backoff,
                )
            if self._stop_event.is_set():
                break
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, backoff_max)

    async def _run_once(self) -> None:
        """Single connect + subscribe + consume cycle (returns on disconnect)."""
        approval_key = await self._fetch_approval_key()
        subscribe = json.dumps({
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {
                "input": {"tr_id": self._tr_id, "tr_key": self._hts_id},
            },
        })

        logger.info(
            "kis_ws.connecting",
            account_id=self._account_id,
            tr_id=self._tr_id,
            url=self._ws_url,
        )

        async with websockets.connect(
            self._ws_url, ping_interval=None,
        ) as ws:
            await ws.send(subscribe)
            logger.info(
                "kis_ws.subscribed",
                account_id=self._account_id, tr_id=self._tr_id,
            )

            while not self._stop_event.is_set():
                try:
                    raw = await ws.recv()
                except websockets.ConnectionClosed:
                    logger.info(
                        "kis_ws.connection_closed",
                        account_id=self._account_id,
                    )
                    return

                await self._dispatch(raw, ws)

    async def _dispatch(self, raw: str | bytes, ws: object) -> None:
        """Route one WS frame: realtime payload, subscribe-ack, or PINGPONG."""
        if isinstance(raw, bytes):
            # 체결통보 페이로드는 text; bytes 프레임은 무시
            return

        if raw.startswith("0") or raw.startswith("1"):
            # 실시간 데이터 프레임
            await self._handle_realtime(raw)
            return

        # JSON 제어 프레임 (subscribe-ack 또는 PINGPONG)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "kis_ws.unknown_frame",
                account_id=self._account_id, sample=raw[:120],
            )
            return

        header = payload.get("header", {}) or {}
        tr_id = header.get("tr_id", "")

        if tr_id == "PINGPONG":
            try:
                await ws.pong(raw)  # type: ignore[attr-defined]
            except Exception:
                logger.debug("kis_ws.pong_failed", account_id=self._account_id)
            return

        parsed = parse_subscription_response(payload)
        if parsed.get("rt_cd") == "0" and parsed.get("key") and parsed.get("iv"):
            self._aes_key = parsed["key"]
            self._aes_iv = parsed["iv"]
            logger.info(
                "kis_ws.aes_keys_received",
                account_id=self._account_id, tr_id=parsed.get("tr_id"),
            )
        elif parsed.get("rt_cd") not in ("", "0"):
            logger.warning(
                "kis_ws.subscribe_error",
                account_id=self._account_id,
                msg_cd=parsed.get("msg_cd"), msg1=parsed.get("msg1"),
            )

    async def _handle_realtime(self, raw: str) -> None:
        """Parse `0|TRID|...` or `1|TRID|cipher` framed payload."""
        parts = raw.split("|", 3)
        if len(parts) < 4:
            return
        _flag, trid, _count_or_, cipher_or_plain = parts

        if trid not in ("H0STCNI0", "H0STCNI9"):
            return

        # 체결통보는 flag='1' (암호화). 평문 경로('0')는 현 스트림에서 사용하지 않음.
        if not self._aes_key or not self._aes_iv:
            logger.warning(
                "kis_ws.missing_aes_keys",
                account_id=self._account_id,
            )
            return

        try:
            plaintext = aes_cbc_base64_decrypt(
                self._aes_key, self._aes_iv, cipher_or_plain,
            )
        except Exception as exc:
            logger.exception(
                "kis_ws.aes_decrypt_failed",
                account_id=self._account_id, error=str(exc),
            )
            return

        event = parse_execution_payload(plaintext, account_id=self._account_id)
        if event is None:
            logger.debug(
                "kis_ws.unparseable_payload",
                account_id=self._account_id, sample=plaintext[:120],
            )
            return

        try:
            await self._on_event(event)
        except Exception:
            logger.exception(
                "kis_ws.on_event_failed",
                account_id=self._account_id,
                broker_order_id=event.broker_order_id,
            )


__all__ = ["KISExecutionStream", "ExecutionCallback"]
