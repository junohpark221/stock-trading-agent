"""Phase 4 Step 8: PositionManager 단위 테스트.

CRUD 각 메서드별 정상/에러/경계값 = 20 테스트.
mock async session 패턴 사용.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.core.enums import ExitReason, StrategyType
from src.core.exceptions import DatabaseError
from src.strategy.position_manager import PositionManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_session_factory():
    """mock async session factory 생성.

    Returns (factory, session) — session에서 동작 제어 가능.
    """
    session = AsyncMock()
    factory = MagicMock()
    # async context manager 지원
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, session


def _mock_position_record(**overrides: object) -> MagicMock:
    """PositionRecord mock 생성."""
    record = MagicMock()
    record.id = 1
    record.symbol = "005930"
    record.strategy_type = "position"
    record.quantity = 100
    record.avg_cost = Decimal("70000")
    record.entry_price = Decimal("70000")
    record.entry_date = date(2026, 3, 20)
    record.stop_loss_price = Decimal("67000")
    record.take_profit_price = Decimal("77000")
    record.status = "open"
    record.exit_price = None
    record.exit_date = None
    record.exit_reason = None
    record.realized_pnl = None
    for k, v in overrides.items():
        setattr(record, k, v)
    return record


def _mock_scalar_result(value: object) -> MagicMock:
    """session.execute() 결과 mock — scalar_one_or_none/scalar_one용."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar_one.return_value = value
    return result


def _mock_scalars_result(values: list) -> MagicMock:
    """session.execute() 결과 mock — scalars().all()용."""
    result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = values
    result.scalars.return_value = scalars_mock
    return result


# ===========================================================================
# create
# ===========================================================================


class TestCreate:
    """포지션 생성 테스트."""

    @pytest.mark.asyncio
    async def test_create_position_success(self) -> None:
        """정상 생성 — 필드가 올바르게 설정됨."""
        factory, session = _mock_session_factory()
        manager = PositionManager(factory)

        with patch(
            "src.strategy.position_manager.PositionRecord"
        ) as MockRecord:
            mock_instance = MagicMock()
            mock_instance.id = 1
            mock_instance.symbol = "005930"
            mock_instance.quantity = 100
            mock_instance.strategy_type = "position"
            MockRecord.return_value = mock_instance

            result = await manager.create(
                symbol="005930",
                strategy_type="position",
                quantity=100,
                entry_price=Decimal("70000"),
                stop_loss_price=Decimal("67000"),
                take_profit_price=Decimal("77000"),
            )

            # PositionRecord 생성자에 올바른 값 전달 확인
            call_kwargs = MockRecord.call_args[1]
            assert call_kwargs["symbol"] == "005930"
            assert call_kwargs["avg_cost"] == Decimal("70000")
            assert call_kwargs["entry_price"] == Decimal("70000")
            assert call_kwargs["status"] == "open"
            assert call_kwargs["stop_loss_price"] == Decimal("67000")

            session.add.assert_called_once_with(mock_instance)
            session.commit.assert_awaited_once()
            session.refresh.assert_awaited_once_with(mock_instance)
            assert result is mock_instance

    @pytest.mark.asyncio
    async def test_create_with_optional_fields(self) -> None:
        """선택 필드 (trailing_stop_pct, max_holding_days, session_id)."""
        factory, session = _mock_session_factory()
        manager = PositionManager(factory)
        session_id = uuid4()

        with patch(
            "src.strategy.position_manager.PositionRecord"
        ) as MockRecord:
            MockRecord.return_value = MagicMock(
                id=1, symbol="005930", quantity=50, strategy_type="swing"
            )

            await manager.create(
                symbol="005930",
                strategy_type="swing",
                quantity=50,
                entry_price=Decimal("50000"),
                stop_loss_price=Decimal("48500"),
                trailing_stop_pct=Decimal("3.0"),
                max_holding_days=10,
                entry_session_id=session_id,
            )

            call_kwargs = MockRecord.call_args[1]
            assert call_kwargs["trailing_stop_pct"] == Decimal("3.0")
            assert call_kwargs["max_holding_days"] == 10
            assert call_kwargs["entry_session_id"] == session_id

    @pytest.mark.asyncio
    async def test_create_raises_database_error(self) -> None:
        """DB 예외 → DatabaseError 변환."""
        factory, session = _mock_session_factory()
        session.commit.side_effect = RuntimeError("connection lost")
        manager = PositionManager(factory)

        with patch("src.strategy.position_manager.PositionRecord"):
            with pytest.raises(DatabaseError, match="Position create failed"):
                await manager.create(
                    symbol="005930",
                    strategy_type="position",
                    quantity=100,
                    entry_price=Decimal("70000"),
                    stop_loss_price=Decimal("67000"),
                )


