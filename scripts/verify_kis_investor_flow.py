"""PRJ-03 단계 2 — KIS 수급 클라이언트 4종 실검증 + 이월 검증 3건 판정.

검증 대상:
1. 스모크: KISClient 수급 메서드 4종 실호출 — 행수·최신 레코드·원 단위 자릿수
2. 이월 ②: 시장 TR ``*_ntby_qty`` 단위 판정 — 축별 implied price(|net_amt|/|net_qty|)
3. 이월 ① + 신규: 대차 TR 종목 모드(``MRKT_DIV_CLS_CODE="3"``) 응답 스키마 확정
   (게이트 ① 프로브는 "1"=코스피 시장 단위 오호출이었음 — 2026-07-15 발견)
   + ``rmnd_amt`` 단위 판정 + 100행 캡·창 이동 페이징 동작
4. 공매도 장기 범위(1년) 행수 캡 실측

실행 (운영 EC2, 사용자 SSH):
    cd /home/ec2-user/stock-trading-agent
    docker compose -f docker-compose.prod.yml exec app \
        python scripts/verify_kis_investor_flow.py > prj03_stage2_verify.md
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
from decimal import Decimal

from probe_common import (
    LOAN_TRANS_PATH,
    LOAN_TRANS_TR,
    build_kis_client,
    close_kis_client,
    dump_fields,
    kv,
    recent_business_day,
    report_error,
    report_header,
    sample_rows,
    section,
    setup_probe_logging,
    yyyymmdd,
)

# 시장 TR 수급 15축 (KISMarketInvestorFlowOutput → MarketInvestorFlowRecord 컬럼 접두)
_MARKET_AXES = [
    "frgn", "frgn_reg", "frgn_nreg", "prsn", "orgn", "scrt", "ivtr",
    "pe_fund", "bank", "insu", "mrbn", "fund", "etc", "etc_corp", "etc_orgt",
]


async def verify_smoke(client, symbol: str) -> None:
    section("1. 스모크 — 클라이언트 수급 메서드 4종 실호출")
    end = recent_business_day()
    start = end - timedelta(days=30)

    flows = await client.get_investor_flow(symbol)
    kv("get_investor_flow 행수 (단발)", len(flows))
    if flows:
        latest = flows[-1]
        kv("최신 일자", latest.date)
        kv("frgn_net_qty", f"{latest.frgn_net_qty:,}")
        kv(
            "frgn_net_amt (원 단위 — 대형주 기준 수백억~수천억대가 정상)",
            f"{latest.frgn_net_amt:,.0f}",
        )
        kv("frgn_sell_qty/buy_qty", f"{latest.frgn_sell_qty:,} / {latest.frgn_buy_qty:,}")

    for market in ("kospi", "kosdaq"):
        mflows = await client.get_market_investor_flow(market)
        kv(f"get_market_investor_flow('{market}') 행수", len(mflows))
        if mflows:
            latest = mflows[-1]
            kv(
                f"{market} 최신({latest.date}) 지수 OHLC",
                f"O {latest.index_open} / H {latest.index_high} / "
                f"L {latest.index_low} / C {latest.index_close}",
            )

    shorts = await client.get_daily_short_sale(symbol, start_date=start, end_date=end)
    kv("get_daily_short_sale 행수 (최근 30일)", len(shorts))
    if shorts:
        latest = shorts[-1]
        kv(
            f"최신({latest.date}) 공매도",
            f"qty {latest.short_sale_qty:,} / amt {latest.short_sale_amt:,.0f}원 "
            f"/ 비중 {latest.short_sale_vol_ratio}%",
        )

    loans = await client.get_daily_loan_trans(symbol, start_date=start, end_date=end)
    kv("get_daily_loan_trans 행수 (최근 30일)", len(loans))
    if loans:
        latest = loans[-1]
        kv(
            f"최신({latest.date}) 대차",
            f"신규 {latest.loan_new_qty:,} / 상환 {latest.loan_redemption_qty:,} "
            f"/ 잔고 {latest.loan_balance_qty:,} / 잔고금액(raw) {latest.loan_balance_amt:,.0f}",
        )


async def verify_market_qty_unit(client) -> None:
    section("2. 이월 검증 ② — 시장 TR *_ntby_qty 단위 판정 (천주 의심)")
    print(
        "판정 기준: 축별 implied price = |net_amt(원)| / |net_qty|.\n"
        "- 주 단위라면 implied price ≈ 시장 평균 주가(수만원대)여야 한다.\n"
        "- 값이 수백만~수천만원대로 나오면 net_qty가 **천주 단위**라는 뜻\n"
        "  (실제 주당가는 ÷1,000). probe1 KSP 실측값 기준 예상: 천주 단위.\n"
    )
    for market in ("kospi", "kosdaq"):
        rows = await client.get_market_investor_flow(market)
        if not rows:
            kv(market, "(빈 응답 — 판정 불가)")
            continue
        latest = rows[-1]
        print(f"\n### {market} — {latest.date}\n")
        print("| axis | net_qty (raw) | net_amt (원) | implied price (원) |")
        print("|---|---|---|---|")
        for axis in _MARKET_AXES:
            qty = getattr(latest, f"{axis}_net_qty")
            amt = getattr(latest, f"{axis}_net_amt")
            implied = (
                abs(amt) / abs(qty) if qty not in (None, 0) and amt is not None else None
            )
            implied_s = f"{implied:,.0f}" if implied is not None else "-"
            print(f"| {axis} | {qty:,} | {amt:,.0f} | {implied_s} |")


async def verify_loan_stock_mode(client, symbol: str) -> None:
    section('3. 대차 TR 종목 모드("3") 스키마 확정 + 이월 검증 ① (rmnd_amt 단위)')
    kv(
        "배경",
        'probe1은 MRKT_DIV_CLS_CODE="1"(코스피 시장 단위)로 오호출 — '
        "종목 모드 스키마·단위는 이번이 첫 실측",
    )
    end = recent_business_day()
    start = end - timedelta(days=240)  # ~160영업일 → 100행 캡·창 이동 검증 겸용

    data = await client._request(  # noqa: SLF001 — 원시 스키마 덤프용 저수준 호출
        "GET", LOAN_TRANS_PATH, LOAN_TRANS_TR,
        params={
            "MRKT_DIV_CLS_CODE": "3",
            "MKSC_SHRN_ISCD": symbol,
            "START_DATE": yyyymmdd(start),
            "END_DATE": yyyymmdd(end),
            "CTS": "",
        },
    )
    rows = data.get("output1") or []
    if isinstance(rows, dict):
        rows = [rows]
    kv("응답 top-level 키", sorted(data.keys()))
    kv("행수", f"{len(rows)} (100행 캡 여부: {len(rows) == 100})")
    kv("tr_cont 헤더", repr(data.get("_tr_cont", "")))
    if not rows:
        kv("판정", "빈 응답 — 종목 모드 파라미터 재검토 필요")
        return

    dates = [r.get("bsop_date", "") for r in rows]
    kv("실제 반환 구간", f"{min(dates)} ~ {max(dates)}")
    dump_fields("output1[0] (종목 모드 스키마 확정 원자료)", rows[0])
    kv("output1 샘플", "")
    sample_rows(rows, 2)

    # (a) 가정 스키마(KISLoanTransOutput 6필드) 실존 여부
    expected = {"bsop_date", "new_stcn", "rdmp_stcn", "prdy_rmnd_vrss", "rmnd_stcn", "rmnd_amt"}
    missing = expected - set(rows[0].keys())
    kv("가정 스키마 6필드 실존", "전부 실존" if not missing else f"누락: {sorted(missing)}")

    # (b) 시세 필드가 지수가 아닌 종목 주가인지 (시장 단위 혼입 해소 판정)
    price = await client.get_price(symbol)
    kv("종목 현재가 (get_price)", f"{price.current_price:,.0f}원")
    kv(
        "output1[0].stck_prpr",
        f"{rows[0].get('stck_prpr', '(없음)')} — 종목 주가 자릿수와 일치해야 정상 "
        "(probe1에선 지수값 7475.94가 혼입됐었음)",
    )

    # (c) rmnd_amt 단위 판정: rmnd_amt / rmnd_stcn ≈ 주가라면 원 단위
    latest = rows[0]
    rmnd_stcn = Decimal(latest.get("rmnd_stcn") or "0")
    rmnd_amt = Decimal(latest.get("rmnd_amt") or "0")
    if rmnd_stcn > 0:
        per_share = rmnd_amt / rmnd_stcn
        kv("rmnd_amt / rmnd_stcn", f"{per_share:,.2f}")
        print(
            "- **판정 기준**: 위 값이 주가(≈현재가)면 rmnd_amt는 **원 단위**, "
            "주가의 1/1,000이면 **천원**, 1/1,000,000이면 **백만원** 단위. "
            "확정되면 KISLoanTransOutput.to_domain 1곳에 변환을 반영한다."
        )
    else:
        kv("rmnd_amt 단위 판정", "rmnd_stcn=0 — 다른 종목으로 재실행 필요")

    # (d) 클라이언트 창 이동 페이징 결과와 대조
    records = await client.get_daily_loan_trans(symbol, start_date=start, end_date=end)
    kv(
        "get_daily_loan_trans (동일 8개월 범위)",
        f"{len(records)}행 / {records[0].date if records else '-'} ~ "
        f"{records[-1].date if records else '-'}",
    )
    kv(
        "페이징 판정",
        "단발 100행 캡 대비 클라이언트가 더 긴 구간을 채웠으면 창 이동 정상 동작",
    )


async def verify_short_sale_cap(client, symbol: str) -> None:
    section("4. 공매도 TR 장기 범위(1년) 행수 캡 실측")
    end = recent_business_day()
    start = end - timedelta(days=365)
    records = await client.get_daily_short_sale(symbol, start_date=start, end_date=end)
    kv("요청 범위", f"{yyyymmdd(start)} ~ {yyyymmdd(end)} (기대 ~245영업일)")
    kv("반환 행수", len(records))
    if records:
        kv("실제 커버 구간", f"{records[0].date} ~ {records[-1].date}")
    kv(
        "판정",
        "행수가 기대 영업일 수준이면 캡 없음(창 이동 루프는 방어용으로 유지), "
        "특정 행수에서 잘리면 그 값이 1회 캡 — dev.md에 기록",
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="005930", help="검증 종목코드 (기본 005930)")
    args = parser.parse_args()

    setup_probe_logging()
    report_header("PRJ-03 단계 2 — KIS 수급 클라이언트 실검증 리포트")
    kv("검증 종목", args.symbol)

    client, redis = await build_kis_client()
    try:
        for name, coro in (
            ("스모크", verify_smoke(client, args.symbol)),
            ("시장 ntby_qty 단위", verify_market_qty_unit(client)),
            ("대차 종목 모드", verify_loan_stock_mode(client, args.symbol)),
            ("공매도 장기 캡", verify_short_sale_cap(client, args.symbol)),
        ):
            try:
                await coro
            except Exception as exc:  # noqa: BLE001 — 섹션별 격리, 리포트에 남긴다
                report_error(name, exc)
    finally:
        await close_kis_client(client, redis)


if __name__ == "__main__":
    asyncio.run(main())
