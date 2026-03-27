"""Phase 7 Step 1: Enum, Pydantic model, Config 기반 테스트.

Tests:
- BacktestStatus, BacktestMode enum 직렬화/역직렬화
- BacktestConfig 기본값 검증 (initial_capital=10M, slippage_bps=10, mode=TECHNICAL)
- BacktestTradeRecord Decimal 필드 정밀도
- BacktestResult status 필드 정상 할당
- Config BACKTEST_* 환경변수 오버라이드
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from src.core.enums import (
    BacktestMode,
    BacktestStatus,
    ExitReason,
    OrderSide,
    StrategyType,
)
from src.core.models import (
    BacktestConfig,
    BacktestResult,
    BacktestTradeRecord,
    PerformanceMetrics,
)
from tests.conftest import make_settings


# ---------------------------------------------------------------------------
# Enum Tests
# ---------------------------------------------------------------------------


class TestBacktestStatus:
    """BacktestStatus enum 테스트."""

    def test_values(self):
        assert BacktestStatus.PENDING == "pending"
        assert BacktestStatus.RUNNING == "running"
        assert BacktestStatus.COMPLETED == "completed"
        assert BacktestStatus.FAILED == "failed"

    def test_from_string(self):
        assert BacktestStatus("pending") is BacktestStatus.PENDING
        assert BacktestStatus("completed") is BacktestStatus.COMPLETED

    def test_invalid_value(self):
        with pytest.raises(ValueError):
            BacktestStatus("invalid")

    def test_all_members(self):
        assert len(BacktestStatus) == 4


class TestBacktestMode:
    """BacktestMode enum 테스트."""

    def test_values(self):
        assert BacktestMode.TECHNICAL == "technical"
        assert BacktestMode.LLM_REPLAY == "llm_replay"
        assert BacktestMode.LLM_LIVE == "llm_live"

    def test_from_string(self):
        assert BacktestMode("technical") is BacktestMode.TECHNICAL
        assert BacktestMode("llm_replay") is BacktestMode.LLM_REPLAY

    def test_invalid_value(self):
        with pytest.raises(ValueError):
            BacktestMode("invalid")

    def test_all_members(self):
        assert len(BacktestMode) == 3


# ---------------------------------------------------------------------------
# Pydantic Model Tests
# ---------------------------------------------------------------------------


class TestBacktestConfig:
    """BacktestConfig 모델 테스트."""

    def test_defaults(self):
        cfg = BacktestConfig(
            strategy_type=StrategyType.POSITION,
            start_date=date(2023, 1, 1),
            end_date=date(2025, 12, 31),
        )
        assert cfg.initial_capital == Decimal("10000000")
        assert cfg.slippage_bps == 10
        assert cfg.mode == BacktestMode.TECHNICAL
        assert cfg.symbols == []
        assert cfg.parameters == {}
        assert cfg.llm_model_filter is None
        assert cfg.benchmark_symbol == "KOSPI"

    def test_custom_values(self):
        cfg = BacktestConfig(
            strategy_type=StrategyType.SWING,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            initial_capital=Decimal("50000000"),
            symbols=["005930", "000660"],
            slippage_bps=20,
            mode=BacktestMode.LLM_REPLAY,
            parameters={"rsi_period": 14},
            llm_model_filter="gpt-4o",
            benchmark_symbol="KOSDAQ",
        )
        assert cfg.strategy_type == StrategyType.SWING
        assert cfg.symbols == ["005930", "000660"]
        assert cfg.slippage_bps == 20
        assert cfg.mode == BacktestMode.LLM_REPLAY
        assert cfg.llm_model_filter == "gpt-4o"

    def test_symbols_empty_list_default(self):
        cfg1 = BacktestConfig(
            strategy_type=StrategyType.POSITION,
            start_date=date(2023, 1, 1),
            end_date=date(2025, 12, 31),
        )
        cfg2 = BacktestConfig(
            strategy_type=StrategyType.POSITION,
            start_date=date(2023, 1, 1),
            end_date=date(2025, 12, 31),
        )
        # 기본값 리스트가 공유되지 않는지 확인
        cfg1.symbols.append("005930")
        assert cfg2.symbols == []


class TestBacktestTradeRecord:
    """BacktestTradeRecord 모델 테스트."""

    def test_decimal_precision(self):
        trade = BacktestTradeRecord(
            symbol="005930",
            side=OrderSide.BUY,
            quantity=10,
            price=Decimal("71500.00"),
            commission=Decimal("107.25"),
            slippage=Decimal("71.50"),
            trade_date=date(2024, 3, 15),
        )
        assert trade.price == Decimal("71500.00")
        assert trade.commission == Decimal("107.25")
        assert trade.slippage == Decimal("71.50")
        assert trade.pnl is None
        assert trade.exit_reason is None

    def test_with_exit(self):
        trade = BacktestTradeRecord(
            symbol="005930",
            side=OrderSide.SELL,
            quantity=10,
            price=Decimal("73000.00"),
            commission=Decimal("142.35"),
            slippage=Decimal("73.00"),
            trade_date=date(2024, 4, 10),
            pnl=Decimal("14500.00"),
            exit_reason=ExitReason.TAKE_PROFIT,
        )
        assert trade.pnl == Decimal("14500.00")
        assert trade.exit_reason == ExitReason.TAKE_PROFIT


class TestBacktestResult:
    """BacktestResult 모델 테스트."""

    def test_status_assignment(self):
        run_id = uuid4()
        cfg = BacktestConfig(
            strategy_type=StrategyType.POSITION,
            start_date=date(2023, 1, 1),
            end_date=date(2025, 12, 31),
        )
        result = BacktestResult(
            run_id=run_id,
            config=cfg,
            status=BacktestStatus.COMPLETED,
            started_at=datetime(2026, 3, 27, 10, 0, 0),
            completed_at=datetime(2026, 3, 27, 10, 0, 30),
            total_trades=15,
        )
        assert result.status == BacktestStatus.COMPLETED
        assert result.total_trades == 15
        assert result.trades == []
        assert result.metrics is None
        assert result.error_message is None

    def test_failed_result(self):
        run_id = uuid4()
        cfg = BacktestConfig(
            strategy_type=StrategyType.SWING,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
        )
        result = BacktestResult(
            run_id=run_id,
            config=cfg,
            status=BacktestStatus.FAILED,
            started_at=datetime(2026, 3, 27, 10, 0, 0),
            error_message="Insufficient OHLCV data",
        )
        assert result.status == BacktestStatus.FAILED
        assert result.error_message == "Insufficient OHLCV data"
        assert result.completed_at is None

    def test_trades_default_empty_list(self):
        r1 = BacktestResult(
            run_id=uuid4(),
            config=BacktestConfig(
                strategy_type=StrategyType.POSITION,
                start_date=date(2023, 1, 1),
                end_date=date(2025, 12, 31),
            ),
            status=BacktestStatus.PENDING,
            started_at=datetime(2026, 3, 27, 10, 0, 0),
        )
        r2 = BacktestResult(
            run_id=uuid4(),
            config=BacktestConfig(
                strategy_type=StrategyType.POSITION,
                start_date=date(2023, 1, 1),
                end_date=date(2025, 12, 31),
            ),
            status=BacktestStatus.PENDING,
            started_at=datetime(2026, 3, 27, 10, 0, 0),
        )
        r1.trades.append(
            BacktestTradeRecord(
                symbol="005930",
                side=OrderSide.BUY,
                quantity=1,
                price=Decimal("70000"),
                commission=Decimal("10"),
                slippage=Decimal("7"),
                trade_date=date(2024, 1, 2),
            )
        )
        assert r2.trades == []


# ---------------------------------------------------------------------------
# Config Tests
# ---------------------------------------------------------------------------


class TestBacktestSettings:
    """Phase 7 Config 환경변수 테스트."""

    def test_defaults(self):
        s = make_settings()
        assert s.BACKTEST_DEFAULT_INITIAL_CAPITAL == 10_000_000
        assert s.BACKTEST_SLIPPAGE_BPS == 10
        assert s.BACKTEST_MAX_WORKERS == 4

    def test_override(self):
        s = make_settings(
            BACKTEST_DEFAULT_INITIAL_CAPITAL=50_000_000,
            BACKTEST_SLIPPAGE_BPS=20,
            BACKTEST_MAX_WORKERS=8,
        )
        assert s.BACKTEST_DEFAULT_INITIAL_CAPITAL == 50_000_000
        assert s.BACKTEST_SLIPPAGE_BPS == 20
        assert s.BACKTEST_MAX_WORKERS == 8
