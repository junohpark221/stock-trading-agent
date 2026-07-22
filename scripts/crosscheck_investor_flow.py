"""PRJ-03 단계 5 — 수급 DB↔pykrx 크로스소스 대조 스크립트 (EC2 실행).

DB에 저장된 수급 행(최근 창은 kis 잡이 덮어써 source='kis')을 pykrx(KRX 원천)로
신선 재조회한 값과 대조한다 — 단계 5 완료 게이트(1회 PASS) + 이후 의심 시 재실행.
비교 규칙·허용오차는 src/data/flow_crosscheck.py(provider 리비전 감지와 동일 구현).

실행 (EC2, 거래일 19:00 잡 완료 후 권장 — 창 전체가 kis 행):

    docker compose -f docker-compose.prod.yml exec app \
        python scripts/crosscheck_investor_flow.py \
        > crosscheck_stage5.md 2> crosscheck_stage5.log

stdout = 마크다운 리포트 / stderr = 로그. exit 0=PASS, 1=FAIL, 2=환경/인자 오류.
범위: 시장 단위(kospi/kosdaq) 창 전체 + 종목 표본(거래대금 상위 절반 + 랜덤 절반).

표본은 pykrx 주식 티커 커버 종목으로 한정한다(07-22 EC2 1차 실행 교훈): DB 수급 행은
KIS가 ETF·ETN까지 수집하지만 pykrx 종목 수급 API는 주식 전용(ETF는 ISIN 미등재 → 0행)
— 거래대금 상위가 ETF로 채워지면 강등 감지기("DB 행 존재 + pykrx 0행")가 오탐 중단한다.
ETF·ETN은 크로스소스 대조 원천이 없어 비교 대상이 아님(영구 미검증 — 사용자 감수 07-22).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from backfill_pykrx_common import (
    INDEX_TICKERS,
    SLEEP_SEC,
    KrxAuthError,
    _stock,
    build_flow_rows,
    build_market_rows,
    fetch_market_frames,
    fetch_symbol_flow_frames,
    krx_call,
    yyyymmdd,
)
from probe_common import kv, report_header, section, setup_probe_logging
from sqlalchemy import select, text

from src.config import get_settings
from src.data.flow_crosscheck import (
    INDEX_OHLC_FIELDS,
    MARKET_CROSS_FIELDS,
    REVISION_FIELDS_MARKET,
    REVISION_FIELDS_SYMBOL,
    CrosscheckStats,
    crosscheck_rows,
)
from src.db.models.investor_flow import InvestorFlowDaily, MarketInvestorFlowDaily
from src.db.session import close_db, get_session_factory, init_db

KST = ZoneInfo("Asia/Seoul")

# 백필 강등 감지기 미러 — 연속 N종목이 "DB 행은 있는데 pykrx flow 0행"이면
# KRX 익명 세션 강등으로 판정하고 중단한다. 표본이 pykrx 커버 종목으로
# 한정되므로(아래 fetch_stock_universe) 이 전제는 주식에서만 유효하게 성립.
DEGRADATION_STREAK = 5

# 기동 자격 게이트 — pykrx 주식 티커 수가 이 값 이하면 익명 세션 강등으로
# 판정하고 즉시 중단한다(백필 스크립트 기동 게이트 미러).
MIN_STOCK_TICKERS = 2000

# 거래대금 상위 표본 후보 여유 배수 — 상위권은 ETF가 지배하므로(07-22 실측:
# 상위 25 중 11+ ETF) 필터 후에도 목표 수를 채우도록 넉넉히 조회한다.
TOP_CANDIDATE_MULTIPLIER = 3

# 판정 임계 — verdict() 참조.
MAX_OVERALL_MISMATCH_RATE = 0.005  # 전체 셀 0.5%
MAX_FIELD_MISMATCH_RATE = 0.01  # 필드별 1% (compared >= MIN_FIELD_COMPARED)
MIN_FIELD_COMPARED = 20


def _progress(msg: str) -> None:
    print(f"[{datetime.now(KST).strftime('%m-%d %H:%M:%S')}] {msg}", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PRJ-03 단계 5 수급 DB↔pykrx 크로스체크")
    parser.add_argument("--days", type=int, default=30,
                        help="비교 창 거래일 근사 (달력일 ×1.5로 환산, 기본 30)")
    parser.add_argument("--end", type=date.fromisoformat, default=None,
                        help="창 종료일 (기본: 오늘 KST). 비교는 이 날짜 미만만 — 당일 잠정치 제외")
    parser.add_argument("--symbols", default=None,
                        help="쉼표 구분 종목 지정 (표본 선정 생략)")
    parser.add_argument("--sample-size", type=int, default=50,
                        help="종목 표본 크기 — 절반 거래대금 상위 + 절반 랜덤 (기본 50)")
    parser.add_argument("--seed", type=int, default=42, help="랜덤 표본 시드")
    parser.add_argument("--sleep", type=float, default=SLEEP_SEC,
                        help="KRX 콜 간 페이싱(초)")
    parser.add_argument("--max-mismatch-samples", type=int, default=10,
                        help="테이블별 불일치 샘플 출력 상한")
    return parser.parse_args(argv)


# ── 표본 선정 ─────────────────────────────────────────────────────────


def fetch_stock_universe(end: date, sleep_sec: float) -> set[str]:
    """pykrx 주식 티커 집합(KOSPI+KOSDAQ, 우선주 포함 — ETF·ETN 등 비주식 없음).

    표본을 pykrx 종목 수급 API 커버리지로 한정하는 필터이자 기동 자격 게이트:
    호출자는 반환 크기 ≤ MIN_STOCK_TICKERS면 익명 세션 강등으로 판정한다.
    """
    stock = _stock()
    snap = end
    while snap.weekday() >= 5:  # 주말이면 직전 평일로 (미래 날짜 회피 — 뒤로 보정)
        snap -= timedelta(days=1)
    tickers = krx_call(
        stock.get_market_ticker_list, yyyymmdd(snap), market="ALL", sleep_sec=sleep_sec
    )
    return set(tickers)


def filter_covered(candidates: list[str], covered: set[str]) -> tuple[list[str], list[str]]:
    """pykrx 커버 종목만 유지(순서 보존). (유지, 제외) 반환 — 순수 함수."""
    kept = [s for s in candidates if s in covered]
    dropped = [s for s in candidates if s not in covered]
    return kept, dropped


def split_sample(top: list[str], pool: list[str], size: int, seed: int) -> tuple[list[str], list[str]]:
    """(거래대금 상위 절반, 랜덤 절반) — 순수 함수(시드 결정적)."""
    top_half = top[: size // 2]
    remainder = [s for s in pool if s not in set(top_half)]
    k = min(size - len(top_half), len(remainder))
    rand_half = sorted(random.Random(seed).sample(remainder, k)) if k > 0 else []
    return top_half, rand_half


async def pick_sample(
    sf: Any, start: date, end: date, size: int, seed: int, covered: set[str],
) -> tuple[list[str], list[str], int, int]:
    """DB에서 표본 후보 조회 → pykrx 커버 필터 → split_sample.

    (top, random, 상위 후보 제외 수, 랜덤 풀 제외 수) 반환.
    상위 후보는 ETF가 지배하므로 여유 배수로 조회 후 필터한다.
    """
    async with sf() as session:
        top_rows = await session.execute(
            text(
                "SELECT symbol FROM daily_ohlcv WHERE date BETWEEN :s AND :e "
                "GROUP BY symbol ORDER BY sum(trading_value) DESC NULLS LAST LIMIT :n"
            ),
            {"s": start, "e": end, "n": (size // 2) * TOP_CANDIDATE_MULTIPLIER},
        )
        top_raw = [r[0] for r in top_rows]
        pool_rows = await session.execute(
            text(
                "SELECT DISTINCT symbol FROM investor_flow_daily "
                "WHERE date BETWEEN :s AND :e ORDER BY symbol"
            ),
            {"s": start, "e": end},
        )
        pool_raw = [r[0] for r in pool_rows]
    top, top_dropped = filter_covered(top_raw, covered)
    pool, pool_dropped = filter_covered(pool_raw, covered)
    top_half, rand_half = split_sample(top, pool, size, seed)
    return top_half, rand_half, len(top_dropped), len(pool_dropped)


# ── DB 조회 ───────────────────────────────────────────────────────────


async def load_stored_rows(
    sf: Any, *, model: type, key_col: str, key_value: str,
    fields: tuple[str, ...], start: date, end: date,
) -> dict[date, Any]:
    stmt = select(
        model.date, model.source, *[getattr(model, f) for f in fields]
    ).where(
        getattr(model, key_col) == key_value,
        model.date >= start,
        model.date <= end,
    )
    async with sf() as session:
        result = await session.execute(stmt)
        return {row["date"]: row for row in result.mappings()}


async def stored_source_distribution(sf: Any, table: str, start: date, end: date) -> list[tuple]:
    async with sf() as session:
        rows = await session.execute(
            text(
                f"SELECT source, count(*), min(date), max(date) FROM {table} "  # noqa: S608 — 고정 테이블명
                "WHERE date BETWEEN :s AND :e GROUP BY source ORDER BY source"
            ),
            {"s": start, "e": end},
        )
        return list(rows)


# ── 판정 (순수 — 단위 테스트 대상) ────────────────────────────────────


def verdict(stats_by_table: dict[str, CrosscheckStats]) -> tuple[bool, list[str]]:
    """PASS 조건: 비교 행 존재 AND 전체 셀 mismatch ≤ 0.5% AND
    compared ≥ 20인 모든 필드 mismatch ≤ 1%. (통과 여부, 사유 목록) 반환."""
    reasons: list[str] = []
    total_rows = sum(s.compared_rows for s in stats_by_table.values())
    total_cells = sum(s.compared_cells for s in stats_by_table.values())
    total_mismatch = sum(s.mismatched_cells for s in stats_by_table.values())
    if total_rows == 0:
        return False, ["비교된 행이 없음 — 창/표본/DB 적재 상태 확인 필요"]
    overall = total_mismatch / total_cells if total_cells else 0.0
    if overall > MAX_OVERALL_MISMATCH_RATE:
        reasons.append(
            f"전체 셀 불일치율 {overall:.3%} > {MAX_OVERALL_MISMATCH_RATE:.1%}"
        )
    for table, stats in stats_by_table.items():
        for fld, (compared, mismatched) in sorted(stats.per_field.items()):
            if compared >= MIN_FIELD_COMPARED and mismatched / compared > MAX_FIELD_MISMATCH_RATE:
                reasons.append(
                    f"{table}.{fld} 불일치율 {mismatched / compared:.2%} "
                    f"({mismatched}/{compared}) > {MAX_FIELD_MISMATCH_RATE:.0%}"
                )
    return not reasons, reasons


# ── 리포트 렌더 ───────────────────────────────────────────────────────


def render_stats(table: str, stats: CrosscheckStats, max_samples: int) -> None:
    section(f"{table} — 비교 결과")
    kv("비교 행", stats.compared_rows)
    kv("비교 셀", stats.compared_cells)
    kv("불일치 셀", stats.mismatched_cells)
    kv("revision 행(동일 소스 값 변경)", stats.revision_rows)
    kv("cross_source 행(허용오차 초과)", stats.cross_source_rows)
    if stats.per_field:
        print("\n| field | compared | mismatch | rate |")
        print("|---|---|---|---|")
        for fld, (compared, mismatched) in sorted(stats.per_field.items()):
            rate = f"{mismatched / compared:.2%}" if compared else "-"
            print(f"| `{fld}` | {compared} | {mismatched} | {rate} |")
    if stats.diffs:
        print(f"\n**불일치 샘플 (최대 {max_samples}건)**\n")
        print("| key | date | kind | source | field | stored | pykrx | delta |")
        print("|---|---|---|---|---|---|---|---|")
        for d in stats.diffs[:max_samples]:
            try:
                delta = d.new - d.old  # type: ignore[operator]
            except TypeError:
                delta = "-"
            print(
                f"| {d.key} | {d.day} | {d.kind} | {d.source_before} "
                f"| `{d.field}` | {d.old} | {d.new} | {delta} |"
            )


# ── 메인 ──────────────────────────────────────────────────────────────


async def run(args: argparse.Namespace) -> int:
    end = args.end or datetime.now(KST).date()
    start = end - timedelta(days=int(args.days * 1.5))
    t0 = time.monotonic()

    if not (os.getenv("KRX_ID") and os.getenv("KRX_PW")):
        print("오류: KRX_ID/KRX_PW 환경변수가 없습니다 (.env 확인).", file=sys.stderr)
        return 2

    await init_db(get_settings())
    sf = get_session_factory()
    try:
        report_header("PRJ-03 단계 5 — 수급 DB↔pykrx 크로스체크")
        kv("비교 창", f"{start} ~ {end} (비교는 {end} 미만 — 당일 잠정치 제외)")
        kv("허용오차", "대금 ±1e6원(KIS 백만원 절사) · 시장 수량 ±500주 · 지수 ±0.01 · 종목 수량 exact")

        # pykrx 주식 커버 집합 — 표본 필터 + 기동 자격 게이트
        _progress("pykrx 주식 티커 조회 (표본 커버리지 필터 + 자격 게이트)")
        covered = await asyncio.to_thread(fetch_stock_universe, end, args.sleep)
        if len(covered) <= MIN_STOCK_TICKERS:
            print(
                f"오류: pykrx 주식 티커 {len(covered)}개 (≤{MIN_STOCK_TICKERS}) "
                "— KRX 로그인/익명 강등 확인 필요.",
                file=sys.stderr,
            )
            return 2

        # 표본 선정 — pykrx 커버 종목 한정 (ETF·ETN은 대조 원천 부재 — 모듈 독스트링)
        top_dropped = pool_dropped = 0
        dropped_symbols: list[str] = []
        if args.symbols:
            requested = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            top_half, dropped_symbols = filter_covered(requested, covered)
            rand_half = []
        else:
            top_half, rand_half, top_dropped, pool_dropped = await pick_sample(
                sf, start, end, args.sample_size, args.seed, covered
            )
        symbols = top_half + rand_half
        section("표본")
        kv("거래대금 상위", f"{len(top_half)}개 — {', '.join(top_half)}")
        kv(f"랜덤(seed={args.seed})", f"{len(rand_half)}개 — {', '.join(rand_half)}")
        if dropped_symbols:
            kv(
                "pykrx 미커버 제외(--symbols)",
                f"{len(dropped_symbols)}개 — {', '.join(dropped_symbols)}",
            )
        if top_dropped or pool_dropped:
            kv(
                "pykrx 미커버 제외(ETF·ETN 등)",
                f"상위 후보 {top_dropped}개 · 랜덤 풀 {pool_dropped}개",
            )
        if not symbols:
            print("오류: 표본이 비었습니다 — pykrx 커버 종목이 없습니다.", file=sys.stderr)
            return 2

        # 저장 source 분포 (판독 맥락 — 창 대부분 kis여야 크로스소스 비교가 성립)
        section("저장 행 source 분포 (창 내)")
        for table in ("investor_flow_daily", "market_investor_flow_daily"):
            for src_name, cnt, dmin, dmax in await stored_source_distribution(sf, table, start, end):
                kv(table, f"{src_name}: {cnt}행 ({dmin} ~ {dmax})")

        stats_by_table: dict[str, CrosscheckStats] = {
            "market_flow": CrosscheckStats(),
            "flow": CrosscheckStats(),
        }

        # 1) 시장 페이즈 — kospi/kosdaq 창 전체 (시장당 3콜)
        for market in INDEX_TICKERS:
            _progress(f"시장 대조: {market}")
            frames = await asyncio.to_thread(
                fetch_market_frames, market, start, end, sleep_sec=args.sleep
            )
            pykrx_rows = build_market_rows(market, frames, start)
            stored = await load_stored_rows(
                sf, model=MarketInvestorFlowDaily, key_col="market", key_value=market,
                fields=REVISION_FIELDS_MARKET, start=start, end=end,
            )
            stats = crosscheck_rows(
                stored, pykrx_rows, key_value=market, table="market_flow",
                incoming_source="krx", before=end,
            )
            stats_by_table["market_flow"].merge(stats, max_diffs=args.max_mismatch_samples)
            _progress(
                f"  {market}: 행 {stats.compared_rows} · 불일치 셀 {stats.mismatched_cells}"
            )

        # 2) 종목 페이즈 — 표본 (종목당 6콜)
        degradation_streak = 0
        for i, symbol in enumerate(symbols, 1):
            _progress(f"종목 대조 {i}/{len(symbols)}: {symbol}")
            frames = await asyncio.to_thread(
                fetch_symbol_flow_frames, symbol, start, end, sleep_sec=args.sleep
            )
            pykrx_rows = build_flow_rows(symbol, frames)
            stored = await load_stored_rows(
                sf, model=InvestorFlowDaily, key_col="symbol", key_value=symbol,
                fields=REVISION_FIELDS_SYMBOL, start=start, end=end,
            )
            if not pykrx_rows and stored:
                degradation_streak += 1
                if degradation_streak >= DEGRADATION_STREAK:
                    raise KrxAuthError(
                        f"연속 {DEGRADATION_STREAK}종목 pykrx flow 0행(DB 행은 존재) "
                        "— KRX 익명 세션 강등 의심"
                    )
                continue
            degradation_streak = 0
            stats = crosscheck_rows(
                stored, pykrx_rows, key_value=symbol, table="flow",
                incoming_source="krx", before=end,
            )
            stats_by_table["flow"].merge(stats, max_diffs=args.max_mismatch_samples)

        # 3) 리포트·판정
        for table, stats in stats_by_table.items():
            render_stats(table, stats, args.max_mismatch_samples)

        ok, reasons = verdict(stats_by_table)
        section("판정")
        kv("소요", f"{time.monotonic() - t0:.0f}s")
        for reason in reasons:
            kv("사유", reason)
        print(f"\n**판정: {'PASS' if ok else 'FAIL'}**")
        return 0 if ok else 1
    except KrxAuthError as exc:
        print(f"\n**판정: FAIL (중단)** — {exc}")
        _progress(f"중단: {exc}")
        return 2
    finally:
        await close_db()


def main() -> None:
    setup_probe_logging()
    sys.exit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
