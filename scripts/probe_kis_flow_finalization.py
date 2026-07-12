"""PRJ-03 착수 게이트 ④ — KIS 수급 확정치 반영 시각 실측 프로브.

T일 장마감 후 종목별(FHPTJ04160001)·시장단위(FHPTJ04040000) 당일 수급 행이
언제 등장하고 언제까지 값이 바뀌는지 샘플링해 확정 시각을 잡는다
(수집 시각 T일 저녁 + T+1 새벽 이중화 — plan.md 확정 10의 최종 시각 결정 재료).

실행 (운영 EC2, 사용자 SSH — 거래일 15:40 이후 시작 권장):

  [1] T일 저녁 watch 루프 — detach 실행, SSH 끊겨도 지속:
    cd /home/ec2-user/stock-trading-agent
    docker compose -f docker-compose.prod.yml exec -d app sh -c \
        'python scripts/probe_kis_flow_finalization.py --watch \
         > /tmp/flow_watch.log 2>&1'

  [2] T+1 새벽(pre-open, 예: 07~08시) 단발 스냅샷 — 새벽 보정 존재 여부 판정:
    docker compose -f docker-compose.prod.yml exec app \
        python scripts/probe_kis_flow_finalization.py --once

  [3] 결과 수거:
    docker compose -f docker-compose.prod.yml exec app cat /tmp/flow_watch.log
    docker compose -f docker-compose.prod.yml exec app cat /tmp/flow_watch.jsonl

⚠️ 스냅샷 JSONL(/tmp/flow_watch.jsonl)은 컨테이너 파일시스템에 있음 —
   컨테이너 재생성 시 유실되므로 결과 수거 전 docker compose 재기동 금지.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import time as dtime
from pathlib import Path
from typing import Any

from probe_common import (
    build_kis_client,
    close_kis_client,
    fetch_investor_daily,
    fetch_market_investor_daily,
    now_kst,
    prev_business_day,
    recent_business_day,
    setup_probe_logging,
    yyyymmdd,
)

DEFAULT_SYMBOLS = "005930,000660,247540"
DEFAULT_OUT = "/tmp/flow_watch.jsonl"


def _log(msg: str) -> None:
    print(f"[{now_kst().isoformat(timespec='seconds')}] {msg}", flush=True)


def _default_target_date() -> str:
    """KST 15:30 이후면 오늘(T일 저녁 관찰), 이전이면 직전 영업일(T+1 새벽 검증).

    컨테이너는 UTC — 시각·요일 판단은 전부 KST로 명시한다.
    """
    now = now_kst()
    if now.time() >= dtime(15, 30):
        return yyyymmdd(recent_business_day())
    return yyyymmdd(prev_business_day(now.date()))


async def take_snapshot(client, symbols: list[str], target: str) -> dict[str, Any]:
    """target 일자(YYYYMMDD)의 종목별·시장단위 수급 행을 1회 샘플링."""
    snap: dict[str, Any] = {
        "ts": now_kst().isoformat(timespec="seconds"),
        "target_date": target,
        "per_symbol": {},
        "market": {},
    }
    for symbol in symbols:
        try:
            _, rows, _ = await fetch_investor_daily(client, symbol, target, max_pages=1)
            row = next((r for r in rows if r.get("stck_bsop_date") == target), None)
            snap["per_symbol"][symbol] = row
        except Exception as exc:  # noqa: BLE001 — 실패도 기록
            snap["per_symbol"][symbol] = {"_error": f"{type(exc).__name__}: {exc}"}
    for market, sector in (("KSP", "0001"), ("KSQ", "1001")):
        try:
            data = await fetch_market_investor_daily(
                client, market=market, sector_code=sector, anchor_date=target
            )
            rows = data.get("output") or data.get("output1") or []
            if isinstance(rows, dict):
                rows = [rows]
            row = next((r for r in rows if r.get("stck_bsop_date") == target), None)
            snap["market"][market] = row
        except Exception as exc:  # noqa: BLE001
            snap["market"][market] = {"_error": f"{type(exc).__name__}: {exc}"}
    return snap


def _sources(snap: dict[str, Any]) -> dict[str, Any]:
    """스냅샷을 {소스명: 행} 평면 매핑으로 — 비교·요약 공용."""
    out = {f"symbol:{s}": r for s, r in snap.get("per_symbol", {}).items()}
    out.update({f"market:{m}": r for m, r in snap.get("market", {}).items()})
    return out


def diff_snapshots(prev: dict[str, Any] | None, cur: dict[str, Any]) -> list[str]:
    """직전 스냅샷 대비 등장/변경/소멸 이벤트 목록."""
    events: list[str] = []
    prev_sources = _sources(prev) if prev else {}
    for name, row in _sources(cur).items():
        before = prev_sources.get(name)
        has_now = bool(row) and "_error" not in (row or {})
        had_before = bool(before) and "_error" not in (before or {})
        if has_now and not had_before:
            events.append(f"{name}: 당일 행 등장")
        elif has_now and had_before and row != before:
            changed = [
                k for k in row if row.get(k) != before.get(k) and not k.startswith("_")
            ]
            events.append(f"{name}: 값 변경 — {changed}")
        elif not has_now and had_before:
            events.append(f"{name}: 당일 행 소멸(이상)")
    return events


def load_last_snapshot(path: Path, target: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    last = None
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("target_date") == target:
                last = rec
    return last


def append_snapshot(path: Path, snap: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")


def summarize(path: Path, target: str) -> None:
    """target 일자 전체 스냅샷에서 소스별 최초 등장·최종 변경 시각 요약."""
    records: list[dict[str, Any]] = []
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("target_date") == target:
                    records.append(rec)
    _log(f"=== 요약 (target {target}, 스냅샷 {len(records)}개) ===")
    if not records:
        return
    names = _sources(records[0]).keys() | _sources(records[-1]).keys()
    for name in sorted(names):
        first_seen = None
        last_changed = None
        prev_row = None
        for rec in records:
            row = _sources(rec).get(name)
            has = bool(row) and "_error" not in (row or {})
            if has and first_seen is None:
                first_seen = rec["ts"]
            if has and prev_row is not None and row != prev_row:
                last_changed = rec["ts"]
            if has:
                prev_row = row
        _log(
            f"  {name}: 최초 등장 {first_seen or '없음'}"
            f" / 최종 변경 {last_changed or '(등장 후 불변)'}"
        )


async def run_once(client, symbols: list[str], target: str, out: Path) -> None:
    prev = load_last_snapshot(out, target)
    snap = await take_snapshot(client, symbols, target)
    events = diff_snapshots(prev, snap)
    append_snapshot(out, snap)
    for name, row in _sources(snap).items():
        state = "없음" if not row else ("오류" if "_error" in row else "존재")
        _log(f"{name}: 당일({target}) 행 {state}")
    if prev:
        _log(f"직전 스냅샷({prev['ts']}) 대비 이벤트: {events or '변화 없음'}")
    else:
        _log("직전 스냅샷 없음 — 첫 기록")


async def run_watch(
    client, symbols: list[str], target: str, out: Path, interval_min: int, until: dtime
) -> None:
    _log(f"watch 시작 — target {target}, {interval_min}분 간격, KST {until}까지, out={out}")
    prev = load_last_snapshot(out, target)
    while True:
        snap = await take_snapshot(client, symbols, target)
        events = diff_snapshots(prev, snap)
        append_snapshot(out, snap)
        _log(f"스냅샷 기록 — 이벤트: {events or '변화 없음'}")
        prev = snap
        if now_kst().time() >= until:
            break
        await asyncio.sleep(interval_min * 60)
    summarize(out, target)
    _log("watch 종료")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--watch", action="store_true", help="루프 샘플링 (기본 모드)")
    mode.add_argument("--once", action="store_true", help="단발 스냅샷 (T+1 새벽 검증용)")
    mode.add_argument("--summary", action="store_true", help="기존 JSONL 요약만 출력")
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    parser.add_argument("--date", default=None, help="target 일자 YYYYMMDD (기본: 자동 판정)")
    parser.add_argument("--interval-min", type=int, default=15)
    parser.add_argument("--until", default="23:30", help="watch 종료 시각 HH:MM")
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    setup_probe_logging()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    target = args.date or _default_target_date()
    out = Path(args.out)
    until = dtime.fromisoformat(args.until)

    if args.summary:
        summarize(out, target)
        return

    client, redis = await build_kis_client()
    try:
        if args.once:
            await run_once(client, symbols, target, out)
        else:
            await run_watch(client, symbols, target, out, args.interval_min, until)
    finally:
        await close_kis_client(client, redis)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("중단됨", file=sys.stderr)
