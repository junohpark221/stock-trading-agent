"""PRJ-03 착수 게이트 ③ — KIS ↔ KRX(pykrx) 수급 정합성 크로스체크 프로브.

동일 종목·일자의 투자자별 순매수(수량·대금)를 KIS FHPTJ04160001과
pykrx(detail=True)에서 각각 수집해 셀 단위 비교한다. 특히 외국인은
KIS `frgn` vs pykrx `외국인` 단독 / `외국인+기타외국인` 합산 두 조합을 모두
비교해 어느 정의가 일치하는지 판정한다(함정 ④ 외국인 정의 2종 실측 해소).

전제: 게이트 ①(KIS 실응답)·②(pykrx 커버리지) 프로브가 성공한 뒤 실행.

실행 (운영 EC2, 사용자 SSH):
    cd /home/ec2-user/stock-trading-agent
    docker compose -f docker-compose.prod.yml exec app \
        python scripts/probe_kis_krx_crosscheck.py > probe3_crosscheck.md
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from probe_common import (
    build_kis_client,
    close_kis_client,
    fetch_investor_daily,
    kv,
    recent_business_day,
    report_error,
    report_header,
    section,
    setup_probe_logging,
    yyyymmdd,
)

# KOSPI 대형 5 + KOSDAQ 3
DEFAULT_SYMBOLS = "005930,000660,005380,035420,105560,247540,086520,041510"

PYKRX_SLEEP = 0.7


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "").strip() or "0")
    except InvalidOperation:
        return None


def _kis_rows_by_date(rows: list[dict]) -> dict[str, dict]:
    return {r["stck_bsop_date"]: r for r in rows if r.get("stck_bsop_date")}


async def _fetch_pykrx(stock, fn_name: str, start: date, end: date, ticker: str):
    """pykrx 동기 호출을 스레드로 — (date_str → {컬럼: Decimal}) 매핑 반환."""

    def _call():
        import time

        time.sleep(PYKRX_SLEEP)
        fn = getattr(stock, fn_name)
        return fn(yyyymmdd(start), yyyymmdd(end), ticker, detail=True)

    df = await asyncio.to_thread(_call)
    out: dict[str, dict[str, Decimal | None]] = {}
    for idx, row in df.iterrows():
        key = idx.strftime("%Y%m%d") if hasattr(idx, "strftime") else str(idx)
        out[key] = {col: _dec(row[col]) for col in df.columns}
    return out, list(df.columns)


class AxisStat:
    """비교 축 하나(예: 외국인 수량 vs 외국인 단독)의 일치 통계."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.total = 0
        self.match = 0
        self.mismatches: list[str] = []

    def add(self, sym: str, day: str, kis: Decimal | None, krx: Decimal | None) -> None:
        if kis is None or krx is None:
            return
        self.total += 1
        if kis == krx:
            self.match += 1
        elif len(self.mismatches) < 10:
            ratio = f"{kis / krx:.6f}" if krx else "div0"
            self.mismatches.append(f"{sym} {day}: KIS={kis} KRX={krx} (KIS/KRX={ratio})")

    def report(self) -> None:
        rate = (
            f"{self.match}/{self.total} ({self.match / self.total:.1%})"
            if self.total
            else "표본 0"
        )
        kv(self.name, rate)
        for m in self.mismatches:
            print(f"  - 불일치 {m}")


async def crosscheck_symbol(
    client, stock, symbol: str, start: date, end: date, stats: dict[str, AxisStat]
) -> None:
    _, kis_rows, _ = await fetch_investor_daily(client, symbol, yyyymmdd(end), max_pages=2)
    kis_by_date = {
        d: r for d, r in _kis_rows_by_date(kis_rows).items() if d >= yyyymmdd(start)
    }
    krx_qty, qty_cols = await _fetch_pykrx(
        stock, "get_market_trading_volume_by_date", start, end, symbol
    )
    krx_val, val_cols = await _fetch_pykrx(
        stock, "get_market_trading_value_by_date", start, end, symbol
    )
    kv(
        f"{symbol} 표본",
        f"KIS {len(kis_by_date)}일 / KRX qty {len(krx_qty)}일·val {len(krx_val)}일",
    )

    for day, kis in sorted(kis_by_date.items()):
        for krx_map, kis_suffix, unit in (
            (krx_qty, "_ntby_qty", "수량"),
            (krx_val, "_ntby_tr_pbmn", "대금"),
        ):
            krx = krx_map.get(day)
            if not krx:
                continue
            frgn_kis = _dec(kis.get(f"frgn{kis_suffix}"))
            frgn_only = krx.get("외국인")
            frgn_etc = krx.get("기타외국인")
            frgn_sum = (
                frgn_only + frgn_etc if frgn_only is not None and frgn_etc is not None else None
            )
            stats[f"외국인 {unit} vs KRX 외국인 단독"].add(symbol, day, frgn_kis, frgn_only)
            stats[f"외국인 {unit} vs KRX 외국인+기타외국인"].add(symbol, day, frgn_kis, frgn_sum)
            stats[f"기관합계 {unit}"].add(
                symbol, day, _dec(kis.get(f"orgn{kis_suffix}")), krx.get("기관합계")
            )
            stats[f"개인 {unit}"].add(
                symbol, day, _dec(kis.get(f"prsn{kis_suffix}")), krx.get("개인")
            )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS, help="쉼표 구분 종목코드")
    parser.add_argument("--days", type=int, default=20, help="최근 N영업일 비교(달력일 기준 근사)")
    args = parser.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    setup_probe_logging()
    report_header("PRJ-03 게이트 ③ — KIS ↔ KRX 수급 정합성 크로스체크")

    from pykrx import stock  # noqa: PLC0415

    end = recent_business_day()
    start = end - timedelta(days=int(args.days * 1.5))  # 영업일→달력일 근사
    section(f"비교 구간 {yyyymmdd(start)} ~ {yyyymmdd(end)} · 종목 {len(symbols)}개")
    kv("종목", symbols)

    stats: dict[str, AxisStat] = {}
    for unit in ("수량", "대금"):
        for name in (
            f"외국인 {unit} vs KRX 외국인 단독",
            f"외국인 {unit} vs KRX 외국인+기타외국인",
            f"기관합계 {unit}",
            f"개인 {unit}",
        ):
            stats[name] = AxisStat(name)

    client, redis = await build_kis_client()
    try:
        for symbol in symbols:
            try:
                await crosscheck_symbol(client, stock, symbol, start, end, stats)
            except Exception as exc:  # noqa: BLE001 — 종목별 독립
                report_error(f"종목 {symbol}", exc)
    finally:
        await close_kis_client(client, redis)

    section("축별 일치율")
    for stat in stats.values():
        stat.report()

    section("판정 가이드")
    print(
        "- 외국인 두 조합 중 일치율이 높은 쪽이 KIS `frgn`의 실제 정의"
        " (거래소 기준 vs 한도 기준 — plan.md 함정 ④).\n"
        "- 대금 축 불일치의 KIS/KRX 비율이 일정 상수(예: 1000)면 단위 차이"
        " (KIS 대금 단위 확인 → 스키마 주석에 반영).\n"
        "- 전 축 불일치면 T+2 정산·잠정치 혼입 여부부터 의심."
    )


if __name__ == "__main__":
    asyncio.run(main())