# ===========================================================================
# close
# ===========================================================================


class TestClose:
    """포지션 청산 테스트."""

    @pytest.mark.asyncio
    async def test_close_profit(self) -> None:
        """이익 청산 — realized_pnl > 0."""
        factory, session = _mock_session_factory()
        record = _mock_position_record(
            avg_cost=Decimal("70000"), quantity=100
        )
        session.execute.return_value = _mock_scalar_result(record)
        manager = PositionManager(factory)

        result = await manager.close(
            1, exit_price=Decimal("75000"), exit_reason=ExitReason.TAKE_PROFIT
        )

        # realized_pnl = (75000 - 70000) × 100 = 500,000
        assert record.realized_pnl == Decimal("500000")
        assert record.status == "closed"
        assert record.exit_reason == "take_profit"
        assert record.exit_price == Decimal("75000")
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_loss(self) -> None:
        """손실 청산 — realized_pnl < 0."""
        factory, session = _mock_session_factory()
        record = _mock_position_record(
            avg_cost=Decimal("70000"), quantity=100
        )
        session.execute.return_value = _mock_scalar_result(record)
        manager = PositionManager(factory)

        await manager.close(
            1, exit_price=Decimal("65000"), exit_reason=ExitReason.STOP_LOSS
        )

        # realized_pnl = (65000 - 70000) × 100 = -500,000
        assert record.realized_pnl == Decimal("-500000")
        assert record.exit_reason == "stop_loss"

    @pytest.mark.asyncio
    async def test_close_with_session_id(self) -> None:
        """exit_session_id 전달 확인."""
        factory, session = _mock_session_factory()
        record = _mock_position_record()
        session.execute.return_value = _mock_scalar_result(record)
        manager = PositionManager(factory)
        sid = uuid4()

        await manager.close(
            1,
            exit_price=Decimal("75000"),
            exit_reason=ExitReason.TAKE_PROFIT,
            exit_session_id=sid,
        )

        assert record.exit_session_id == sid

    @pytest.mark.asyncio
    async def test_close_not_found(self) -> None:
        """존재하지 않는 포지션 → DatabaseError."""
        factory, session = _mock_session_factory()
        session.execute.return_value = _mock_scalar_result(None)
        manager = PositionManager(factory)

        with pytest.raises(DatabaseError, match="Position not found"):
            await manager.close(
                999,
                exit_price=Decimal("75000"),
                exit_reason=ExitReason.TAKE_PROFIT,
            )

    @pytest.mark.asyncio
    async def test_close_already_closed(self) -> None:
        """이미 청산된 포지션 → DatabaseError."""
        factory, session = _mock_session_factory()
        record = _mock_position_record(status="closed")
        session.execute.return_value = _mock_scalar_result(record)
        manager = PositionManager(factory)

        with pytest.raises(DatabaseError, match="already closed"):
            await manager.close(
                1,
                exit_price=Decimal("75000"),
                exit_reason=ExitReason.TAKE_PROFIT,
            )


