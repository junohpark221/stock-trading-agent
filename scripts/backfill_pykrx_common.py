"""PRJ-03 단계 4 — pykrx 5년 백필 공용 로직 (순수 함수 + upsert + 체크포인트).

backfill_pykrx_5y.py(오케스트레이션)와 tests/test_backfill_pykrx.py(단위 테스트)가
공유한다. pykrx 임포트는 fetch_* 함수 내부로 지연 — 빌더/체크포인트/upsert는
네트워크 없이 테스트 가능.

핵심 정책 (plan.md 확정 + 07-16 사용자 확정 3건):
- 매도/매수 총량 분해 포함 — 종목당 flow 6콜(value/volume × 순매수/매도/매수, detail=True).
- KIS 행 불가침 — flow 테이블은 ON CONFLICT DO UPDATE ... WHERE source != 'kis',
  daily_ohlcv(source 컬럼 없음)는 ON CONFLICT DO NOTHING.
- 대금은 원(KRW) 그대로(×1e6 없음 — 그건 KIS 백만원 전용), 수량은 주 그대로.
- `etc`(기타 합계)는 파생하지 않고 NULL — KRX에 기타단체 축이 없어 KIS 정의
  (기타법인+기타단체)를 재현할 수 없고, etc=etc_corp 근사는 단계 5 크로스체크를 오염시킨다.

KRX 세션: pykrx 1.2.8이 요청마다 자동 갱신(auth.get_auth_session, 만료 300s 전
재로그인). 단 갱신 실패 시 익명 세션으로 조용히 강등되어 빈/캡 데이터가 나오므로,
krx_call()이 재시도 전 강제 재로그인 + 실패 시 KrxAuthError로 즉시 중단한다.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.models import InvestorFlowRecord, MarketInvestorFlowRecord

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SLEEP_SEC = 0.7  # KRX 스크래핑 매너 (probe_pykrx_coverage.py 동일)
_RETRY_BACKOFF_SEC = (5.0, 30.0, 120.0)

FLOW_SOURCE = "krx"

# asyncpg 바인드 파라미터 상한 32,767 — flow 행은 93파라미터(90 데이터 + symbol/date/
# source)라 500배치(46,500)가 프로토콜 에러. 300 × 93 = 27,900로 캡.
FLOW_BATCH = 300
MARKET_BATCH = 500
OHLCV_BATCH = 500

# ── 갱신 컬럼 (KIS provider와 동일하게 도메인 레코드에서 유도) ────────
# 시장 테이블은 KIS 레코드에 없는 pykrx 전용 지수 3컬럼을 백필이 직접 채운다.
FLOW_UPDATE_COLS: tuple[str, ...] = tuple(
    k for k in InvestorFlowRecord.model_fields if k not in ("symbol", "date")
)
MARKET_UPDATE_COLS: tuple[str, ...] = tuple(
    k for k in MarketInvestorFlowRecord.model_fields if k not in ("market", "date")
) + ("index_volume", "index_trading_value", "index_market_cap")

# ── KRX detail=True 12컬럼 → 모델 prefix 매핑 (investor_flow.py 독스트링 기준) ──
DIRECT_KRX_COLS: dict[str, str] = {
    "금융투자": "scrt",
    "보험": "insu",
    "투신": "ivtr",
    "사모": "pe_fund",
    "은행": "bank",
    "기타금융": "mrbn",
    "연기금": "fund",
    "기타법인": "etc_corp",
    "개인": "prsn",
}
ORGN_COMPONENTS: tuple[str, ...] = (
    "금융투자", "보험", "투신", "사모", "은행", "기타금융", "연기금",
)
FRGN_COMPONENTS: tuple[str, ...] = ("외국인", "기타외국인")  # 합산 = KIS frgn (게이트 ③ 100% 일치)
# KIS 전용 NULL 축: frgn_reg / frgn_nreg / etc / etc_orgt

ON_SLOT: dict[str, str] = {"순매수": "net", "매도": "sell", "매수": "buy"}
FLOW_ONS: tuple[str, ...] = ("순매수", "매도", "매수")

INDEX_TICKERS: dict[str, tuple[str, str]] = {
    "kospi": ("KOSPI", "1001"),
    "kosdaq": ("KOSDAQ", "2001"),
}


def yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


# ── 값 변환 (NaN-안전; 대금은 정수 원 단위 → Decimal) ─────────────────


def _is_missing(v: Any) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v))


def to_int(v: Any) -> int | None:
    if _is_missing(v):
        return None
    return int(round(float(v)))


def to_dec(v: Any) -> Decimal | None:
    """정수 원 단위 대금 — float64 표현을 벗겨 Decimal 정수로."""
    if _is_missing(v):
        return None
    return Decimal(int(round(float(v))))


def to_price(v: Any) -> Decimal | None:
    """가격/지수 — 소수부 보존 (Numeric(15,2))."""
    if _is_missing(v):
        return None
    if isinstance(v, float) and v.is_integer():
        return Decimal(int(v))
    return Decimal(str(v))


def to_rate(v: Any) -> Decimal | None:
    """등락률 — Numeric(10,4)."""
    if _is_missing(v):
        return None
    return Decimal(str(round(float(v), 4)))


def _as_date(ts: Any) -> date:
    return ts.date() if hasattr(ts, "date") else ts


# ── KRX 호출 래퍼 ─────────────────────────────────────────────────────


class KrxAuthError(RuntimeError):
    """KRX 재로그인 실패 — 익명 세션 강등으로 쓰레기 적재를 막기 위해 즉시 중단."""


def force_krx_relogin() -> None:
    """pykrx 전역 세션 강제 재로그인. 실패 시 KrxAuthError."""
    from pykrx.website.comm.auth import build_krx_session, set_auth_session

    with contextlib.redirect_stdout(sys.stderr):
        krx_session = build_krx_session()
    if krx_session is None:
        raise KrxAuthError("KRX 재로그인 실패 — KRX_ID/KRX_PW 환경변수를 확인하세요")
    set_auth_session(krx_session)


def krx_call(fn: Any, /, *args: Any, sleep_sec: float = SLEEP_SEC, retries: int = 3, **kwargs: Any):
    """페이싱 + 재시도(백오프 5/30/120s) + 재시도 전 강제 재로그인.

    pykrx의 로그인/경고 print가 stdout 리포트를 오염시키지 않도록 stderr로 돌린다.
    """
    last_exc: Exception | None = None
    for attempt in range(retries):
        time.sleep(sleep_sec)
        try:
            with contextlib.redirect_stdout(sys.stderr):
                return fn(*args, **kwargs)
        except KrxAuthError:
            raise
        except Exception as exc:  # noqa: BLE001 — KRX 스크래핑: 예외 유형 비정형
            last_exc = exc
            if attempt < retries - 1:
                print(
                    f"[krx_call] {getattr(fn, '__name__', fn)} 실패({type(exc).__name__}: {exc}) "
                    f"— {_RETRY_BACKOFF_SEC[attempt]:.0f}s 후 재로그인·재시도",
                    file=sys.stderr,
                )
                time.sleep(_RETRY_BACKOFF_SEC[attempt])
                force_krx_relogin()
    assert last_exc is not None
    raise last_exc


def _stock() -> Any:
    """pykrx.stock 지연 임포트 — 임포트 시점 자동 로그인 print도 stderr로."""
    with contextlib.redirect_stdout(sys.stderr):
        from pykrx import stock
    return stock


# ── 유니버스 (상폐 포함 point-in-time — 확정 15) ──────────────────────


def build_universe(
    start: date, end: date, *, freq_days: int = 30, sleep_sec: float = SLEEP_SEC
) -> tuple[list[str], list[tuple[str, int]]]:
    """월간 스냅샷 합집합 유니버스. (정렬 종목 리스트, 스냅샷별 (일자, 종목수)) 반환.

    한 스냅샷 간격(~30일) 안에 상장·상폐가 모두 일어난 종목은 누락 — 감수(리포트 명기).
    """
    stock = _stock()
    universe: set[str] = set()
    snapshots: list[tuple[str, int]] = []
    d = start
    dates = []
    while d <= end:
        dates.append(d)
        d += timedelta(days=freq_days)
    if dates[-1] != end:
        dates.append(end)
    for snap in dates:
        shifted = snap
        while shifted.weekday() >= 5:  # 주말 회피 (probe 동일)
            shifted += timedelta(days=1)
        tickers = krx_call(
            stock.get_market_ticker_list, yyyymmdd(shifted), market="ALL", sleep_sec=sleep_sec
        )
        snapshots.append((yyyymmdd(shifted), len(tickers)))
        universe.update(tickers)
    return sorted(universe), snapshots


# ── fetch (동기 — asyncio.to_thread 안에서 호출) ─────────────────────


def fetch_symbol_flow_frames(
    symbol: str, start: date, end: date, *, sleep_sec: float = SLEEP_SEC
) -> dict[tuple[str, str], pd.DataFrame]:
    """종목 수급 6프레임 — (value|volume, 순매수|매도|매수) → detail=True DataFrame."""
    stock = _stock()
    frames: dict[tuple[str, str], pd.DataFrame] = {}
    for kind, fn in (
        ("value", stock.get_market_trading_value_by_date),
        ("volume", stock.get_market_trading_volume_by_date),
    ):
        for on in FLOW_ONS:
            frames[(kind, on)] = krx_call(
                fn, yyyymmdd(start), yyyymmdd(end), symbol,
                on=on, detail=True, sleep_sec=sleep_sec,
            )
    return frames


def fetch_symbol_ohlcv_frames(
    symbol: str, start: date, end: date, *, sleep_sec: float = SLEEP_SEC
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(수정주가 OHLCV[네이버 fchart], 시가총액[KRX — 거래대금 조달용]) 프레임."""
    stock = _stock()
    df_ohlcv = krx_call(
        stock.get_market_ohlcv_by_date, yyyymmdd(start), yyyymmdd(end), symbol,
        sleep_sec=sleep_sec,
    )
    df_cap = krx_call(
        stock.get_market_cap_by_date, yyyymmdd(start), yyyymmdd(end), symbol,
        sleep_sec=sleep_sec,
    )
    return df_ohlcv, df_cap


