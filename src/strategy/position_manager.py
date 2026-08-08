"""PositionManager — positions 테이블 CRUD 관리자.

Strategy.save_position/close_position/get_open_positions를 대체하여
포지션 생성, 청산, 조회, 스톱로스 업데이트 등 DB 오퍼레이션을 캡슐화한다.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.core.enums import ExitReason, StrategyType
from src.core.exceptions import DatabaseError
from src.db.models.strategy import PositionRecord
from src.strategy.exit_calculator import ExitPriceCalculator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.strategy.memory_manager import AgentMemoryManager

logger = structlog.get_logger(__name__)

_Q2 = Decimal("0.01")
_ZERO = Decimal("0")
_ONE = Decimal("1")


def _q2(value: Decimal) -> Decimal:
    """positions 금액 컬럼(Numeric(15,2)) 정밀도로 반올림."""
    return value.quantize(_Q2, rounding=ROUND_HALF_UP)


# ── 오픈 포지션 스냅샷 인덱싱 (PRJ-04 §10) ────────────────────────────────
#
# 알렘빅 018 부분 유니크 인덱스가 `(account_id, symbol) WHERE status='open'` 을
# DB에서 강제하므로 "계좌·종목당 open 1행"은 불변식이다. 아래 헬퍼는 그 전제로
# 스냅샷을 인덱싱하되, 인덱스가 없어지거나 스냅샷이 꼬인 이상 상황에서 조용히
# 임의의 행을 집지 않도록 **최고령 행 승 + warning 로그**로 수렴시킨다.
# (경보·예외는 두지 않는다 — 발생 확률이 사실상 0이고, 청산 잡을 죽이는 편이 더 위험하다.)


def unique_open_positions(
    positions: Iterable[PositionRecord],
    *,
    context: str,
) -> list[PositionRecord]:
    """`(account_id, symbol)` 중복을 제거한 오픈 포지션 스냅샷.

    같은 종목이라도 **계좌가 다르면 별개 포지션**이므로 둘 다 보존한다.
    중복이 발견되면 먼저 온 행(호출자가 `get_open()` 정렬을 유지하면 최고령 행)을
    남기고 나머지는 버리며, 그 사실을 warning으로 남긴다.
    """
    kept: dict[tuple[str, str], PositionRecord] = {}
    dropped: dict[tuple[str, str], list[int]] = {}

    for position in positions:
        key = (position.account_id, position.symbol)
        winner = kept.get(key)
        if winner is None:
            kept[key] = position
        else:
            dropped.setdefault(key, []).append(position.id)

    for (account_id, symbol), dropped_ids in dropped.items():
        logger.warning(
            "position.duplicate_open_snapshot",
            context=context,
            account_id=account_id,
            symbol=symbol,
            kept_id=kept[(account_id, symbol)].id,
            dropped_ids=dropped_ids,
        )

    return list(kept.values())


def open_by_symbol(
    positions: Iterable[PositionRecord],
    *,
    context: str,
) -> dict[str, PositionRecord]:
    """계좌 스코프 오픈 포지션 리스트 → `symbol` 인덱스.

    호출자가 **단일 계좌로 스코프된** 리스트를 넘긴다는 전제다(전 계좌 합집합에는
    `unique_open_positions()` 를 쓸 것 — 계좌가 다른 동일 종목이 서로를 덮는다).
    """
    return {p.symbol: p for p in unique_open_positions(positions, context=context)}


class PositionManager:
    """positions 테이블 CRUD 관리자.

    포지션 생성, 청산, 조회, 스톱로스 업데이트 등 DB 오퍼레이션을 캡슐화.
    Strategy.save_position/close_position/get_open_positions를 대체한다.

    memory_manager가 주입되면, 포지션이 전량 청산될 때(close 또는 reduce 전량)
    학습 메모리(record_trade_outcome)를 best-effort로 기록한다 — 인라인 체결
    (executor)·비동기 reconcile(fill_finalizer) 어느 경로로 닫혀도 단일 funnel에서
    교훈이 누락 없이 적재되도록.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        memory_manager: AgentMemoryManager | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._memory_manager = memory_manager

    # ── Create ────────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        symbol: str,
        strategy_type: str,
        quantity: int,
        entry_price: Decimal,
        stop_loss_price: Decimal,
        take_profit_price: Decimal | None = None,
        trailing_stop_pct: Decimal | None = None,
        max_holding_days: int | None = None,
        entry_session_id: UUID | None = None,
        account_id: str = "default",
        entry_analysis_snapshot: dict | None = None,
    ) -> PositionRecord:
        """포지션 생성 — **동일 종목 open 포지션이 있으면 병합**(PRJ-04).

        `merge_or_create()`에 위임하는 호환 래퍼다. 부분 유니크 인덱스
        `(account_id, symbol) WHERE status='open'` 하에서 무조건 INSERT는 충돌하므로
        모든 진입 확정 경로는 병합 경유여야 한다. 병합 여부가 필요한 호출자는
        `merge_or_create()`를 직접 쓴다.
        """
        record, _ = await self.merge_or_create(
            symbol=symbol,
            strategy_type=strategy_type,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            trailing_stop_pct=trailing_stop_pct,
            max_holding_days=max_holding_days,
            entry_session_id=entry_session_id,
            account_id=account_id,
            entry_analysis_snapshot=entry_analysis_snapshot,
        )
        return record

    async def merge_or_create(
        self,
        *,
        symbol: str,
        strategy_type: str,
        quantity: int,
        entry_price: Decimal,
        stop_loss_price: Decimal,
        take_profit_price: Decimal | None = None,
        trailing_stop_pct: Decimal | None = None,
        max_holding_days: int | None = None,
        entry_session_id: UUID | None = None,
        account_id: str = "default",
        entry_analysis_snapshot: dict | None = None,
    ) -> tuple[PositionRecord, bool]:
        """MTS식 포지션 병합 (PRJ-04 §1) — 종목당 open 포지션 1행을 유지한다.

        동일 `(account_id, symbol)`의 open 포지션이 있으면 **가중평균으로 병합
        (UPDATE)** 하고, 없으면 신규 INSERT한다. 브로커(KIS)는 이미 종목당 1행
        (`pchs_avg_pric`) 모델이므로 내부 모델을 여기에 맞춘다. 체결별 이력은
        orders/executions에 그대로 남아 감사 추적은 잃지 않는다.

        병합 시 필드 처리:

        - `quantity` 합산 / `avg_cost` 가중평균 / `entry_price` 불변(최초 체결가 기록)
        - 손절·익절: **이번 주문의 폭(%)** 을 새 평단에 재적용(§4). 폭이 없거나
          비정상이면 **기존 포지션 폭**을 새 평단에 재적용(폴백)
        - `entry_date`·`max_holding_days` 리셋(§7) — 추가매수 시점부터 보유 시계 재기산
        - `highest_price` 리셋, 익절가 복원 → 트레일링 전환 상태 해제(§5)
        - `entry_analysis_snapshot`·`entry_session_id`는 최신 분석으로 교체(§6)
        - `entry_trigger`는 union(F-14 성과 귀인 보존), `realized_pnl`은 보존

        동시성(§2): 기존 행 병합은 `SELECT ... FOR UPDATE` 행 잠금으로 직렬화하고,
        양쪽 다 "행 없음"으로 판단하는 INSERT 경합만 유니크 인덱스가 막는다
        (PG는 미존재 행에 갭 잠금이 없다). 충돌 시 1회 재조회해 병합으로 착지한다.

        Returns
        -------
        (record, merged) — merged=True면 기존 행에 병합됨
        """
        # F-14: 진입 트리거 태그(스윙 기술 셋업)를 스냅샷 전송 배관에서 꺼내 전용
        # 컬럼으로 승격. realized_pnl과 조인한 트리거별 성과 귀인용(관측용 주석, 게이트 아님).
        entry_trigger = (entry_analysis_snapshot or {}).get("entry_trigger") or None

        kwargs = {
            "symbol": symbol,
            "strategy_type": strategy_type,
            "quantity": quantity,
            "entry_price": entry_price,
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
            "trailing_stop_pct": trailing_stop_pct,
            "max_holding_days": max_holding_days,
            "entry_session_id": entry_session_id,
            "account_id": account_id,
            "entry_analysis_snapshot": entry_analysis_snapshot,
            "entry_trigger": entry_trigger,
        }

        try:
            record, merged = await self._merge_or_create_once(**kwargs)  # type: ignore[arg-type]
        except IntegrityError:
            # 유니크 인덱스 충돌 = 동시 INSERT 경합에서 진 쪽. 상대가 만든 행에 병합한다.
            logger.warning(
                "position.merge_insert_conflict_retry",
                symbol=symbol, account_id=account_id,
            )
            try:
                record, merged = await self._merge_or_create_once(**kwargs)  # type: ignore[arg-type]
            except Exception as exc:
                raise DatabaseError(f"Position merge retry failed: {exc}") from exc
        except Exception as exc:
            raise DatabaseError(f"Position create failed: {exc}") from exc

        if merged:
            logger.info(
                "position.merged",
                id=record.id,
                symbol=record.symbol,
                added_quantity=quantity,
                total_quantity=record.quantity,
                new_avg_cost=str(record.avg_cost),
                account_id=account_id,
            )
        else:
            logger.info(
                "position.created",
                id=record.id,
                symbol=record.symbol,
                quantity=record.quantity,
                strategy=record.strategy_type,
                account_id=account_id,
            )
        return record, merged

    async def _merge_or_create_once(
        self,
        *,
        symbol: str,
        strategy_type: str,
        quantity: int,
        entry_price: Decimal,
        stop_loss_price: Decimal,
        take_profit_price: Decimal | None,
        trailing_stop_pct: Decimal | None,
        max_holding_days: int | None,
        entry_session_id: UUID | None,
        account_id: str,
        entry_analysis_snapshot: dict | None,
        entry_trigger: list[str] | None,
    ) -> tuple[PositionRecord, bool]:
        """병합/생성 1회 시도. IntegrityError는 호출자가 재시도하도록 그대로 올린다."""
        async with self._session_factory() as session:
            stmt = (
                select(PositionRecord)
                .where(
                    PositionRecord.account_id == account_id,
                    PositionRecord.symbol == symbol,
                    PositionRecord.status == "open",
                )
                # 유니크 인덱스 도입 전 잔존 중복 행에서도 결정론적으로 최고령 행 선택.
                .order_by(PositionRecord.entry_date.asc(), PositionRecord.id.asc())
                .limit(1)
                .with_for_update()
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()

            if existing is None:
                record = PositionRecord(
                    symbol=symbol,
                    strategy_type=strategy_type,
                    quantity=quantity,
                    avg_cost=entry_price,
                    entry_price=entry_price,
                    entry_date=date.today(),
                    stop_loss_price=stop_loss_price,
                    take_profit_price=take_profit_price,
                    trailing_stop_pct=trailing_stop_pct,
                    max_holding_days=max_holding_days,
                    status="open",
                    entry_session_id=entry_session_id,
                    account_id=account_id,
                    entry_analysis_snapshot=entry_analysis_snapshot,
                    entry_trigger=entry_trigger,
                )
                session.add(record)
                merged = False
            else:
                self._apply_merge(
                    existing,
                    strategy_type=strategy_type,
                    fill_quantity=quantity,
                    fill_price=entry_price,
                    stop_loss_price=stop_loss_price,
                    take_profit_price=take_profit_price,
                    trailing_stop_pct=trailing_stop_pct,
                    max_holding_days=max_holding_days,
                    entry_session_id=entry_session_id,
                    entry_analysis_snapshot=entry_analysis_snapshot,
                    entry_trigger=entry_trigger,
                )
                record = existing
                merged = True

            await session.commit()
            await session.refresh(record)
        return record, merged

    @staticmethod
    def _apply_merge(
        record: PositionRecord,
        *,
        strategy_type: str,
        fill_quantity: int,
        fill_price: Decimal,
        stop_loss_price: Decimal,
        take_profit_price: Decimal | None,
        trailing_stop_pct: Decimal | None,
        max_holding_days: int | None,
        entry_session_id: UUID | None,
        entry_analysis_snapshot: dict | None,
        entry_trigger: list[str] | None,
    ) -> None:
        """기존 open 포지션에 추가매수 체결을 병합(in-place). 커밋은 호출자 몫."""
        if record.strategy_type != strategy_type:
            logger.warning(
                "position.merge_strategy_mismatch",
                id=record.id, symbol=record.symbol,
                existing=record.strategy_type, incoming=strategy_type,
            )

        old_qty = record.quantity
        old_avg = record.avg_cost
        new_qty = old_qty + fill_quantity
        new_avg = (
            _q2((old_avg * old_qty + fill_price * fill_quantity) / new_qty)
            if new_qty > 0
            else old_avg
        )

        # 손절·익절: 이번 주문 폭 우선(전략 공식 최신 산출값), 실패 시 기존 포지션 폭.
        rebased = ExitPriceCalculator.rebase(
            reference_price=fill_price,
            stop0=stop_loss_price,
            tp0=take_profit_price,
            target_price=new_avg,
        )
        if rebased is None:
            rebased = ExitPriceCalculator.rebase(
                reference_price=old_avg,
                stop0=record.stop_loss_price,
                tp0=record.take_profit_price,
                target_price=new_avg,
            )
        if rebased is not None:
            new_stop, new_tp = rebased
            record.stop_loss_price = _q2(new_stop)
            if new_tp is None and record.take_profit_price and old_avg > _ZERO:
                # 이번 주문에 익절가가 없으면 기존 익절 폭을 새 평단에 재적용.
                p_tp = (record.take_profit_price - old_avg) / old_avg
                if p_tp > _ZERO:
                    new_tp = new_avg * (_ONE + p_tp)
            record.take_profit_price = _q2(new_tp) if new_tp is not None else None

        record.quantity = new_qty
        record.avg_cost = new_avg
        # entry_price는 최초 체결가 기록으로 보존(§3) — 판정 기준은 avg_cost로 통일됨.
        record.entry_date = date.today()
        if max_holding_days is not None:
            record.max_holding_days = max_holding_days
        if trailing_stop_pct is not None:
            record.trailing_stop_pct = trailing_stop_pct
        # 트레일링 전환 상태 해제(§5) — 고점 추적을 새 평단 기준으로 재시작.
        record.highest_price = None

        if entry_analysis_snapshot is not None:
            record.entry_analysis_snapshot = entry_analysis_snapshot
            if entry_session_id is not None:
                record.entry_session_id = entry_session_id

        if entry_trigger:
            merged_trigger = list(record.entry_trigger or [])
            for tag in entry_trigger:
                if tag not in merged_trigger:
                    merged_trigger.append(tag)
            record.entry_trigger = merged_trigger

    # ── Close ─────────────────────────────────────────────────────────────

    async def close(
        self,
        position_id: int,
        *,
        exit_price: Decimal,
        exit_reason: ExitReason,
        exit_session_id: UUID | None = None,
    ) -> PositionRecord:
        """포지션 청산 — realized_pnl 자동 계산.

        Parameters
        ----------
        position_id: 포지션 ID
        exit_price: 청산가
        exit_reason: 청산 사유 (ExitReason)
        exit_session_id: decision_log 연결 세션 ID (선택)

        Raises
        ------
        DatabaseError: 포지션을 찾을 수 없거나 이미 청산된 경우
        """
        try:
            async with self._session_factory() as session:
                stmt = select(PositionRecord).where(
                    PositionRecord.id == position_id
                )
                result = await session.execute(stmt)
                record = result.scalar_one_or_none()

                if record is None:
                    raise DatabaseError(
                        f"Position not found: id={position_id}"
                    )

                if record.status == "closed":
                    raise DatabaseError(
                        f"Position already closed: id={position_id}"
                    )

                # 청산 처리
                # realized_pnl = (청산가 - 평균단가) × 수량
                record.status = "closed"
                record.exit_price = exit_price
                record.exit_date = date.today()
                record.exit_reason = exit_reason.value
                record.realized_pnl = (
                    exit_price - record.avg_cost
                ) * record.quantity
                record.exit_session_id = exit_session_id

                await session.commit()
                await session.refresh(record)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Position close failed: {exc}") from exc

        logger.info(
            "position.closed",
            id=record.id,
            symbol=record.symbol,
            reason=exit_reason.value,
            realized_pnl=str(record.realized_pnl),
        )
        await self._record_outcome_safe(record)
        return record

    async def reduce(
        self,
        position_id: int,
        *,
        exit_quantity: int,
        exit_price: Decimal,
        exit_reason: ExitReason,
        exit_session_id: UUID | None = None,
    ) -> PositionRecord:
        """부분 청산 (F-03) — exit_quantity 만큼만 청산하고 잔여는 open 유지.

        체결 수량만큼의 부분 `realized_pnl`을 기존 값에 **누적**한다(부분→전량
        순차 청산 시 손익이 사라지지 않게). `exit_quantity`가 잔여 수량 이상이면
        전량 청산(status='closed', exit_price/date 기록)으로 처리한다.

        Parameters
        ----------
        position_id: 포지션 ID
        exit_quantity: 이번에 청산된 수량 (>0)
        exit_price: 청산가
        exit_reason: 청산 사유 (ExitReason)
        exit_session_id: decision_log 연결 세션 ID (선택)

        Raises
        ------
        DatabaseError: 포지션을 찾을 수 없거나 이미 청산됐거나 수량이 잘못된 경우
        """
        if exit_quantity <= 0:
            raise DatabaseError(f"Invalid exit_quantity: {exit_quantity}")
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(PositionRecord).where(PositionRecord.id == position_id)
                )
                record = result.scalar_one_or_none()

                if record is None:
                    raise DatabaseError(f"Position not found: id={position_id}")
                if record.status == "closed":
                    raise DatabaseError(f"Position already closed: id={position_id}")

                prior_pnl = record.realized_pnl or Decimal("0")

                if exit_quantity >= record.quantity:
                    # 잔여 이상 청산 요청 — 전량 청산으로 마감.
                    # 실현손익은 실제 보유 수량 기준(과청산 방지), 수량은 청산
                    # 시점 보유분을 보존(closed 포지션 관례 — close()와 동일).
                    realized = (exit_price - record.avg_cost) * record.quantity
                    record.realized_pnl = prior_pnl + realized
                    record.status = "closed"
                    record.exit_price = exit_price
                    record.exit_date = date.today()
                    record.exit_reason = exit_reason.value
                    record.exit_session_id = exit_session_id
                    fully_closed = True
                else:
                    # 부분 청산 — 잔여 수량으로 open 유지 (avg_cost 불변)
                    realized = (exit_price - record.avg_cost) * exit_quantity
                    record.realized_pnl = prior_pnl + realized
                    record.quantity -= exit_quantity
                    fully_closed = False

                await session.commit()
                await session.refresh(record)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Position reduce failed: {exc}") from exc

        logger.info(
            "position.reduced",
            id=record.id,
            symbol=record.symbol,
            exit_quantity=exit_quantity,
            remaining=record.quantity,
            fully_closed=fully_closed,
            realized_pnl=str(record.realized_pnl),
        )
        if fully_closed:
            await self._record_outcome_safe(record)
        return record

    async def _record_outcome_safe(self, record: PositionRecord) -> None:
        """전량 청산된 포지션 → 학습 메모리 기록 (best-effort).

        메모리 매니저가 주입되지 않았거나 기록이 실패해도 청산 흐름은
        영향받지 않는다(예외 삼키고 로깅).
        """
        if self._memory_manager is None:
            return
        try:
            await self._memory_manager.record_trade_outcome(
                record, account_id=record.account_id
            )
        except Exception:
            logger.warning(
                "position.record_outcome_failed",
                id=record.id,
                exc_info=True,
            )

    # ── Read ──────────────────────────────────────────────────────────────

    async def get_open(
        self,
        strategy_type: StrategyType | None = None,
        *,
        account_id: str | None = None,
    ) -> list[PositionRecord]:
        """열린 포지션 조회 (strategy_type, account_id 필터 옵션).

        Parameters
        ----------
        strategy_type: 전략 유형 필터 (None이면 전체)
        account_id: 계좌 ID 필터 (None이면 전체 계좌)
        """
        try:
            async with self._session_factory() as session:
                stmt = select(PositionRecord).where(
                    PositionRecord.status == "open"
                )

                if strategy_type is not None:
                    stmt = stmt.where(
                        PositionRecord.strategy_type == strategy_type.value
                    )

                if account_id is not None:
                    stmt = stmt.where(
                        PositionRecord.account_id == account_id
                    )

                # PRJ-04 §10: id 2차 키로 결정론적 정렬 — 스냅샷 인덱싱의
                # "최고령 행 승"이 merge_or_create가 잠그는 행과 같아진다.
                stmt = stmt.order_by(
                    PositionRecord.entry_date.asc(), PositionRecord.id.asc()
                )
                result = await session.execute(stmt)
                return list(result.scalars().all())
        except Exception as exc:
            raise DatabaseError(
                f"Open positions query failed: {exc}"
            ) from exc

    async def get_by_symbol(
        self,
        symbol: str,
        *,
        status: str = "open",
        account_id: str | None = None,
    ) -> PositionRecord | None:
        """종목별 포지션 조회 (동일 종목 다중 행이면 가장 오래된 1건).

        Parameters
        ----------
        symbol: 종목 코드
        status: 포지션 상태 필터 (기본: "open")
        account_id: 계좌 ID 필터 (None이면 전체 계좌)
        """
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(PositionRecord)
                    .where(
                        PositionRecord.symbol == symbol,
                        PositionRecord.status == status,
                    )
                )

                if account_id is not None:
                    stmt = stmt.where(
                        PositionRecord.account_id == account_id
                    )

                # F-27 중복 행에서 임의의 행이 뽑히지 않도록 결정론적 정렬 후 1건.
                stmt = stmt.order_by(
                    PositionRecord.entry_date.asc(), PositionRecord.id.asc()
                ).limit(1)
                result = await session.execute(stmt)
                return result.scalar_one_or_none()
        except Exception as exc:
            raise DatabaseError(
                f"Position query by symbol failed: {exc}"
            ) from exc

    async def get_daily_entries(
        self, target_date: date, *, account_id: str | None = None
    ) -> int:
        """당일 진입 건수.

        Parameters
        ----------
        target_date: 조회 대상 날짜
        account_id: 계좌 ID 필터 (None이면 전체 계좌)
        """
        try:
            async with self._session_factory() as session:
                stmt = select(func.count()).where(
                    PositionRecord.entry_date == target_date
                )

                if account_id is not None:
                    stmt = stmt.where(
                        PositionRecord.account_id == account_id
                    )
                result = await session.execute(stmt)
                return result.scalar_one()
        except Exception as exc:
            raise DatabaseError(
                f"Daily entries count failed: {exc}"
            ) from exc

    # ── Update ────────────────────────────────────────────────────────────

    async def update_stop_loss(
        self, position_id: int, new_stop_loss: Decimal
    ) -> None:
        """트레일링 스톱 업데이트.

        Parameters
        ----------
        position_id: 포지션 ID
        new_stop_loss: 새 손절가

        Raises
        ------
        DatabaseError: 포지션을 찾을 수 없거나 이미 청산된 경우
        """
        try:
            async with self._session_factory() as session:
                stmt = select(PositionRecord).where(
                    PositionRecord.id == position_id
                )
                result = await session.execute(stmt)
                record = result.scalar_one_or_none()

                if record is None:
                    raise DatabaseError(
                        f"Position not found: id={position_id}"
                    )

                if record.status == "closed":
                    raise DatabaseError(
                        f"Cannot update closed position: id={position_id}"
                    )

                record.stop_loss_price = new_stop_loss
                await session.commit()
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Stop loss update failed: {exc}"
            ) from exc

        logger.info(
            "position.stop_loss_updated",
            id=position_id,
            new_stop_loss=str(new_stop_loss),
        )

    async def update_highest_price(
        self, position_id: int, price: Decimal
    ) -> None:
        """고점(high water mark) 갱신 — 트레일링 스탑 기준가 추적용.

        Parameters
        ----------
        position_id: 포지션 ID
        price: 새 고점 가격

        Raises
        ------
        DatabaseError: 포지션을 찾을 수 없거나 이미 청산된 경우
        """
        try:
            async with self._session_factory() as session:
                stmt = select(PositionRecord).where(
                    PositionRecord.id == position_id
                )
                result = await session.execute(stmt)
                record = result.scalar_one_or_none()

                if record is None:
                    raise DatabaseError(
                        f"Position not found: id={position_id}"
                    )

                if record.status == "closed":
                    raise DatabaseError(
                        f"Cannot update closed position: id={position_id}"
                    )

                record.highest_price = price
                await session.commit()
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Highest price update failed: {exc}"
            ) from exc

        logger.info(
            "position.highest_price_updated",
            id=position_id,
            highest_price=str(price),
        )

    async def transition_to_trailing(
        self, position_id: int, *, break_even_price: Decimal
    ) -> None:
        """부분익절 후 잔량을 트레일링 모드로 전환(F-10 Phase 2).

        - ``take_profit_price = None``: 익절가 도달 재발화 차단(이후 사이클은
          트레일링/손절/시간 청산만 평가).
        - ``stop_loss_price = max(기존 손절, break_even_price)``: 부분익절로 이익을
          확정한 잔량을 본전 플로어로 보호('무위험 러너'). 동적 트레일링과 병행
          (둘 중 먼저 닿는 쪽이 청산).

        Parameters
        ----------
        position_id: 포지션 ID
        break_even_price: 본전가(보통 평단가 ``avg_cost``).

        Raises
        ------
        DatabaseError: 포지션을 찾을 수 없거나 이미 청산된 경우
        """
        try:
            async with self._session_factory() as session:
                stmt = select(PositionRecord).where(
                    PositionRecord.id == position_id
                )
                result = await session.execute(stmt)
                record = result.scalar_one_or_none()

                if record is None:
                    raise DatabaseError(
                        f"Position not found: id={position_id}"
                    )

                if record.status == "closed":
                    raise DatabaseError(
                        f"Cannot update closed position: id={position_id}"
                    )

                record.take_profit_price = None
                current_stop = record.stop_loss_price or Decimal("0")
                if break_even_price > current_stop:
                    record.stop_loss_price = break_even_price
                await session.commit()
                new_stop = record.stop_loss_price
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Trailing transition failed: {exc}"
            ) from exc

        logger.info(
            "position.transition_to_trailing",
            id=position_id,
            break_even_price=str(break_even_price),
            new_stop_loss=str(new_stop),
        )

    async def update_quantity(
        self,
        position_id: int,
        *,
        quantity: int,
        avg_cost: Decimal,
    ) -> PositionRecord:
        """수량·평균단가 갱신 (브로커 정합성 보정용).

        Parameters
        ----------
        position_id: 포지션 ID
        quantity: 브로커 실보유 수량
        avg_cost: 브로커 평균단가

        Raises
        ------
        DatabaseError: 포지션을 찾을 수 없거나 이미 청산된 경우
        """
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(PositionRecord).where(PositionRecord.id == position_id)
                )
                record = result.scalar_one_or_none()

                if record is None:
                    raise DatabaseError(f"Position not found: id={position_id}")
                if record.status == "closed":
                    raise DatabaseError(
                        f"Cannot update closed position: id={position_id}"
                    )

                record.quantity = quantity
                record.avg_cost = avg_cost
                await session.commit()
                await session.refresh(record)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Quantity update failed: {exc}") from exc

        logger.info(
            "position.quantity_updated",
            id=position_id,
            quantity=quantity,
            avg_cost=str(avg_cost),
        )
        return record
