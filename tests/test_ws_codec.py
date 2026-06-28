"""Tests for KIS WebSocket codec (AES256 + 체결통보 페이로드 파서)."""

from __future__ import annotations

import base64
from decimal import Decimal

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from src.broker.kis.ws_codec import (
    aes_cbc_base64_decrypt,
    parse_execution_payload,
    parse_price_payload,
    parse_subscription_response,
)
from src.core.enums import OrderSide


# ── AES256 ────────────────────────────────────────────────────────────


def _aes_encrypt(key: str, iv: str, plaintext: str) -> str:
    cipher = AES.new(key.encode(), AES.MODE_CBC, iv.encode())
    ct = cipher.encrypt(pad(plaintext.encode(), AES.block_size))
    return base64.b64encode(ct).decode()


def test_aes_cbc_roundtrip_ascii():
    key = "0123456789abcdef0123456789abcdef"  # 32 bytes
    iv = "0123456789abcdef"
    plaintext = "hello kis webhook payload"

    cipher_text = _aes_encrypt(key, iv, plaintext)
    decoded = aes_cbc_base64_decrypt(key, iv, cipher_text)

    assert decoded == plaintext


def test_aes_cbc_roundtrip_unicode():
    key = "abcdefghijklmnopqrstuvwxyz123456"
    iv = "ivexampleivxmpl0"
    plaintext = "^".join([
        "HTSID", "5012345601", "0000001234", "", "02", "", "", "", "005930",
        "10", "72000", "093045", "N", "2", "N",
    ])

    cipher_text = _aes_encrypt(key, iv, plaintext)
    decoded = aes_cbc_base64_decrypt(key, iv, cipher_text)

    assert decoded == plaintext


# ── Subscription response ────────────────────────────────────────────


def test_parse_subscription_response_with_keys():
    payload = {
        "header": {"tr_id": "H0STCNI9", "tr_key": "TESTHTS"},
        "body": {
            "rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUCCESS",
            "output": {"key": "aes_key_32bytes_xxxxxxxxxxxxxxxx", "iv": "iv_16bytes_xxxxx"},
        },
    }
    parsed = parse_subscription_response(payload)
    assert parsed["rt_cd"] == "0"
    assert parsed["tr_id"] == "H0STCNI9"
    assert parsed["key"] == "aes_key_32bytes_xxxxxxxxxxxxxxxx"
    assert parsed["iv"] == "iv_16bytes_xxxxx"


def test_parse_subscription_response_error():
    payload = {
        "header": {"tr_id": "H0STCNI0"},
        "body": {"rt_cd": "1", "msg_cd": "OPSP0001", "msg1": "FAIL"},
    }
    parsed = parse_subscription_response(payload)
    assert parsed["rt_cd"] == "1"
    assert parsed["key"] == ""
    assert parsed["iv"] == ""


# ── Execution payload parser ─────────────────────────────────────────


def _make_payload(
    *,
    is_filled: bool,
    is_rejected: bool = False,
    side_code: str = "02",  # 매수
    odno: str = "0000001234",
    symbol: str = "005930",
    qty: int = 10,
    price: int = 72000,
) -> str:
    """Build a `^`-split 체결통보 plaintext per ws_domestic_stock 레퍼런스."""
    fields = [
        "HTSID",              # 0 고객ID
        "5012345601",         # 1 계좌번호
        odno,                 # 2 주문번호
        "",                   # 3 원주문번호
        side_code,            # 4 매도매수구분
        "",                   # 5 정정구분
        "",                   # 6 주문종류
        "",                   # 7 주문조건
        symbol,               # 8 주식단축종목코드
        str(qty),             # 9 (체결 시) 체결수량 / (접수 시) 주문수량
        str(price),           # 10 (체결 시) 체결단가 / (접수 시) 주문가격
        "093045",             # 11 주식체결시간
        "Y" if is_rejected else "N",  # 12 거부여부
        "2" if is_filled else "1",    # 13 체결여부
        "N",                   # 14 접수여부
    ]
    return "^".join(fields)


