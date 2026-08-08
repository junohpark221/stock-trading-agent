"""PRJ-04 1단계 — `PositionManager.merge_or_create` 실 PostgreSQL 검증 (EC2 실행).

단위 테스트(`tests/test_position_merge.py` 19건)는 `_FakeSession` 스텁 기반이라
**실 DB에서만 드러나는 영역**이 안 덮인다:

- `SELECT ... ORDER BY ... LIMIT 1 FOR UPDATE` 의 실제 행 잠금·직렬화
- 병합 UPDATE의 `Numeric(15,2)` · `JSONB` · `ARRAY(String)` 영속화
- 동시 INSERT 경합 → asyncpg 유니크 위반이 SQLAlchemy `IntegrityError` 로 올라와
  `merge_or_create` 의 1회 재시도 경로가 타는지

이 경로가 라이브에서 처음 실행되는 순간은 **실제 추가매수의 체결 확정**이고, 거기서
실패하면 브로커엔 체결이 있는데 DB엔 포지션이 없는 상태(F-31류 정합성 사고)가 된다.
그래서 장 전에 실 DB에서 확인한다. 병합 로직을 고칠 때마다 재실행하는 회귀 도구다.

**운영 DB에는 쓰지 않는다.** 같은 PostgreSQL 인스턴스에 일회용 스크래치 DB를 만들어
(`alembic upgrade head` 로 동일 스키마) 거기서 전부 돌리고 끝나면 DROP 한다.
`merge_or_create` 는 내부에서 커밋하므로 트랜잭션 롤백으로 감쌀 수 없고, 운영 DB에
sentinel 포지션을 만들면 스크립트가 중간에 죽었을 때 고아 open 행이 남아 장중에
폴링 청산·WS·reconciler 가 실보유로 취급한다.

실행 (EC2):

    docker compose -f docker-compose.prod.yml exec -T app \
        python scripts/verify_position_merge.py \
        > verify_merge.md 2> verify_merge.log

stdout = 마크다운 리포트 / stderr = 로그. exit 0=PASS, 1=FAIL, 2=환경/인자 오류.
`--keep` 로 스크래치 DB 보존(디버깅), `--recreate` 로 잔존 스크래치 DB 재생성.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from probe_common import kv, report_header, section, setup_probe_logging
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.core.enums import ExitReason
from src.db.models.account import Account
from src.db.models.strategy import PositionRecord
from src.strategy.position_manager import PositionManager

SCRATCH_SUFFIX = "_prj04_verify"
ACCOUNT_ID = "verify"
APP_DIR = "/app"

# 경합 테스트에서 상대 트랜잭션이 커밋될 때까지 기다리는 상한(초).
RACE_TIMEOUT_SEC = 20.0


# ── 결과 수집 ────────────────────────────────────────────────────────


@dataclass
class Case:
    """검증 케이스 1건의 결과. 개별 assert 실패를 모아 리포트 행으로 낸다."""

    name: str
    checks: list[tuple[bool, str, str]] = field(default_factory=list)
    error: str | None = None

    def check(self, label: str, actual: Any, expected: Any) -> None:
        self.checks.append((actual == expected, label, f"actual={actual!r} expected={expected!r}"))

    def check_true(self, label: str, cond: bool, detail: str = "") -> None:
        self.checks.append((bool(cond), label, detail))

    @property
    def ok(self) -> bool:
        return self.error is None and all(c[0] for c in self.checks)


class Report:
    def __init__(self) -> None:
        self.cases: list[Case] = []

    def case(self, name: str) -> Case:
        c = Case(name)
        self.cases.append(c)
        return c

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.cases)

    def render(self) -> None:
        section("결과 요약")
        print("| 케이스 | 결과 | 체크 |")
        print("|--------|------|------|")
        for c in self.cases:
            status = "✅ PASS" if c.ok else "❌ FAIL"
            passed = sum(1 for k, _, _ in c.checks if k)
            print(f"| {c.name} | {status} | {passed}/{len(c.checks)} |")

        section("상세")
        for c in self.cases:
            print(f"\n### {c.name} — {'PASS' if c.ok else 'FAIL'}\n")
            if c.error:
                print(f"```\n{c.error}\n```")
            for ok, label, detail in c.checks:
                mark = "✅" if ok else "❌"
                suffix = f" — {detail}" if (detail and not ok) else ""
                print(f"- {mark} {label}{suffix}")


# ── 스크래치 DB 생명주기 ─────────────────────────────────────────────


def _swap_db_name(url: str, new_name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{new_name}", parts.query, parts.fragment))


def _db_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


async def _admin_execute(admin_url: str, sql: str) -> None:
    """CREATE/DROP DATABASE 는 트랜잭션 밖에서만 가능 → AUTOCOMMIT 엔진."""
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


async def _database_exists(admin_url: str, name: str) -> bool:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            )
            return result.scalar_one_or_none() is not None
    finally:
        await engine.dispose()


def _run_migrations(scratch_url: str) -> None:
    """entrypoint.sh 와 같은 `alembic upgrade head` 로 스크래치 DB 스키마를 만든다.

    alembic/env.py 가 `get_settings().DATABASE_URL` 을 읽고, 환경변수가 .env 보다
    우선하므로 서브프로세스 환경만 갈아끼우면 된다(마이그레이션 아티팩트 자체를 검증).
    """
    env = {**os.environ, "DATABASE_URL": scratch_url}
    cwd = APP_DIR if os.path.isdir(APP_DIR) else os.getcwd()
    proc = subprocess.run(  # noqa: S603
        ["alembic", "upgrade", "head"],  # noqa: S607
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head 실패 (rc={proc.returncode})\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    print(proc.stdout, file=sys.stderr)
    print(proc.stderr, file=sys.stderr)


async def _seed_account(factory: async_sessionmaker[AsyncSession]) -> None:
    """positions.account_id 의 FK(accounts.id) 충족용 행 1건."""
    async with factory() as session:
        session.add(Account(id=ACCOUNT_ID, nickname="prj04-verify", is_active=False))
        await session.commit()


# ── 조회 헬퍼 ────────────────────────────────────────────────────────


async def _fetch(
    factory: async_sessionmaker[AsyncSession], symbol: str, *, status: str | None = "open"
) -> list[PositionRecord]:
    """DB 재조회 — ORM 아이덴티티 맵을 거치지 않는 새 세션으로 영속 상태를 본다."""
    async with factory() as session:
        stmt = select(PositionRecord).where(
            PositionRecord.account_id == ACCOUNT_ID,
            PositionRecord.symbol == symbol,
        )
        if status is not None:
            stmt = stmt.where(PositionRecord.status == status)
        result = await session.execute(stmt.order_by(PositionRecord.id.asc()))
        return list(result.scalars().all())


def _d(value: str) -> Decimal:
    return Decimal(value)


# ── 검증 케이스 ──────────────────────────────────────────────────────


async def case_create_and_merge(
    pm: PositionManager, factory: async_sessionmaker[AsyncSession], rpt: Report
) -> None:
    """T1·T2·T3 — 신규 생성 → 추가매수 병합 → 손절·익절 폭 재적용."""
    symbol = "VRF001"

    c1 = rpt.case("T1 신규 생성")
    rec, merged = await pm.merge_or_create(
        symbol=symbol,
        strategy_type="swing",
        quantity=10,
        entry_price=_d("10000"),
        stop_loss_price=_d("9500"),
        take_profit_price=_d("11000"),
        account_id=ACCOUNT_ID,
    )
    c1.check("merged", merged, False)
    rows = await _fetch(factory, symbol)
    c1.check("open 행수", len(rows), 1)
    c1.check("quantity", rows[0].quantity, 10)
    c1.check("avg_cost", str(rows[0].avg_cost), "10000.00")
    c1.check("entry_price", str(rows[0].entry_price), "10000.00")
    c1.check_true("avg_cost 가 Decimal", isinstance(rows[0].avg_cost, Decimal))
    first_id = rec.id

    # 추가매수: 5주 @13,000 (손절 -5% = 12,350 / 익절 +10% = 14,300)
    c2 = rpt.case("T2 추가매수 병합")
    rec2, merged2 = await pm.merge_or_create(
        symbol=symbol,
        strategy_type="swing",
        quantity=5,
        entry_price=_d("13000"),
        stop_loss_price=_d("12350"),
        take_profit_price=_d("14300"),
        account_id=ACCOUNT_ID,
    )
    c2.check("merged", merged2, True)
    c2.check("같은 id 유지", rec2.id, first_id)
    rows = await _fetch(factory, symbol)
    c2.check("open 행수", len(rows), 1)
    row = rows[0]
    c2.check("quantity 합산", row.quantity, 15)
    # (10,000×10 + 13,000×5) / 15 = 11,000
    c2.check("avg_cost 가중평균", str(row.avg_cost), "11000.00")
    c2.check("entry_price 불변(최초 체결가)", str(row.entry_price), "10000.00")
    c2.check("entry_date 리셋", row.entry_date, date.today())
    c2.check("highest_price 리셋", row.highest_price, None)

    c3 = rpt.case("T3 손절·익절 재적용 (이번 주문 폭)")
    # 이번 주문 폭 = 손절 -5% / 익절 +10% → 새 평단 11,000 에 재적용
    c3.check("stop_loss_price", str(row.stop_loss_price), "10450.00")
    c3.check("take_profit_price", str(row.take_profit_price), "12100.00")


async def case_width_fallback(
    pm: PositionManager, factory: async_sessionmaker[AsyncSession], rpt: Report
) -> None:
    """T3-b — 이번 주문 폭이 비정상이면 기존 포지션 폭을 새 평단에 재적용."""
    symbol = "VRF002"
    c = rpt.case("T3-b 폭 폴백 (기존 포지션 폭)")

    await pm.merge_or_create(
        symbol=symbol,
        strategy_type="swing",
        quantity=10,
        entry_price=_d("10000"),
        stop_loss_price=_d("9000"),  # 기존 폭 -10%
        take_profit_price=_d("12000"),  # 기존 폭 +20%
        account_id=ACCOUNT_ID,
    )
    # 이번 주문의 손절가가 체결가보다 높다 = 비정상 → rebase None → 기존 폭 폴백
    await pm.merge_or_create(
        symbol=symbol,
        strategy_type="swing",
        quantity=10,
        entry_price=_d("14000"),
        stop_loss_price=_d("15000"),
        take_profit_price=None,
        account_id=ACCOUNT_ID,
    )
    rows = await _fetch(factory, symbol)
    c.check("open 행수", len(rows), 1)
    row = rows[0]
    c.check("avg_cost", str(row.avg_cost), "12000.00")  # (10,000+14,000)/2
    c.check("stop_loss_price = 새 평단 -10%", str(row.stop_loss_price), "10800.00")
    c.check("take_profit_price = 새 평단 +20%", str(row.take_profit_price), "14400.00")


async def case_jsonb_array_realized(
    pm: PositionManager, factory: async_sessionmaker[AsyncSession], rpt: Report
) -> None:
    """T4 — JSONB 스냅샷 교체 · ARRAY union · 부분청산 realized_pnl 보존.

    F-14 `entry_trigger`는 별도 파라미터가 아니라 **스냅샷 배관**으로 전달된다
    (`merge_or_create`가 `entry_analysis_snapshot["entry_trigger"]`를 꺼내 전용 컬럼으로
    승격 — `position_manager.py:142`). 운영 경로도 동일하다(`jobs.py:601`).
    """
    symbol = "VRF003"
    c = rpt.case("T4 JSONB·ARRAY·realized_pnl 영속")

    snapshot_1 = {
        "action": "buy",
        "confidence": 0.7,
        "entry_trigger": ["rsi_oversold"],
    }
    rec, _ = await pm.merge_or_create(
        symbol=symbol,
        strategy_type="swing",
        quantity=20,
        entry_price=_d("10000"),
        stop_loss_price=_d("9500"),
        take_profit_price=_d("11000"),
        account_id=ACCOUNT_ID,
        entry_analysis_snapshot=snapshot_1,
    )
    rows = await _fetch(factory, symbol)
    c.check(
        "신규 생성 시 entry_trigger 승격",
        list(rows[0].entry_trigger or []),
        ["rsi_oversold"],
    )
    # 부분익절 → realized_pnl 발생 (병합 후에도 보존되어야 함)
    await pm.reduce(
        rec.id,
        exit_quantity=10,
        exit_price=_d("11000"),
        exit_reason=ExitReason.PARTIAL_TAKE_PROFIT,
    )
    rows = await _fetch(factory, symbol)
    realized_before = rows[0].realized_pnl
    c.check("부분청산 realized_pnl", str(realized_before), "10000.00")  # (11,000-10,000)×10

    snapshot_2 = {
        "action": "buy",
        "confidence": 0.9,
        "note": "add",
        "entry_trigger": ["macd_golden_cross", "rsi_oversold"],
    }
    await pm.merge_or_create(
        symbol=symbol,
        strategy_type="swing",
        quantity=10,
        entry_price=_d("12000"),
        stop_loss_price=_d("11400"),
        take_profit_price=_d("13200"),
        account_id=ACCOUNT_ID,
        entry_analysis_snapshot=snapshot_2,
    )
    rows = await _fetch(factory, symbol)
    c.check("open 행수", len(rows), 1)
    row = rows[0]
    c.check("realized_pnl 보존", str(row.realized_pnl), "10000.00")
    c.check("quantity", row.quantity, 20)
    c.check("avg_cost 가중평균", str(row.avg_cost), "11000.00")  # (10,000×10 + 12,000×10)/20
    c.check("snapshot 최신 교체(JSONB 왕복)", row.entry_analysis_snapshot, snapshot_2)
    # 기존 태그 뒤에 신규 태그만 append — 중복 없이 순서 보존
    c.check(
        "entry_trigger union(순서 보존)",
        list(row.entry_trigger or []),
        ["rsi_oversold", "macd_golden_cross"],
    )


async def case_unique_index(
    factory: async_sessionmaker[AsyncSession], rpt: Report
) -> None:
    """T5 — 부분 유니크 인덱스: open 중복은 거부, closed 는 예외."""
    c = rpt.case("T5 유니크 인덱스 (원시 INSERT)")
    base = {
        "account_id": ACCOUNT_ID,
        "symbol": "VRF004",
        "strategy_type": "swing",
        "quantity": 10,
        "avg_cost": _d("10000"),
        "entry_price": _d("10000"),
        "entry_date": date.today(),
        "stop_loss_price": _d("9500"),
    }

    async with factory() as session:
        await session.execute(insert(PositionRecord).values(**base, status="open"))
        await session.commit()

    rejected = False
    async with factory() as session:
        try:
            await session.execute(insert(PositionRecord).values(**base, status="open"))
            await session.commit()
        except IntegrityError:
            rejected = True
            await session.rollback()
    c.check_true("open 중복 INSERT 거부", rejected, "IntegrityError 미발생")

    closed_ok = True
    async with factory() as session:
        try:
            await session.execute(insert(PositionRecord).values(**base, status="closed"))
            await session.commit()
        except IntegrityError:
            closed_ok = False
            await session.rollback()
    c.check_true("closed 행은 제약 대상 밖", closed_ok, "closed INSERT 가 거부됨")


async def case_insert_race(
    pm: PositionManager, factory: async_sessionmaker[AsyncSession], rpt: Report
) -> None:
    """T6 — INSERT 경합 → IntegrityError → 1회 재시도로 상대 행에 병합.

    경합을 우연에 맡기지 않고 결정론적으로 만든다: 다른 세션이 open 행을
    INSERT 한 채 커밋하지 않으면, `merge_or_create` 의 SELECT 는 그 행을 보지 못하고
    (미커밋) INSERT 를 시도하다 유니크 인덱스에서 **블록**된다. 그 상태에서 상대를
    커밋하면 유니크 위반이 확정돼 재시도 경로가 반드시 탄다.
    """
    symbol = "VRF005"
    c = rpt.case("T6 INSERT 경합 → 재시도 병합")

    blocker = factory()  # 커밋 전까지 트랜잭션을 열어둔 채 유지(autobegin)
    await blocker.execute(
        insert(PositionRecord).values(
            account_id=ACCOUNT_ID,
            symbol=symbol,
            strategy_type="swing",
            quantity=10,
            avg_cost=_d("10000"),
            entry_price=_d("10000"),
            entry_date=date.today(),
            stop_loss_price=_d("9500"),
            take_profit_price=_d("11000"),
            status="open",
        )
    )  # 커밋하지 않음 — 아직 다른 트랜잭션에 보이지 않는다

    task = asyncio.create_task(
        pm.merge_or_create(
            symbol=symbol,
            strategy_type="swing",
            quantity=5,
            entry_price=_d("13000"),
            stop_loss_price=_d("12350"),
            take_profit_price=_d("14300"),
            account_id=ACCOUNT_ID,
        )
    )
    try:
        # task 가 INSERT 에서 블록될 때까지 양보한 뒤 상대를 커밋한다.
        await asyncio.sleep(1.0)
        blocked = not task.done()
        c.check_true("상대 커밋 전까지 대기(블록)", blocked, "경합 없이 조기 완료")
        if blocked:
            await blocker.commit()
        else:
            # 경합이 성립하지 않았다 — 상대를 되돌려 상태를 오염시키지 않는다.
            await blocker.rollback()
        _, merged = await asyncio.wait_for(task, timeout=RACE_TIMEOUT_SEC)
        c.check("재시도 후 merged", merged, blocked)
    except TimeoutError:
        c.check_true("재시도 완료", False, f"{RACE_TIMEOUT_SEC}s 내 미완료(교착 의심)")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        await blocker.close()

    rows = await _fetch(factory, symbol)
    c.check("open 행수", len(rows), 1)
    if rows:
        c.check("quantity 합산", rows[0].quantity, 15)
        c.check("avg_cost 가중평균", str(rows[0].avg_cost), "11000.00")


async def case_update_serialization(
    pm: PositionManager, factory: async_sessionmaker[AsyncSession], rpt: Report
) -> None:
    """T7 — 기존 행에 동시 병합 5건: FOR UPDATE 직렬화로 lost update 없음."""
    symbol = "VRF006"
    c = rpt.case("T7 동시 병합 5건 (FOR UPDATE 직렬화)")

    await pm.merge_or_create(
        symbol=symbol,
        strategy_type="swing",
        quantity=10,
        entry_price=_d("10000"),
        stop_loss_price=_d("9500"),
        take_profit_price=_d("11000"),
        account_id=ACCOUNT_ID,
    )

    async def add() -> None:
        await pm.merge_or_create(
            symbol=symbol,
            strategy_type="swing",
            quantity=4,
            entry_price=_d("10000"),
            stop_loss_price=_d("9500"),
            take_profit_price=_d("11000"),
            account_id=ACCOUNT_ID,
        )

    results = await asyncio.gather(*(add() for _ in range(5)), return_exceptions=True)
    failures = [r for r in results if isinstance(r, BaseException)]
    c.check_true("5건 전부 성공", not failures, f"실패 {len(failures)}건: {failures[:2]}")

    rows = await _fetch(factory, symbol)
    c.check("open 행수", len(rows), 1)
    if rows:
        # 10 + 4×5 = 30 — 하나라도 덮어쓰기(lost update)면 30 미만
        c.check("quantity 누적 정확", rows[0].quantity, 30)
        c.check("avg_cost 불변(동일가 매수)", str(rows[0].avg_cost), "10000.00")


async def case_reentry(
    pm: PositionManager, factory: async_sessionmaker[AsyncSession], rpt: Report
) -> None:
    """T8 — 전량 청산 후 재진입: closed 행 보존 + 새 open 행 INSERT."""
    symbol = "VRF007"
    c = rpt.case("T8 청산 후 재진입")

    rec, _ = await pm.merge_or_create(
        symbol=symbol,
        strategy_type="swing",
        quantity=10,
        entry_price=_d("10000"),
        stop_loss_price=_d("9500"),
        take_profit_price=_d("11000"),
        account_id=ACCOUNT_ID,
    )
    await pm.close(rec.id, exit_price=_d("11000"), exit_reason=ExitReason.TAKE_PROFIT)

    rec2, merged2 = await pm.merge_or_create(
        symbol=symbol,
        strategy_type="swing",
        quantity=7,
        entry_price=_d("12000"),
        stop_loss_price=_d("11400"),
        take_profit_price=_d("13200"),
        account_id=ACCOUNT_ID,
    )
    c.check("merged (closed 행에 병합되지 않음)", merged2, False)
    c.check_true("새 id 발급", rec2.id != rec.id, f"{rec2.id} == {rec.id}")

    open_rows = await _fetch(factory, symbol)
    all_rows = await _fetch(factory, symbol, status=None)
    c.check("open 행수", len(open_rows), 1)
    c.check("전체 행수(closed 보존)", len(all_rows), 2)
    if open_rows:
        c.check("신규 open quantity", open_rows[0].quantity, 7)


# ── 엔트리포인트 ─────────────────────────────────────────────────────


async def run_cases(factory: async_sessionmaker[AsyncSession], rpt: Report) -> None:
    """케이스 하나가 죽어도 나머지는 계속 실행한다."""
    pm = PositionManager(factory)  # memory_manager 미주입 = 학습 메모리 쓰기 없음

    runners = [
        ("T1~T3", case_create_and_merge(pm, factory, rpt)),
        ("T3-b", case_width_fallback(pm, factory, rpt)),
        ("T4", case_jsonb_array_realized(pm, factory, rpt)),
        ("T5", case_unique_index(factory, rpt)),
        ("T6", case_insert_race(pm, factory, rpt)),
        ("T7", case_update_serialization(pm, factory, rpt)),
        ("T8", case_reentry(pm, factory, rpt)),
    ]
    for label, coro in runners:
        before = len(rpt.cases)
        try:
            await coro
        except Exception:
            tb = traceback.format_exc()
            if len(rpt.cases) > before:
                rpt.cases[-1].error = tb
            else:
                failed = rpt.case(f"{label} (예외)")
                failed.error = tb
            print(f"[예외] {label}\n{tb}", file=sys.stderr)


async def main() -> int:
    parser = argparse.ArgumentParser(description="PRJ-04 merge_or_create 실DB 검증")
    parser.add_argument("--keep", action="store_true", help="스크래치 DB를 지우지 않는다")
    parser.add_argument("--recreate", action="store_true", help="잔존 스크래치 DB를 재생성")
    args = parser.parse_args()

    setup_probe_logging()
    settings = get_settings()
    prod_url = settings.DATABASE_URL
    prod_name = _db_name(prod_url)
    scratch_name = f"{prod_name}{SCRATCH_SUFFIX}"

    if not prod_name or scratch_name == prod_name:
        print(
            f"[오류] 스크래치 DB 이름을 만들 수 없다: DATABASE_URL db={prod_name!r}",
            file=sys.stderr,
        )
        return 2

    scratch_url = _swap_db_name(prod_url, scratch_name)
    admin_url = _swap_db_name(prod_url, "postgres")

    report_header("PRJ-04 · merge_or_create 실DB 검증")
    kv("운영 DB (읽기·쓰기 모두 없음)", prod_name)
    kv("스크래치 DB", scratch_name)

    if await _database_exists(admin_url, scratch_name):
        if not args.recreate:
            print(
                f"[오류] 스크래치 DB {scratch_name} 가 이미 존재한다. "
                "이전 실행 잔여물일 수 있다 — 내용을 확인하고 --recreate 로 재실행하라.",
                file=sys.stderr,
            )
            return 2
        await _admin_execute(admin_url, f'DROP DATABASE "{scratch_name}" WITH (FORCE)')

    await _admin_execute(admin_url, f'CREATE DATABASE "{scratch_name}"')

    rpt = Report()
    engine = None
    try:
        _run_migrations(scratch_url)
        kv("스키마", "alembic upgrade head 완료")

        engine = create_async_engine(scratch_url, pool_pre_ping=True)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_account(factory)
        await run_cases(factory, rpt)
    except Exception:
        tb = traceback.format_exc()
        c = rpt.case("환경 구성")
        c.error = tb
        print(tb, file=sys.stderr)
    finally:
        if engine is not None:
            await engine.dispose()
        if args.keep:
            kv("정리", f"--keep 지정 — {scratch_name} 보존됨 (수동 DROP 필요)")
        else:
            try:
                await _admin_execute(admin_url, f'DROP DATABASE "{scratch_name}" WITH (FORCE)')
                kv("정리", f"{scratch_name} DROP 완료")
            except Exception as exc:
                kv("정리", f"⚠️ DROP 실패 — 수동 정리 필요: {exc}")

    rpt.render()

    section("판정")
    if rpt.ok:
        kv("결과", "✅ PASS — 실 PG에서 병합·잠금·재시도 경로 정상")
        return 0
    kv("결과", "❌ FAIL — 상세 섹션의 ❌ 항목 확인")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
