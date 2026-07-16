"""PRJ-03 단계 4 — pykrx 5년 수급·OHLCV·지수 백필 (운영 EC2 전용 실행).

대상 테이블: investor_flow_daily(90축, source='krx') · market_investor_flow_daily
(net 축 + 지수 OHLC/거래량/거래대금/시총) · daily_ohlcv(수정주가 + 거래대금).
KIS 행 불가침(WHERE source != 'kis' / DO NOTHING) — 07-16 확정.

실행 (운영 EC2, 사용자 SSH — 러너북: docs/plans/projects/PRJ-03-*/dev.md 단계 4):
    cd /home/ec2-user/stock-trading-agent
    # 1) 파일럿 (쓰기 없음 — 상폐 종목 1개 포함해 네이버 커버리지 확인):
    docker compose -f docker-compose.prod.yml exec app \
        python scripts/backfill_pykrx_5y.py --symbols 005930,041510,000060 --dry-run
    # 2) 소규모 쓰기 스모크:
    docker compose -f docker-compose.prod.yml exec app \
        python scripts/backfill_pykrx_5y.py --limit 20
    # 3) 전체 detached 실행 (SSH 끊겨도 생존; ~24-26h, 체크포인트 재개 가능):
    docker compose -f docker-compose.prod.yml exec -d app sh -c \
        'python scripts/backfill_pykrx_5y.py \
           > /app/data/backfill/backfill_report.md \
           2> /app/data/backfill/backfill_progress.log'
    # 모니터: ... exec app tail -f /app/data/backfill/backfill_progress.log
    # 우아한 중단: ... exec app touch /app/data/backfill/STOP → 재실행 시 자동 재개

리포트는 stdout(마크다운), 진행 로그는 stderr — probe 스크립트 컨벤션.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backfill_pykrx_common import (
    FLOW_BATCH,
    FLOW_SOURCE,
    FLOW_UPDATE_COLS,
    INDEX_TICKERS,
    MARKET_BATCH,
    MARKET_UPDATE_COLS,
    OHLCV_BATCH,
    SLEEP_SEC,
    Checkpoint,
    KrxAuthError,
    build_flow_rows,
    build_market_rows,
    build_ohlcv_rows,
    build_universe,
    fetch_market_frames,
    fetch_symbol_flow_frames,
    fetch_symbol_ohlcv_frames,
    krx_call,
    load_checkpoint,
    save_checkpoint,
    upsert_rows,
)
from sqlalchemy import text

from src.config import get_settings
from src.db.models.investor_flow import InvestorFlowDaily, MarketInvestorFlowDaily
from src.db.models.market_data import DailyOHLCV
from src.db.session import close_db, get_session_factory, init_db

KST = ZoneInfo("Asia/Seoul")

DEFAULT_CHECKPOINT = "/app/data/backfill/pykrx_5y_checkpoint.json"

# 파일럿 sanity: 상장 유지 종목(OHLCV 커버리지 60%↑)의 flow 행수가 거래일수의
# 60% 미만이면 행 캡/익명 강등 의심 — 몇 시간짜리 쓰레기 적재 전에 중단한다.
SANITY_MIN_RATIO = 0.6
# 연속 N종목이 "OHLCV는 있는데 flow 0행"이면 KRX 익명 세션 강등으로 판정.
DEGRADATION_STREAK = 5


def _progress(msg: str) -> None:
    print(f"[{datetime.now(KST).strftime('%m-%d %H:%M:%S')}] {msg}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PRJ-03 pykrx 5년 백필")
    parser.add_argument("--start", type=date.fromisoformat, default=None,
                        help="백필 시작일 (기본: end - 5년)")
    parser.add_argument("--end", type=date.fromisoformat, default=None,
                        help="백필 종료일 (기본: 오늘 KST)")
    parser.add_argument("--symbols", default=None,
                        help="쉼표 구분 종목 서브셋 (유니버스 스냅샷 생략)")
    parser.add_argument("--limit", type=int, default=None,
                        help="대기 종목 중 앞 N개만 처리 (파일럿)")
    parser.add_argument("--phase", choices=("all", "market", "flows", "ohlcv", "report"),
                        default="all", help="실행 범위 (report = DB 커버리지 리포트만)")
    parser.add_argument("--checkpoint", type=Path, default=Path(DEFAULT_CHECKPOINT))
    parser.add_argument("--reset-checkpoint", action="store_true",
                        help="체크포인트 삭제 후 처음부터")
    parser.add_argument("--retry-failed", action="store_true",
                        help="이전 실행 실패 종목을 대기열로 복귀")
    parser.add_argument("--resume-from-db", action="store_true",
                        help="체크포인트 유실 시 DB에서 done 셋 복원 (flow 축 기준)")
    parser.add_argument("--dry-run", action="store_true",
                        help="수집·변환만 수행, DB 쓰기·체크포인트 저장 없음")
    parser.add_argument("--snapshot-freq-days", type=int, default=30,
                        help="point-in-time 유니버스 스냅샷 간격(일)")
    parser.add_argument("--sleep", type=float, default=SLEEP_SEC,
                        help="KRX 콜 간 페이싱(초)")
    parser.add_argument("--stop-file", type=Path, default=None,
                        help="존재하면 우아하게 중단 (기본: <체크포인트 폴더>/STOP)")
    parser.add_argument("--max-consecutive-failures", type=int, default=10)
    return parser.parse_args()


# ── 시장 단위 페이즈 ──────────────────────────────────────────────────


async def run_market_phase(
    sf: Any, cp: Checkpoint, args: argparse.Namespace, start: date, end: date
) -> None:
    for market in INDEX_TICKERS:
        _progress(f"시장 페이즈: {market} 수급+지수 5년")
        frames = await asyncio.to_thread(
            fetch_market_frames, market, start, end, sleep_sec=args.sleep
        )
        rows = build_market_rows(market, frames, start)
        if market == "kospi":
            cp.expected_trading_days = len(rows)
        if args.dry_run:
            _progress(f"[dry-run] {market}: {len(rows)}행 (샘플: {rows[:1]})")
            continue
        n = await upsert_rows(
            sf,
            model=MarketInvestorFlowDaily,
            constraint="uq_market_investor_flow_daily_market_date",
            rows=rows,
            batch_size=MARKET_BATCH,
            mode="update_unless_kis",
            source=FLOW_SOURCE,
            update_cols=MARKET_UPDATE_COLS,
        )
        _progress(f"시장 페이즈: {market} rows={len(rows)} upserted={n}")
    if not args.dry_run:
        cp.market_done = True
        save_checkpoint(args.checkpoint, cp)


# ── 종목 페이즈 ───────────────────────────────────────────────────────


async def process_symbol(
    sf: Any, symbol: str, args: argparse.Namespace, start: date, end: date
) -> dict[str, Any]:
    """한 종목의 flow/ohlcv 수집→변환→upsert. 반환: 체크포인트 done 정보."""
    info: dict[str, Any] = {}
    if args.phase in ("all", "flows"):
        frames = await asyncio.to_thread(
            fetch_symbol_flow_frames, symbol, start, end, sleep_sec=args.sleep
        )
        flow_rows = build_flow_rows(symbol, frames)
        info["flow_rows"] = len(flow_rows)
        if args.dry_run:
            sample = flow_rows[:1]
            _progress(f"[dry-run] {symbol} flow {len(flow_rows)}행 (샘플: {sample})")
        else:
            info["flow_upserted"] = await upsert_rows(
                sf,
                model=InvestorFlowDaily,
                constraint="uq_investor_flow_daily_symbol_date",
                rows=flow_rows,
                batch_size=FLOW_BATCH,
                mode="update_unless_kis",
                source=FLOW_SOURCE,
                update_cols=FLOW_UPDATE_COLS,
            )
    if args.phase in ("all", "ohlcv"):
        df_ohlcv, df_cap = await asyncio.to_thread(
            fetch_symbol_ohlcv_frames, symbol, start, end, sleep_sec=args.sleep
        )
        ohlcv_rows = build_ohlcv_rows(symbol, df_ohlcv, df_cap)
        info["ohlcv_rows"] = len(ohlcv_rows)
        if args.dry_run:
            _progress(f"[dry-run] {symbol} ohlcv {len(ohlcv_rows)}행 (샘플: {ohlcv_rows[:1]})")
        else:
            # DO NOTHING이라 rowcount = 신규 삽입 수(기존 KIS 행 스킵 제외)
            info["ohlcv_inserted"] = await upsert_rows(
                sf,
                model=DailyOHLCV,
                constraint="uq_daily_ohlcv_symbol_date",
                rows=ohlcv_rows,
                batch_size=OHLCV_BATCH,
                mode="do_nothing",
            )
    if info.get("flow_rows", 0) == 0 and info.get("ohlcv_rows", 0) == 0:
        info["note"] = "no_data"  # 창 이전 상폐 등 — 실패 아님
    return info


def _sanity_check(info: dict[str, Any], expected: int | None, symbol: str) -> bool:
    """파일럿 sanity — 상장 유지 종목 1개에서 flow 행수/거래일수 비율 검사.

    반환: 이 종목으로 판정을 수행했는지(True면 이후 재검사 불필요).
    """
    if not expected or "flow_rows" not in info or "ohlcv_rows" not in info:
        return False
    if info["ohlcv_rows"] < expected * SANITY_MIN_RATIO:
        return False  # 창 중간 상장/상폐 종목 — 판정 부적합, 다음 종목에서 재시도
    if info["flow_rows"] < expected * SANITY_MIN_RATIO:
        raise RuntimeError(
            f"파일럿 sanity 실패: {symbol} flow {info['flow_rows']}행 < 거래일 {expected}의 "
            f"{SANITY_MIN_RATIO:.0%} — KRX 행 캡 또는 익명 세션 강등 의심. 중단합니다."
        )
    _progress(
        f"파일럿 sanity 통과: {symbol} flow={info['flow_rows']} "
        f"ohlcv={info['ohlcv_rows']} expected≈{expected}"
    )
    return True


async def run_symbol_phase(
    sf: Any,
    cp: Checkpoint,
    run_symbols: list[str],
    args: argparse.Namespace,
    start: date,
    end: date,
    stop_file: Path,
) -> None:
    total = len(run_symbols)
    t0 = time.monotonic()
    consecutive_failures = 0
    degradation_streak = 0
    sanity_done = False

    for i, symbol in enumerate(run_symbols, 1):
        if stop_file.exists():
            _progress(f"STOP 파일 감지 — {i - 1}/{total}에서 우아하게 중단 (재실행 시 재개)")
            break
        try:
            t_sym = time.monotonic()
            info = await process_symbol(sf, symbol, args, start, end)

            if not sanity_done:
                sanity_done = _sanity_check(info, cp.expected_trading_days, symbol)

            # 익명 세션 강등 감지: OHLCV(네이버)는 나오는데 flow(KRX)만 연속 0행
            if info.get("flow_rows") == 0 and info.get("ohlcv_rows", 0) > 0:
                degradation_streak += 1
                if degradation_streak >= DEGRADATION_STREAK:
                    raise KrxAuthError(
                        f"연속 {degradation_streak}종목 flow 0행(OHLCV는 정상) — "
                        "KRX 익명 세션 강등 의심. 중단합니다."
                    )
            elif info.get("flow_rows", 0) > 0:
                degradation_streak = 0

            consecutive_failures = 0
            if not args.dry_run:
                cp.mark_done(symbol, info)
                save_checkpoint(args.checkpoint, cp)

            elapsed = time.monotonic() - t0
            eta_min = (elapsed / i) * (total - i) / 60
            _progress(
                f"[{i}/{total}] {symbol} flow={info.get('flow_rows', '-')} "
                f"ohlcv={info.get('ohlcv_rows', '-')} t={time.monotonic() - t_sym:.1f}s "
                f"eta={eta_min / 60:.1f}h fail={len(cp.failed)}"
            )
        except KrxAuthError:
            if not args.dry_run:
                save_checkpoint(args.checkpoint, cp)
            raise
        except Exception as exc:  # noqa: BLE001 — 종목 단위 격리, 연속 실패만 중단
            consecutive_failures += 1
            _progress(f"[{i}/{total}] {symbol} 실패({type(exc).__name__}: {exc}) "
                      f"연속 {consecutive_failures}회")
            if not args.dry_run:
                cp.mark_failed(symbol, f"{type(exc).__name__}: {exc}")
                save_checkpoint(args.checkpoint, cp)
            if consecutive_failures >= args.max_consecutive_failures:
                raise RuntimeError(
                    f"연속 {consecutive_failures}종목 실패 — KRX 차단/네트워크 장애 의심. 중단."
                ) from exc


# ── DB 복원·리포트 ────────────────────────────────────────────────────


async def seed_done_from_db(sf: Any, cp: Checkpoint, start: date, end: date) -> int:
    """체크포인트 유실 복구 — krx flow 행이 있는 종목을 done으로 시드(보수적)."""
    async with sf() as session:
        result = await session.execute(
            text(
                "SELECT symbol, count(*) FROM investor_flow_daily "
                "WHERE source = 'krx' AND date BETWEEN :s AND :e GROUP BY symbol"
            ),
            {"s": start, "e": end},
        )
        seeded = 0
        for symbol, n in result.all():
            if symbol not in cp.done:
                cp.mark_done(symbol, {"flow_rows": n, "note": "seeded_from_db"})
                seeded += 1
    return seeded


async def print_final_report(
    sf: Any, cp: Checkpoint, args: argparse.Namespace, elapsed_sec: float
) -> None:
    print("# PRJ-03 단계 4 — pykrx 5년 백필 리포트")
    print(f"\n- 실행 시각: {datetime.now(KST).isoformat(timespec='seconds')} (KST)")
    print(f"- 창: {cp.start} ~ {cp.end} / phase={args.phase} / sleep={args.sleep}s")
    print(f"- 유니버스: {len(cp.universe)}종목 "
          "(월간 스냅샷 합집합 — 한 간격 내 상장+상폐 누락 감수)")
    no_data = sum(1 for v in cp.done.values() if v.get("note") == "no_data")
    print(f"- 완료 {len(cp.done)} / 실패 {len(cp.failed)} / no_data {no_data} "
          f"/ 소요 {elapsed_sec / 3600:.1f}h")
    if cp.failed:
        print("\n## 실패 종목 (최대 50)\n")
        for symbol, err in list(cp.failed.items())[:50]:
            print(f"- `{symbol}`: {err}")

    print("\n## DB 커버리지\n")
    async with sf() as session:
        for title, sql in (
            (
                "investor_flow_daily (source별)",
                "SELECT source, count(*), min(date), max(date) "
                "FROM investor_flow_daily GROUP BY source ORDER BY source",
            ),
            (
                "investor_flow_daily 연도별 (krx)",
                "SELECT extract(year FROM date)::int AS y, count(*) "
                "FROM investor_flow_daily WHERE source = 'krx' GROUP BY y ORDER BY y",
            ),
            (
                "market_investor_flow_daily",
                "SELECT source, market, count(*), min(date), max(date) "
                "FROM market_investor_flow_daily GROUP BY source, market ORDER BY source, market",
            ),
            (
                "daily_ohlcv",
                "SELECT count(*), min(date), max(date) FROM daily_ohlcv",
            ),
            (
                "겹침 스팟체크 (최근 40일 flow — kis 행 무손상 확인)",
                "SELECT source, count(*) FROM investor_flow_daily "
                "WHERE date > current_date - 40 GROUP BY source ORDER BY source",
            ),
        ):
            result = await session.execute(text(sql))
            print(f"### {title}\n")
            for row in result.all():
                print(f"- {tuple(row)}")
            print()


# ── 메인 ──────────────────────────────────────────────────────────────


async def run(args: argparse.Namespace) -> None:
    end = args.end or datetime.now(KST).date()
    start = args.start or end - timedelta(days=5 * 365)
    t0 = time.monotonic()

    # 자격 게이트 — pykrx는 갱신 실패 시 익명 강등되므로 시작 전에 명시 확인
    if not (os.getenv("KRX_ID") and os.getenv("KRX_PW")):
        print("오류: KRX_ID/KRX_PW 환경변수가 없습니다 (.env 확인).", file=sys.stderr)
        sys.exit(2)

    await init_db(get_settings())
    sf = get_session_factory()
    try:
        # 체크포인트
        if args.reset_checkpoint and args.checkpoint.exists():
            args.checkpoint.unlink()
            _progress("체크포인트 초기화")
        cp = load_checkpoint(args.checkpoint)
        if cp is not None and (cp.start, cp.end) != (start.isoformat(), end.isoformat()):
            print(
                f"오류: 체크포인트 창({cp.start}~{cp.end})과 요청 창({start}~{end}) 불일치 — "
                "--reset-checkpoint 또는 다른 --checkpoint 경로를 쓰세요.",
                file=sys.stderr,
            )
            sys.exit(2)
        if cp is None:
            cp = Checkpoint(start=start.isoformat(), end=end.isoformat())

        if args.phase == "report":
            await print_final_report(sf, cp, args, time.monotonic() - t0)
            return

        # 기동 게이트 — 로그인 세션으로 티커 목록이 정상 조회되는지
        from pykrx import stock as _stock_mod  # noqa: PLC0415 — 무거운 임포트 지연

        tickers_today = await asyncio.to_thread(
            krx_call, _stock_mod.get_market_ticker_list,
            end.strftime("%Y%m%d"), market="ALL", sleep_sec=args.sleep,
        )
        if len(tickers_today) < 2000:
            raise KrxAuthError(
                f"기동 게이트 실패: 티커 {len(tickers_today)}개(<2000) — KRX 로그인 상태 의심"
            )
        _progress(f"기동 게이트 통과: 현재 상장 {len(tickers_today)}종목")

        # STOP 파일 잔재 정리
        stop_file = args.stop_file or args.checkpoint.parent / "STOP"
        if stop_file.exists():
            stop_file.unlink()
            _progress("이전 STOP 파일 제거")

        # 유니버스
        if args.symbols:
            requested = [s.strip() for s in args.symbols.split(",") if s.strip()]
            cp.universe = sorted(set(cp.universe) | set(requested))
            run_symbols = [s for s in requested if s not in cp.done]
        else:
            if not cp.universe:
                _progress("point-in-time 유니버스 구성 중 (~1분)")
                cp.universe, snapshots = await asyncio.to_thread(
                    build_universe, start, end,
                    freq_days=args.snapshot_freq_days, sleep_sec=args.sleep,
                )
                _progress(f"유니버스 {len(cp.universe)}종목 (스냅샷 {len(snapshots)}개)")
                if not args.dry_run:
                    save_checkpoint(args.checkpoint, cp)
            if args.resume_from_db:
                seeded = await seed_done_from_db(sf, cp, start, end)
                _progress(f"DB에서 done {seeded}종목 시드")
                if not args.dry_run:
                    save_checkpoint(args.checkpoint, cp)
            run_symbols = cp.pending(retry_failed=args.retry_failed)
        if args.limit:
            run_symbols = run_symbols[: args.limit]

        # 시장 페이즈 (선행 — expected_trading_days 확보)
        if args.phase in ("all", "market") and (not cp.market_done or args.phase == "market"):
            await run_market_phase(sf, cp, args, start, end)

        # 종목 페이즈
        if args.phase != "market":
            _progress(f"종목 페이즈 시작: 대기 {len(run_symbols)}종목")
            await run_symbol_phase(sf, cp, run_symbols, args, start, end, stop_file)

        if not args.dry_run:
            await print_final_report(sf, cp, args, time.monotonic() - t0)
    finally:
        await close_db()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
