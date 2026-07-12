"""PRJ-03 착수 게이트 ① — KIS 수급 TR 4종 실응답 검증 프로브.

검증 대상:
1. FHPTJ04160001 종목별 투자자매매동향 — 실제 응답 스키마·페이지당 행수·연속조회·재앵커
2. FHPTJ04040000 시장/업종 단위 투자자매매동향 — 지수 OHLC 필드 실존(확정 16 전제)·페이징 유무
3. FHPST04830000 종목별 공매도 일별추이 — 날짜범위 지정 동작
4. HHPST074500C0 종목별 대차거래추이 — 100행 캡·CTS 연속키 동작
부수: 컨테이너 디스크 여유(plan.md 부수 실측 항목)

실행 (운영 EC2, 사용자 SSH):
    cd /home/ec2-user/stock-trading-agent
    docker compose -f docker-compose.prod.yml exec app \
        python scripts/probe_kis_investor_flow_api.py > probe1_kis_api.md
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
from datetime import date, timedelta

from probe_common import (
    LOAN_TRANS_PATH,
    LOAN_TRANS_TR,
    SHORT_SALE_PATH,
    SHORT_SALE_TR,
    build_kis_client,
    close_kis_client,
    dump_fields,
    fetch_investor_daily,
    fetch_market_investor_daily,
    kv,
    prev_business_day,
    recent_business_day,
    report_error,
    report_header,
    sample_rows,
    section,
    setup_probe_logging,
    yyyymmdd,
)


async def probe_investor_daily(client, symbol: str, max_pages: int) -> None:
    section(f"1. FHPTJ04160001 종목별 투자자매매동향 — {symbol}")
    anchor = recent_business_day()
    kv("앵커 일자", yyyymmdd(anchor))
    output1, rows, per_page = await fetch_investor_daily(
        client, symbol, yyyymmdd(anchor), max_pages=max_pages
    )
    kv("페이지 수", len(per_page))
    kv("페이지당 행수", per_page)
    kv("총 행수", len(rows))
    if rows:
        dates = [r.get("stck_bsop_date", "") for r in rows]
        kv("커버 구간", f"{min(dates)} ~ {max(dates)}")
        dump_fields("output1 (종목 요약)", output1)
        dump_fields("output2[0] (일별 시계열 — 스키마 확정 원자료)", rows[0])
        kv("output2 샘플", "")
        sample_rows(rows, n=2)

        # 재앵커: 최고(最古) 일자 - 1일을 새 앵커로 — 연속조회 한계 너머 조회 가능 여부
        oldest = min(dates)
        re_anchor = prev_business_day(
            date.fromisoformat(f"{oldest[:4]}-{oldest[4:6]}-{oldest[6:]}")
        )
        kv("재앵커 일자", yyyymmdd(re_anchor))
        _, rows2, per_page2 = await fetch_investor_daily(
            client, symbol, yyyymmdd(re_anchor), max_pages=2
        )
        if rows2:
            dates2 = [r.get("stck_bsop_date", "") for r in rows2]
            kv("재앵커 결과", f"{len(rows2)}행, {min(dates2)} ~ {max(dates2)} (페이지 {per_page2})")
            kv("재앵커로 과거 연장 가능", str(max(dates2) < oldest))
        else:
            kv("재앵커 결과", "0행 — 과거 연장 불가 또는 데이터 없음")


async def probe_market_investor(client) -> None:
    section("2. FHPTJ04040000 시장 단위 투자자매매동향 (지수 OHLC 실존 확인)")
    anchor = yyyymmdd(recent_business_day())
    for market, sector in (("KSP", "0001"), ("KSQ", "1001")):
        kv("시장", f"{market} / 업종코드 {sector} / 앵커 {anchor}")
        try:
            data = await fetch_market_investor_daily(
                client, market=market, sector_code=sector, anchor_date=anchor
            )
        except Exception as exc:  # noqa: BLE001 — 프로브: 실패 자체가 실측 결과
            report_error(f"FHPTJ04040000 {market}", exc)
            continue
        rows = data.get("output") or data.get("output1") or []
        if isinstance(rows, dict):
            rows = [rows]
        kv("응답 top-level 키", sorted(data.keys()))
        kv("행수 (단발 호출)", len(rows))
        kv("tr_cont 헤더", repr(data.get("_tr_cont", "")))
        if rows:
            ohlc_fields = ["bstp_nmix_oprc", "bstp_nmix_hgpr", "bstp_nmix_lwpr", "bstp_nmix_prpr"]
            present = {f: (f in rows[0]) for f in ohlc_fields}
            kv("지수 OHLC 필드 실존 (확정 16 전제)", present)
            dump_fields(f"output[0] ({market})", rows[0])


async def probe_short_sale(client, symbol: str) -> None:
    section(f"3. FHPST04830000 공매도 일별추이 — {symbol} (날짜범위 지정)")
    end = recent_business_day()
    start = end - timedelta(days=90)
    kv("요청 범위", f"{yyyymmdd(start)} ~ {yyyymmdd(end)} (약 3개월, 기대 ~60영업일)")
    data = await client._request(  # noqa: SLF001
        "GET", SHORT_SALE_PATH, SHORT_SALE_TR,
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": yyyymmdd(start),
            "FID_INPUT_DATE_2": yyyymmdd(end),
        },
    )
    kv("응답 top-level 키", sorted(data.keys()))
    output1 = data.get("output1") or {}
    rows = data.get("output2") or []
    kv("output2 행수", len(rows))
    kv("tr_cont 헤더", repr(data.get("_tr_cont", "")))
    if rows:
        dates = [r.get("stck_bsop_date", "") for r in rows]
        kv("실제 반환 구간", f"{min(dates)} ~ {max(dates)}")
        kv("범위 지정 동작", str(min(dates) >= yyyymmdd(start) and max(dates) <= yyyymmdd(end)))
    dump_fields("output1 (요약)", output1 if isinstance(output1, dict) else None)
    if rows:
        dump_fields("output2[0] (일별)", rows[0])


async def probe_loan_trans(client, symbol: str) -> None:
    section(f"4. HHPST074500C0 대차거래추이 — {symbol} (100행 캡·CTS 확인)")
    end = recent_business_day()
    start = end - timedelta(days=240)  # ~160영업일 > 100건 캡 검증
    kv("요청 범위", f"{yyyymmdd(start)} ~ {yyyymmdd(end)} (약 8개월 — 100건 초과 유도)")
    data = await client._request(  # noqa: SLF001
        "GET", LOAN_TRANS_PATH, LOAN_TRANS_TR,
        params={
            "MRKT_DIV_CLS_CODE": "1",
            "MKSC_SHRN_ISCD": symbol,
            "START_DATE": yyyymmdd(start),
            "END_DATE": yyyymmdd(end),
            "CTS": "",
        },
    )
    kv("응답 top-level 키", sorted(data.keys()))
    rows = data.get("output1") or data.get("output") or []
    if isinstance(rows, dict):
        rows = [rows]
    kv("행수", f"{len(rows)} (100행 캡 여부: {len(rows) == 100})")
    kv("tr_cont 헤더", repr(data.get("_tr_cont", "")))
    cts_keys = [k for k in data if "cts" in k.lower() or "ctx" in k.lower()]
    kv("CTS/CTX 후보 키", {k: data[k] for k in cts_keys} if cts_keys else "(없음)")
    if rows:
        dates = [r.get("bsop_date", "") for r in rows]
        kv("실제 반환 구간", f"{min(dates)} ~ {max(dates)}")
        dump_fields("output1[0] (일별)", rows[0])

    # CTS 연속키가 보이면 1회 연속조회 시도
    for k in cts_keys:
        if data.get(k):
            kv("CTS 연속조회 시도", f"{k}={data[k]!r}")
            try:
                data2 = await client._request(  # noqa: SLF001
                    "GET", LOAN_TRANS_PATH, LOAN_TRANS_TR,
                    params={
                        "MRKT_DIV_CLS_CODE": "1",
                        "MKSC_SHRN_ISCD": symbol,
                        "START_DATE": yyyymmdd(start),
                        "END_DATE": yyyymmdd(end),
                        "CTS": str(data[k]),
                    },
                    tr_cont="N",
                )
                rows2 = data2.get("output1") or []
                dates2 = [r.get("bsop_date", "") for r in rows2]
                kv(
                    "CTS 연속조회 결과",
                    f"{len(rows2)}행, {min(dates2)} ~ {max(dates2)}" if rows2 else "0행",
                )
            except Exception as exc:  # noqa: BLE001
                report_error("CTS 연속조회", exc)
            break


def probe_disk() -> None:
    section("5. 부수 실측 — 컨테이너 디스크 여유 (plan.md 부수 실측)")
    usage = shutil.disk_usage("/")
    gb = 1024**3
    kv("total", f"{usage.total / gb:.1f} GB")
    kv("used", f"{usage.used / gb:.1f} GB")
    kv("free", f"{usage.free / gb:.1f} GB")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="005930", help="검증 대상 종목 (기본 005930)")
    parser.add_argument("--pages", type=int, default=12, help="FHPTJ04160001 최대 페이지 수")
    args = parser.parse_args()

    setup_probe_logging()
    report_header("PRJ-03 게이트 ① — KIS 수급 TR 실응답 검증")

    client, redis = await build_kis_client()
    try:
        for label, coro in (
            ("FHPTJ04160001", probe_investor_daily(client, args.symbol, args.pages)),
            ("FHPTJ04040000", probe_market_investor(client)),
            ("FHPST04830000", probe_short_sale(client, args.symbol)),
            ("HHPST074500C0", probe_loan_trans(client, args.symbol)),
        ):
            try:
                await coro
            except Exception as exc:  # noqa: BLE001 — 섹션별 독립: 실패도 실측 결과
                report_error(label, exc)
        probe_disk()
    finally:
        await close_kis_client(client, redis)


if __name__ == "__main__":
    asyncio.run(main())