def fetch_market_frames(
    market: str, start: date, end: date, *, sleep_sec: float = SLEEP_SEC
) -> dict[str, pd.DataFrame]:
    """시장 단위 3프레임 — value/volume(net, detail=True) + index(OHLCV, start-14d 선취)."""
    stock = _stock()
    mkt_ticker, index_code = INDEX_TICKERS[market]
    frames: dict[str, pd.DataFrame] = {}
    for kind, fn in (
        ("value", stock.get_market_trading_value_by_date),
        ("volume", stock.get_market_trading_volume_by_date),
    ):
        frames[kind] = krx_call(
            fn, yyyymmdd(start), yyyymmdd(end), mkt_ticker,
            on="순매수", detail=True, sleep_sec=sleep_sec,
        )
    # prev_close 파생을 위해 2주 선취 후 build_market_rows에서 start 미만을 절사
    frames["index"] = krx_call(
        stock.get_index_ohlcv_by_date,
        yyyymmdd(start - timedelta(days=14)), yyyymmdd(end), index_code,
        sleep_sec=sleep_sec,
    )
    return frames


# ── 행 빌더 (순수 — 단위 테스트 대상) ────────────────────────────────


def _extract_axes(srow: pd.Series) -> dict[str, float | None]:
    """detail=True 한 행 → prefix별 원시값. frgn/orgn은 구성 축 NaN-안전 합산."""
    out: dict[str, float | None] = {}
    for col, prefix in DIRECT_KRX_COLS.items():
        if col in srow.index:
            v = srow[col]
            out[prefix] = None if _is_missing(v) else float(v)
    for prefix, components in (("frgn", FRGN_COMPONENTS), ("orgn", ORGN_COMPONENTS)):
        present = [srow[c] for c in components if c in srow.index]
        if present:
            vals = [float(v) for v in present if not _is_missing(v)]
            out[prefix] = sum(vals) if vals else None
    return out