# ===========================================================================
# reduce (F-03 부분 청산)
# ===========================================================================


class TestReduce:
    """부분 청산 테스트 (F-03)."""

    @pytest.mark.asyncio
    async def test_reduce_partial_keeps_open(self) -> None:
        """부분 청산 — 잔여 수량 open 유지 + 부분 realized_pnl."""
        factory, session = _mock_session_factory()
        record = _mock_position_record(avg_cost=Decimal("70000"), quantity=100)
        session.execute.return_value = _mock_scalar_result(record)
        manager = PositionManager(factory)

        await manager.reduce(
            1,
            exit_quantity=40,
            exit_price=Decimal("75000"),
            exit_reason=ExitReason.MANUAL,
        )

        # realized_pnl = (75000-70000) × 40 = 200,000, 잔여 60주 open
        assert record.realized_pnl == Decimal("200000")
        assert record.quantity == 60
        assert record.status == "open"
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reduce_accumulates_realized_pnl(self) -> None:
        """기존 realized_pnl에 부분 손익이 누적된다."""
        factory, session = _mock_session_factory()
        record = _mock_position_record(
            avg_cost=Decimal("70000"), quantity=100, realized_pnl=Decimal("50000")
        )
        session.execute.return_value = _mock_scalar_result(record)
        manager = PositionManager(factory)

        await manager.reduce(
            1, exit_quantity=10, exit_price=Decimal("80000"),
            exit_reason=ExitReason.MANUAL,
        )

        # 50,000 + (80000-70000)×10 = 150,000
        assert record.realized_pnl == Decimal("150000")
        assert record.quantity == 90

    @pytest.mark.asyncio
    async def test_reduce_full_closes(self) -> None:
        """잔여 이상 청산 요청 → 전량 청산(status=closed), 보유분 기준 손익."""
        factory, session = _mock_session_factory()
        record = _mock_position_record(avg_cost=Decimal("70000"), quantity=100)
        session.execute.return_value = _mock_scalar_result(record)
        manager = PositionManager(factory)

        await manager.reduce(
            1, exit_quantity=100, exit_price=Decimal("75000"),
            exit_reason=ExitReason.MANUAL,
        )

        assert record.status == "closed"
        assert record.realized_pnl == Decimal("500000")  # 100주 기준
        assert record.exit_price == Decimal("75000")

    @pytest.mark.asyncio
    async def test_reduce_invalid_quantity(self) -> None:
        """exit_quantity<=0 → DatabaseError."""
        factory, _ = _mock_session_factory()
        manager = PositionManager(factory)
        with pytest.raises(DatabaseError, match="Invalid exit_quantity"):
            await manager.reduce(
                1, exit_quantity=0, exit_price=Decimal("75000"),
                exit_reason=ExitReason.MANUAL,
            )

    @pytest.mark.asyncio
    async def test_reduce_already_closed(self) -> None:
        """이미 청산된 포지션 → DatabaseError."""
        factory, session = _mock_session_factory()
        record = _mock_position_record(status="closed")
        session.execute.return_value = _mock_scalar_result(record)
        manager = PositionManager(factory)
        with pytest.raises(DatabaseError, match="already closed"):
            await manager.reduce(
                1, exit_quantity=10, exit_price=Decimal("75000"),
                exit_reason=ExitReason.MANUAL,
            )


# ===========================================================================
# get_open
# ===========================================================================


