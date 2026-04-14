"""KIS WebSocket codec — AES256 decryption + 체결통보 페이로드 파서.

KIS 실시간 체결통보 (H0STCNI0 실전 / H0STCNI9 모의) 페이로드는 subscribe
응답 본문에서 전달된 AES256 key/iv 로 CBC+PKCS7 복호화된다. 복호화 결과는
`^` 구분 문자열이며, 인덱스 기반으로 필드를 해석한다.

레퍼런스: ``/Users/oliver.p/Desktop/Personal/open-trading-api/legacy/websocket/python/ws_domestic_stock.py``
"""

from __future__ import annotations

from base64 import b64decode
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from src.core.enums import OrderSide

if TYPE_CHECKING:
    from src.core.models import ExecutionEvent


# ── AES256 ────────────────────────────────────────────────────────────


def aes_cbc_base64_decrypt(key: str, iv: str, cipher_text_b64: str) -> str:
    """Decrypt AES256-CBC + PKCS7 padded base64 payload → plaintext string."""
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
    raw = unpad(cipher.decrypt(b64decode(cipher_text_b64)), AES.block_size)
    return raw.decode("utf-8")


# ── Subscription response ─────────────────────────────────────────────


def parse_subscription_response(payload: dict) -> dict:
    """Extract {rt_cd, msg1, tr_id, key, iv} from a WS subscribe response.

    Returns empty key/iv if the response is not a 체결통보 registration.
    """
    header = payload.get("header", {}) or {}
    body = payload.get("body", {}) or {}
    output = body.get("output") or {}
    return {
        "tr_id": header.get("tr_id", ""),
        "tr_key": header.get("tr_key", ""),
        "rt_cd": body.get("rt_cd", ""),
        "msg_cd": body.get("msg_cd", ""),
        "msg1": body.get("msg1", ""),
        "key": output.get("key", ""),
        "iv": output.get("iv", ""),
    }


# ── Execution payload ─────────────────────────────────────────────────


# 체결통보 ^-split 필드 인덱스 — ws_domestic_stock.py의 menulist 기준
_IDX_ACCOUNT_NO = 1
_IDX_ODNO = 2
_IDX_ORGN_ODNO = 3
_IDX_SIDE = 4               # 01=매도, 02=매수
_IDX_SYMBOL = 8
_IDX_FILL_QTY = 9           # (체결통보일 때) 체결수량, (접수 통보일 때) 주문수량
_IDX_FILL_PRICE = 10        # (체결통보일 때) 체결단가, (접수 통보일 때) 주문가격
_IDX_TIME = 11              # 주식체결시간 (HHMMSS)
_IDX_REJECTED = 12          # 거부여부 Y/N
_IDX_IS_FILLED = 13         # 체결여부: '2'=체결, 그 외=접수/정정/취소/거부

_MIN_FIELDS = 15


def _to_decimal(raw: str) -> Decimal:
    if not raw or not raw.strip():
        return Decimal(0)
    try:
        return Decimal(raw.strip())
    except InvalidOperation:
        return Decimal(0)


def _to_int(raw: str) -> int:
    if not raw or not raw.strip():
        return 0
    try:
        return int(raw.strip())
    except ValueError:
        return 0


def _parse_time_hhmmss(raw: str) -> datetime:
    """HHMMSS → datetime(today UTC). 장중 타임스탬프는 KST지만 저장은 UTC로 통일."""
    now = datetime.now(UTC)
    raw = (raw or "").strip()
    if len(raw) != 6 or not raw.isdigit():
        return now
    try:
        hour = int(raw[0:2])
        minute = int(raw[2:4])
        second = int(raw[4:6])
        return now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    except ValueError:
        return now


def parse_execution_payload(
    plaintext: str, *, account_id: str
) -> ExecutionEvent | None:
    """Parse decrypted `^`-split 체결통보 payload into ExecutionEvent.

    Returns None if payload is malformed or missing required fields.
    """
    from src.core.models import ExecutionEvent  # avoid circular

    fields = plaintext.split("^")
    if len(fields) < _MIN_FIELDS:
        return None

    odno = fields[_IDX_ODNO].strip()
    if not odno:
        return None

    is_filled = fields[_IDX_IS_FILLED].strip() == "2"
    rejected_flag = fields[_IDX_REJECTED].strip().upper() == "Y"

    side_code = fields[_IDX_SIDE].strip()
    side = OrderSide.SELL if side_code == "01" else OrderSide.BUY

    filled_qty = _to_int(fields[_IDX_FILL_QTY]) if is_filled else 0
    filled_price = _to_decimal(fields[_IDX_FILL_PRICE]) if is_filled else Decimal(0)

    return ExecutionEvent(
        account_id=account_id,
        broker_order_id=odno,
        orig_broker_order_id=fields[_IDX_ORGN_ODNO].strip(),
        symbol=fields[_IDX_SYMBOL].strip(),
        side=side,
        filled_quantity=filled_qty,
        filled_price=filled_price,
        is_filled=is_filled,
        is_rejected=rejected_flag,
        rejected_reason="KIS 거부" if rejected_flag else "",
        timestamp=_parse_time_hhmmss(fields[_IDX_TIME]),
    )


__all__ = [
    "aes_cbc_base64_decrypt",
    "parse_subscription_response",
    "parse_execution_payload",
]