def _empty_flow_row(symbol: str, d: date) -> dict[str, Any]:
    """90 데이터 키 전부 None으로 초기화 — 멀티행 INSERT는 행 키가 동형이어야 한다."""
    row: dict[str, Any] = {"symbol": symbol, "date": d}
    row.update(dict.fromkeys(FLOW_UPDATE_COLS))
    return row


def build_flow_rows(
    symbol: str, frames: dict[tuple[str, str], pd.DataFrame]
) -> list[dict[str, Any]]:
    """6프레임 → investor_flow_daily 행 dict 리스트 (날짜 union, KIS 전용 축 None)."""
    rows: dict[date, dict[str, Any]] = {}
    for (kind, on), df in frames.items():
        if df is None or df.empty:
            continue
        suffix = "amt" if kind == "value" else "qty"
        conv = to_dec if kind == "value" else to_int
        slot = ON_SLOT[on]
        for ts, srow in df.iterrows():
            d = _as_date(ts)
            row = rows.setdefault(d, _empty_flow_row(symbol, d))
            for prefix, raw in _extract_axes(srow).items():
                row[f"{prefix}_{slot}_{suffix}"] = conv(raw)
    return [rows[d] for d in sorted(rows)]


def build_ohlcv_rows(
    symbol: str, df_ohlcv: pd.DataFrame, df_cap: pd.DataFrame
) -> list[dict[str, Any]]:
    """OHLCV(네이버) + 거래대금(KRX cap) 조인 → daily_ohlcv 행 dict 리스트.

    OHLC NaN 또는 종가<=0 행은 제외(거래정지 아티팩트). 거래량 0은 유지.
    """
    cap_by_date: dict[date, Decimal | None] = {}
    if df_cap is not None and not df_cap.empty and "거래대금" in df_cap.columns:
        for ts, srow in df_cap.iterrows():
            cap_by_date[_as_date(ts)] = to_dec(srow["거래대금"])

    rows: list[dict[str, Any]] = []
    if df_ohlcv is None or df_ohlcv.empty:
        return rows
    has_rate = "등락률" in df_ohlcv.columns
    for ts, srow in df_ohlcv.iterrows():
        d = _as_date(ts)
        o = to_price(srow.get("시가"))
        h = to_price(srow.get("고가"))
        lo = to_price(srow.get("저가"))
        c = to_price(srow.get("종가"))
        if o is None or h is None or lo is None or c is None or c <= 0:
            continue
        rows.append(
            {
                "symbol": symbol,
                "date": d,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": to_int(srow.get("거래량")) or 0,
                "trading_value": cap_by_date.get(d),
                "change_rate": to_rate(srow.get("등락률")) if has_rate else None,
            }
        )
    return rows


