"""PRJ-03 착수 게이트 ② — pykrx 백필 커버리지 검증 프로브.

검증 대상:
1. 종목별 투자자 플로우(get_market_trading_value/volume_by_date)의 최고(最古) 시점 — 목표 5년
2. 반환 컬럼 인벤토리(detail=True 세부주체 분해) — KIS 필드 매핑표 원자료(확정 11 NULL 축)
3. OHLCV·벤치마크 지수(KOSPI 1001/KOSDAQ 2001) 5년 커버리지(확정 13)
4. 상장폐지 종목 커버리지 — 과거 상장 목록·상폐 종목 플로우/OHLCV 조회 가용성(확정 15)

pykrx는 동기(KRX 정보데이터시스템 스크래핑) — KIS/Redis 불요, 호출 간 슬립으로 매너 유지.

실행 (운영 EC2, 사용자 SSH):
    cd /home/ec2-user/stock-trading-agent
    docker compose -f docker-compose.prod.yml exec app \
        python scripts/probe_pykrx_coverage.py > probe2_pykrx.md
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta

from probe_common import kv, report_error, report_header, section, yyyymmdd

SLEEP_SEC = 0.7  # KRX 스크래핑 매너

# 장기 상장 샘플: KOSPI 삼성전자 / KOSDAQ 에스엠(041510, 2000년 상장)
SAMPLE_KOSPI = "005930"
SAMPLE_KOSDAQ = "041510"


def _progress(msg: str) -> None:
    print(f"[진행] {msg}", file=sys.stderr)


def _sleep() -> None:
    time.sleep(SLEEP_SEC)


def _try_window(fn, ticker: str, start: date, end: date, **kwargs):
    """지정 창 조회 — (df 또는 None, 오류 또는 None)."""
    try:
        _sleep()
        df = fn(yyyymmdd(start), yyyymmdd(end), ticker, **kwargs)
        return df, None
    except Exception as exc:  # noqa: BLE001 — 프로브: 오류도 실측 결과
        return None, exc


def probe_oldest(stock, ticker: str) -> None:
    """5/7/10/15년 전 2주 창을 단계 탐색해 데이터 존재 최고 시점을 판정."""
    section(f"1. 투자자 플로우 최고(最古) 시점 — {ticker}")
    today = date.today()
    deepest_with_data = None
    for years in (5, 7, 10, 15):
        start = today - timedelta(days=years * 365)
        end = start + timedelta(days=14)
        _progress(f"{ticker} trading_value {years}년 전 창 {yyyymmdd(start)}~{yyyymmdd(end)}")
        df, err = _try_window(stock.get_market_trading_value_by_date, ticker, start, end)
        if err is not None:
            kv(f"{years}년 전 ({yyyymmdd(start)}~)", f"오류 `{type(err).__name__}: {err}`")
            continue
        n = 0 if df is None else len(df)
        kv(f"{years}년 전 ({yyyymmdd(start)}~{yyyymmdd(end)})", f"{n}행")
        if n > 0:
            deepest_with_data = years
    kv("데이터 존재 최심 단계", f"{deepest_with_data}년 전" if deepest_with_data else "5년 미만!")
    kv(
        "판정",
        "5년 백필 가능"
        if deepest_with_data and deepest_with_data >= 5
        else "5년 백필 불가 — 가능한 만큼(확정 7 단서)",
    )


def probe_columns(stock, ticker: str) -> None:
    section(f"2. 컬럼 인벤토리 (KIS 매핑표 원자료) — {ticker}")
    today = date.today()
    start = today - timedelta(days=21)
    for label, fn in (
        ("get_market_trading_value_by_date", stock.get_market_trading_value_by_date),
        ("get_market_trading_volume_by_date", stock.get_market_trading_volume_by_date),
    ):
        for detail in (False, True):
            _progress(f"{label} detail={detail}")
            df, err = _try_window(fn, ticker, start, today, detail=detail)
            name = f"{label}(detail={detail})"
            if err is not None:
                report_error(name, err)
                continue
            if df is None or df.empty:
                kv(name, "빈 결과")
                continue
            kv(name, f"{len(df)}행 × {len(df.columns)}컬럼")
            kv("  컬럼", list(df.columns))
            kv("  index명", df.index.name)
            print(f"\n```\n{df.head(3).to_string()}\n```")


def probe_ohlcv_5y(stock, ticker: str) -> None:
    section(f"3-a. 종목 OHLCV 5년 커버리지 — {ticker} (확정 13)")
    today = date.today()
    start = today - timedelta(days=5 * 365)
    _progress(f"get_market_ohlcv {ticker} 5년")
    df, err = _try_window(stock.get_market_ohlcv, ticker, start, today)
    if err is not None:
        report_error("get_market_ohlcv", err)
        return
    kv("행수", len(df))
    kv("구간", f"{df.index.min()} ~ {df.index.max()}" if len(df) else "-")
    kv("컬럼", list(df.columns))


def probe_index_5y(stock) -> None:
    section("3-b. 벤치마크 지수 OHLC 5년 커버리지 — KOSPI 1001 / KOSDAQ 2001 (확정 13·16)")
    today = date.today()
    start = today - timedelta(days=5 * 365)
    for code, name in (("1001", "KOSPI"), ("2001", "KOSDAQ")):
        _progress(f"get_index_ohlcv {code} 5년")
        df, err = _try_window(stock.get_index_ohlcv, code, start, today)
        if err is not None:
            report_error(f"get_index_ohlcv {name}", err)
            continue
        kv(f"{name}({code}) 행수", len(df))
        kv(f"{name} 구간", f"{df.index.min()} ~ {df.index.max()}" if len(df) else "-")
        kv(f"{name} 컬럼", list(df.columns))


def probe_delisted(stock, n_samples: int) -> None:
    section("4. 상폐 종목 커버리지 — point-in-time 유니버스 (확정 15)")
    today = date.today()
    past = today - timedelta(days=5 * 365)
    # 주말 회피
    while past.weekday() >= 5:
        past += timedelta(days=1)
    try:
        _sleep()
        old_tickers = set(stock.get_market_ticker_list(yyyymmdd(past), market="ALL"))
        _sleep()
        now_tickers = set(stock.get_market_ticker_list(yyyymmdd(today), market="ALL"))
    except Exception as exc:  # noqa: BLE001
        report_error("get_market_ticker_list", exc)
        return
    delisted = sorted(old_tickers - now_tickers)
    kv(f"{yyyymmdd(past)} 상장 종목 수", len(old_tickers))
    kv(f"{yyyymmdd(today)} 상장 종목 수", len(now_tickers))
    kv("5년 창 내 소멸(상폐 등) 종목 수", len(delisted))
    kv("샘플", delisted[:10])

    window_end = past + timedelta(days=90)
    for ticker in delisted[:n_samples]:
        try:
            _sleep()
            name = stock.get_market_ticker_name(ticker)
        except Exception:  # noqa: BLE001
            name = "(이름 조회 실패)"
        _progress(f"상폐 샘플 {ticker} {name}")
        df_o, err_o = _try_window(stock.get_market_ohlcv, ticker, past, window_end)
        df_f, err_f = _try_window(
            stock.get_market_trading_value_by_date, ticker, past, window_end
        )
        kv(
            f"상폐 {ticker} {name}",
            f"OHLCV {'오류 ' + str(err_o) if err_o else f'{len(df_o)}행'} / "
            f"플로우 {'오류 ' + str(err_f) if err_f else f'{len(df_f)}행'}",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kospi", default=SAMPLE_KOSPI)
    parser.add_argument("--kosdaq", default=SAMPLE_KOSDAQ)
    parser.add_argument("--delisted-samples", type=int, default=2)
    args = parser.parse_args()

    report_header("PRJ-03 게이트 ② — pykrx 백필 커버리지 검증")
    from pykrx import stock  # noqa: PLC0415 — 무거운 임포트는 argparse 이후

    for step in (
        lambda: probe_oldest(stock, args.kospi),
        lambda: probe_oldest(stock, args.kosdaq),
        lambda: probe_columns(stock, args.kospi),
        lambda: probe_ohlcv_5y(stock, args.kospi),
        lambda: probe_index_5y(stock),
        lambda: probe_delisted(stock, args.delisted_samples),
    ):
        try:
            step()
        except Exception as exc:  # noqa: BLE001 — 섹션별 독립
            report_error("섹션 실행", exc)


if __name__ == "__main__":
    main()