class TestGetOpen:
    """열린 포지션 조회 테스트."""

    @pytest.mark.asyncio
    async def test_get_all_open(self) -> None:
        """전체 열린 포지션 조회."""
        factory, session = _mock_session_factory()
        records = [_mock_position_record(id=i) for i in range(3)]
        session.execute.return_value = _mock_scalars_result(records)
        manager = PositionManager(factory)

        result = await manager.get_open()

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_get_open_with_strategy_filter(self) -> None:
        """strategy_type 필터 조회."""
        factory, session = _mock_session_factory()
        session.execute.return_value = _mock_scalars_result([])
        manager = PositionManager(factory)

        result = await manager.get_open(strategy_type=StrategyType.SWING)

        assert result == []
        # execute가 호출되었는지만 확인 (쿼리 내용은 통합 테스트에서)
        session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_open_empty(self) -> None:
        """열린 포지션 없음 → 빈 리스트."""
        factory, session = _mock_session_factory()
        session.execute.return_value = _mock_scalars_result([])
        manager = PositionManager(factory)

        result = await manager.get_open()

        assert result == []


# ===========================================================================
# get_by_symbol
# ===========================================================================


class TestGetBySymbol:
    """종목별 포지션 조회 테스트."""

    @pytest.mark.asyncio
    async def test_found(self) -> None:
        """매칭 포지션 반환."""
        factory, session = _mock_session_factory()
        record = _mock_position_record(symbol="005930")
        session.execute.return_value = _mock_scalar_result(record)
        manager = PositionManager(factory)

        result = await manager.get_by_symbol("005930")

        assert result is record

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        """매칭 없음 → None."""
        factory, session = _mock_session_factory()
        session.execute.return_value = _mock_scalar_result(None)
        manager = PositionManager(factory)

        result = await manager.get_by_symbol("999999")

        assert result is None

    @pytest.mark.asyncio
    async def test_custom_status_filter(self) -> None:
        """status 파라미터 커스텀 — closed 조회."""
        factory, session = _mock_session_factory()
        record = _mock_position_record(status="closed")
        session.execute.return_value = _mock_scalar_result(record)
        manager = PositionManager(factory)

        result = await manager.get_by_symbol("005930", status="closed")

        assert result is record


# ===========================================================================
# get_daily_entries
# ===========================================================================


class TestGetDailyEntries:
    """당일 진입 건수 테스트."""

    @pytest.mark.asyncio
    async def test_count_entries(self) -> None:
        """진입 건수 반환."""
        factory, session = _mock_session_factory()
        session.execute.return_value = _mock_scalar_result(3)
        manager = PositionManager(factory)

        result = await manager.get_daily_entries(date(2026, 3, 22))

        assert result == 3

    @pytest.mark.asyncio
    async def test_zero_entries(self) -> None:
        """진입 없음 → 0."""
        factory, session = _mock_session_factory()
        session.execute.return_value = _mock_scalar_result(0)
        manager = PositionManager(factory)

        result = await manager.get_daily_entries(date(2026, 3, 22))

        assert result == 0


# ===========================================================================
# update_stop_loss
# ===========================================================================


class TestUpdateStopLoss:
    """스톱로스 업데이트 테스트."""

    @pytest.mark.asyncio
    async def test_update_success(self) -> None:
        """정상 업데이트."""
        factory, session = _mock_session_factory()
        record = _mock_position_record(stop_loss_price=Decimal("67000"))
        session.execute.return_value = _mock_scalar_result(record)
        manager = PositionManager(factory)

        await manager.update_stop_loss(1, Decimal("69000"))

        assert record.stop_loss_price == Decimal("69000")
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_not_found(self) -> None:
        """존재하지 않는 포지션 → DatabaseError."""
        factory, session = _mock_session_factory()
        session.execute.return_value = _mock_scalar_result(None)
        manager = PositionManager(factory)

        with pytest.raises(DatabaseError, match="Position not found"):
            await manager.update_stop_loss(999, Decimal("69000"))

    @pytest.mark.asyncio
    async def test_update_closed_position(self) -> None:
        """이미 청산된 포지션 → DatabaseError."""
        factory, session = _mock_session_factory()
        record = _mock_position_record(status="closed")
        session.execute.return_value = _mock_scalar_result(record)
        manager = PositionManager(factory)

        with pytest.raises(DatabaseError, match="Cannot update closed"):
            await manager.update_stop_loss(1, Decimal("69000"))