def _empty_market_row(market: str, d: date) -> dict[str, Any]:
    row: dict[str, Any] = {"market": market, "date": d}
    row.update(dict.fromkeys(MARKET_UPDATE_COLS))
    return row


def build_market_rows(
    market: str, frames: dict[str, pd.DataFrame], start: date
) -> list[dict[str, Any]]:
    """시장 3프레임 → market_investor_flow_daily 행 dict 리스트.

    index 프레임은 start-14d 선취분으로 prev_close/change/change_rate를 파생한 뒤
    start 미만을 절사한다. 수급은 net 축만(시장 테이블에 매도/매수 분해 없음).
    """
    rows: dict[date, dict[str, Any]] = {}

    df_idx = frames.get("index")
    if df_idx is not None and not df_idx.empty:
        prev_close: Decimal | None = None
        for ts, srow in df_idx.sort_index().iterrows():
            d = _as_date(ts)
            close = to_price(srow.get("종가"))
            if d >= start:
                row = rows.setdefault(d, _empty_market_row(market, d))
                row["index_open"] = to_price(srow.get("시가"))
                row["index_high"] = to_price(srow.get("고가"))
                row["index_low"] = to_price(srow.get("저가"))
                row["index_close"] = close
                row["index_volume"] = to_int(srow.get("거래량"))
                row["index_trading_value"] = to_dec(srow.get("거래대금"))
                row["index_market_cap"] = to_dec(srow.get("상장시가총액"))
                row["index_prev_close"] = prev_close
                if close is not None and prev_close is not None and prev_close != 0:
                    change = close - prev_close
                    row["index_change"] = change
                    row["index_change_rate"] = (change / prev_close * 100).quantize(
                        Decimal("0.0001")
                    )
            prev_close = close if close is not None else prev_close

    for kind in ("value", "volume"):
        df = frames.get(kind)
        if df is None or df.empty:
            continue
        suffix = "amt" if kind == "value" else "qty"
        conv = to_dec if kind == "value" else to_int
        for ts, srow in df.iterrows():
            d = _as_date(ts)
            if d < start:
                continue
            row = rows.setdefault(d, _empty_market_row(market, d))
            for prefix, raw in _extract_axes(srow).items():
                row[f"{prefix}_net_{suffix}"] = conv(raw)

    return [rows[d] for d in sorted(rows)]