def test_parse_execution_payload_filled_buy():
    plaintext = _make_payload(is_filled=True, side_code="02")
    event = parse_execution_payload(plaintext, account_id="default")

    assert event is not None
    assert event.broker_order_id == "0000001234"
    assert event.symbol == "005930"
    assert event.side == OrderSide.BUY
    assert event.filled_quantity == 10
    assert event.filled_price == Decimal("72000")
    assert event.is_filled is True
    assert event.is_rejected is False
    assert event.account_id == "default"


def test_parse_execution_payload_filled_sell():
    plaintext = _make_payload(is_filled=True, side_code="01")
    event = parse_execution_payload(plaintext, account_id="acct-1")

    assert event is not None
    assert event.side == OrderSide.SELL
    assert event.is_filled is True


def test_parse_execution_payload_acceptance_not_filled():
    plaintext = _make_payload(is_filled=False, side_code="02")
    event = parse_execution_payload(plaintext, account_id="default")

    assert event is not None
    assert event.is_filled is False
    assert event.is_rejected is False
    # 접수 통보에서는 filled_quantity/price가 0 (주문 수량/가격 무시)
    assert event.filled_quantity == 0
    assert event.filled_price == Decimal(0)


def test_parse_execution_payload_rejected():
    plaintext = _make_payload(is_filled=False, is_rejected=True)
    event = parse_execution_payload(plaintext, account_id="default")

    assert event is not None
    assert event.is_rejected is True
    assert event.rejected_reason == "KIS 거부"


def test_parse_execution_payload_malformed_returns_none():
    # 필드 수 부족
    assert parse_execution_payload("^".join(["a", "b", "c"]), account_id="default") is None


def test_parse_execution_payload_missing_odno_returns_none():
    fields = ["HTSID", "5012345601", "", "", "02", "", "", "", "005930",
              "10", "72000", "093045", "N", "2", "N"]
    assert parse_execution_payload("^".join(fields), account_id="default") is None


# ── Realtime price payload (H0STCNT0) ────────────────────────────────


def _price_record(
    symbol: str = "005930", *, time: str = "093045", price: str = "71000",
) -> list[str]:
    """46-field H0STCNT0 record: [0]종목 [1]체결시간 [2]현재가 + 43 padding."""
    rec = [symbol, time, price]
    rec.extend(str(i) for i in range(46 - len(rec)))  # 나머지 43 필드 패딩
    return rec


def test_parse_price_payload_single_record():
    payload = "^".join(_price_record(price="71000"))
    tick = parse_price_payload(payload, count=1)

    assert tick is not None
    assert tick.symbol == "005930"
    assert tick.price == Decimal("71000")
    assert tick.time == "093045"


def test_parse_price_payload_multi_record_uses_latest():
    rec1 = _price_record(time="093045", price="71000")
    rec2 = _price_record(time="093050", price="70500")
    payload = "^".join(rec1 + rec2)
    tick = parse_price_payload(payload, count=2)

    assert tick is not None
    assert tick.price == Decimal("70500")  # 최신 레코드
    assert tick.time == "093050"


def test_parse_price_payload_decimal_precision():
    payload = "^".join(_price_record(price="71050"))
    tick = parse_price_payload(payload, count=1)
    assert isinstance(tick.price, Decimal)
    assert tick.price == Decimal("71050")


def test_parse_price_payload_zero_price_returns_none():
    payload = "^".join(_price_record(price="0"))
    assert parse_price_payload(payload, count=1) is None


def test_parse_price_payload_empty_symbol_returns_none():
    payload = "^".join(_price_record(symbol="", price="71000"))
    assert parse_price_payload(payload, count=1) is None


def test_parse_price_payload_malformed_returns_none():
    assert parse_price_payload("005930^093045", count=1) is None