# ── upsert (async — KIS 행 불가침) ────────────────────────────────────


async def upsert_rows(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    model: type,
    constraint: str,
    rows: list[dict[str, Any]],
    batch_size: int,
    mode: str,
    source: str | None = None,
    update_cols: tuple[str, ...] | None = None,
) -> int:
    """배치 upsert. mode:

    - "update_unless_kis": source 주입 + ON CONFLICT DO UPDATE ... WHERE 기존
      행의 source != 'kis' — KIS 증분 행 불가침, krx 행은 갭 메우기 재실행 갱신.
    - "do_nothing": ON CONFLICT DO NOTHING — daily_ohlcv(기존 KIS 수집분 보존).

    rowcount는 삽입 + (kis 보호로 스킵되지 않은) 갱신 수만 센다.
    """
    if not rows:
        return 0
    total = 0
    async with session_factory() as session:
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            if mode == "update_unless_kis":
                assert source is not None and update_cols is not None
                batch = [{**row, "source": source} for row in batch]
                stmt = pg_insert(model).values(batch)
                set_ = {k: stmt.excluded[k] for k in update_cols}
                set_["source"] = stmt.excluded.source
                set_["updated_at"] = func.now()
                stmt = stmt.on_conflict_do_update(
                    constraint=constraint,
                    set_=set_,
                    where=(model.__table__.c.source != "kis"),
                )
            elif mode == "do_nothing":
                stmt = pg_insert(model).values(batch)
                stmt = stmt.on_conflict_do_nothing(constraint=constraint)
            else:
                raise ValueError(f"unknown upsert mode: {mode}")
            result = await session.execute(stmt)
            total += result.rowcount
        await session.commit()
    return total


# ── 체크포인트 (종목 단위 재개) ───────────────────────────────────────


@dataclass
class Checkpoint:
    """백필 진행 상태 — 종목마다 원자적으로 저장(tmp + os.replace).

    크래시가 종목 처리 도중이면 그 종목은 done에 없어 재개 시 전체 재수집·재upsert
    되지만, upsert가 멱등이라 안전하다.
    """

    start: str  # ISO date — 창이 다른 체크포인트 오용 방지 가드
    end: str
    universe: list[str] = field(default_factory=list)
    done: dict[str, dict[str, Any]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    market_done: bool = False
    expected_trading_days: int | None = None
    version: int = 1

    def pending(self, *, retry_failed: bool = False) -> list[str]:
        return [
            s
            for s in self.universe
            if s not in self.done and (retry_failed or s not in self.failed)
        ]

    def mark_done(self, symbol: str, info: dict[str, Any]) -> None:
        self.done[symbol] = info
        self.failed.pop(symbol, None)

    def mark_failed(self, symbol: str, error: str) -> None:
        self.failed[symbol] = error


def load_checkpoint(path: Path) -> Checkpoint | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError(f"지원하지 않는 체크포인트 버전: {data.get('version')}")
    return Checkpoint(**data)


def save_checkpoint(path: Path, cp: Checkpoint) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(cp), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
