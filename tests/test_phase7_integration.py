"""Phase 7 E2E 테스트 — Docker DB + Redis, 백테스트 엔진 전체 흐름.

Prerequisites:
    - Docker: docker compose up -d (PostgreSQL 16 + Redis 7)
    - Migrations: uv run alembic upgrade head
    - .env: USE_MOCK_BROKER=true

Run:
    uv run pytest tests/test_phase7_integration.py -v -s --timeout=180

Skip in CI:
    uv run pytest -m "not e2e"
"""

from __future__ import annotations

import asyncio
import math
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import UUID

import httpx
import pytest
from httpx import ASGITransport

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio(loop_scope="module")]


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def env_override():
    """E2E 환경변수 설정 — 모듈 시작 전에 적용."""
    overrides = {
        "USE_MOCK_BROKER": "true",
        "SCHEDULER_ENABLED": "false",
        "HUMAN_APPROVAL_REQUIRED": "false",
        "WEB_VERIFY_ENABLED": "false",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "test_chat",
    }
    original = {}
    for key, value in overrides.items():
        original[key] = os.environ.get(key)
        os.environ[key] = value

    from src.config import get_settings

    get_settings.cache_clear()
    yield
    for key, orig_val in original.items():
        if orig_val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig_val
    get_settings.cache_clear()


@pytest.fixture(scope="module")
async def client(env_override):
    """httpx AsyncClient — lifespan 수동 관리."""
    from src.main import app, lifespan

    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            timeout=90.0,
        ) as c:
            yield c


@pytest.fixture(scope="module")
def session_factory(client):
    """DB session factory — 시드 데이터 삽입용."""
    from src.db.session import get_session_factory

    return get_session_factory()


@pytest.fixture(scope="module")
async def seed_ohlcv_data(session_factory):
    """3종목 × 120거래일 DailyOHLCV 시드.

    sin() + 선형 트렌드로 deterministic 가격 생성.
    SMA(20/60) 크로스오버가 발생하도록 충분한 변동성 부여.
    """
    from sqlalchemy import delete

    from src.db.models.market_data import DailyOHLCV

    symbols = ["005930", "035720", "000660"]
    base_prices = {"005930": 70000, "035720": 35000, "000660": 180000}

    # 120 weekday-only 거래일 생성 (2024-07-01 ~)
    trading_dates: list[date] = []
    cursor = date(2024, 7, 1)
    while len(trading_dates) < 120:
        if cursor.weekday() < 5:  # 월~금
            trading_dates.append(cursor)
        cursor += timedelta(days=1)

    rows: list[DailyOHLCV] = []
    prev_close: dict[str, int] = {}

    for i, trade_date in enumerate(trading_dates):
        for symbol in symbols:
            base = base_prices[symbol]
            # 선형 트렌드: +0.05%/day
            trend = base * 0.0005 * i
            # 사인파: 20일 주기, 3% 진폭
            wave = base * 0.03 * math.sin(i * 2 * math.pi / 20)
            close_val = int(base + trend + wave)
            open_val = int(close_val * (1 + 0.005 * math.sin(i * 0.7)))
            high_val = int(max(open_val, close_val) * 1.015)
            low_val = int(min(open_val, close_val) * 0.985)
            volume = 1_000_000 + int(500_000 * abs(math.sin(i * 0.3)))

            # change_rate 계산
            prev = prev_close.get(symbol)
            if prev and prev != 0:
                change_rate = round((close_val - prev) / prev * 100, 4)
            else:
                change_rate = 0.0
            prev_close[symbol] = close_val

            rows.append(
                DailyOHLCV(
                    symbol=symbol,
                    date=trade_date,
                    open=open_val,
                    high=high_val,
                    low=low_val,
                    close=close_val,
                    volume=volume,
                    change_rate=change_rate,
                )
            )

    # 기존 시드 데이터 정리 후 삽입
    async with session_factory() as session:
        await session.execute(
            delete(DailyOHLCV).where(DailyOHLCV.symbol.in_(symbols))
        )
        session.add_all(rows)
        await session.commit()

    yield {
        "symbols": symbols,
        "start_date": trading_dates[0],
        "end_date": trading_dates[-1],
        "trading_days": len(trading_dates),
    }

    # teardown: 시드 데이터 정리
    async with session_factory() as session:
        await session.execute(
            delete(DailyOHLCV).where(DailyOHLCV.symbol.in_(symbols))
        )
        await session.commit()


# ── Helpers ──────────────────────────────────────────────────────────────


async def _poll_until_complete(
    client: httpx.AsyncClient,
    run_id: str,
    *,
    timeout: float = 60.0,
) -> dict:
    """GET /api/backtest/runs/{run_id} 폴링 — completed/failed 될 때까지."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/api/backtest/runs/{run_id}")
        assert resp.status_code == 200, f"Poll failed: {resp.status_code}"
        data = resp.json()
        status = data["run"]["status"]
        if status not in ("pending", "running"):
            return data
        await asyncio.sleep(0.5)
    raise TimeoutError(f"Backtest {run_id} did not complete within {timeout}s")


# ── E2E Scenarios ────────────────────────────────────────────────────────


class TestBacktestE2E:
    """Phase 7 E2E 통합 테스트 — 8 시나리오."""

    _position_run_id: str | None = None
    _swing_run_id: str | None = None

    # ── 1. Mode 1 포지션 전략 백테스트 ──────────────────────────────

    async def test_mode1_position_trading(self, client, seed_ohlcv_data):
        """POST /run(position, technical) → 폴링 → COMPLETED → metrics 존재."""
        info = seed_ohlcv_data

        resp = await client.post(
            "/api/backtest/run",
            json={
                "strategy_type": "position",
                "mode": "technical",
                "start_date": str(info["start_date"]),
                "end_date": str(info["end_date"]),
                "symbols": info["symbols"],
                "initial_capital": "10000000",
                "slippage_bps": 10,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending"
        run_id = body["run_id"]
        assert UUID(run_id)

        # 폴링
        data = await _poll_until_complete(client, run_id)
        run = data["run"]
        assert run["status"] == "completed", f"Expected completed, got {run['status']}: {run.get('error_message')}"
        assert run["result_metrics"] is not None

        metrics = run["result_metrics"]
        for key in ("total_return_pct", "sharpe_ratio", "max_drawdown_pct", "win_rate_pct"):
            assert key in metrics, f"Missing key: {key}"

        TestBacktestE2E._position_run_id = run_id

    # ── 2. Mode 1 스윙 전략 백테스트 ───────────────────────────────

    async def test_mode1_swing_trading(self, client, seed_ohlcv_data):
        """POST /run(swing, technical) → 폴링 → COMPLETED."""
        info = seed_ohlcv_data

        resp = await client.post(
            "/api/backtest/run",
            json={
                "strategy_type": "swing",
                "mode": "technical",
                "start_date": str(info["start_date"]),
                "end_date": str(info["end_date"]),
                "symbols": info["symbols"],
                "initial_capital": "10000000",
                "slippage_bps": 10,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        run_id = body["run_id"]

        data = await _poll_until_complete(client, run_id)
        run = data["run"]
        assert run["status"] == "completed", f"Expected completed, got {run['status']}: {run.get('error_message')}"
        assert run["result_metrics"] is not None

        TestBacktestE2E._swing_run_id = run_id

    # ── 3. 벤치마크 비교 검증 ──────────────────────────────────────

    async def test_benchmark_comparison(self, client):
        """결과에 total_return_pct 존재 — 벤치마크 비교 플로우 검증."""
        run_id = TestBacktestE2E._position_run_id
        assert run_id is not None, "Scenario 1 must run first"

        resp = await client.get(f"/api/backtest/runs/{run_id}")
        assert resp.status_code == 200
        data = resp.json()

        metrics = data["run"]["result_metrics"]
        assert metrics is not None
        assert "total_return_pct" in metrics
        assert "sharpe_ratio" in metrics

    # ── 4. 전략 비교 (compare API) ──────────────────────────────────

    async def test_compare_position_vs_swing(self, client):
        """2 runs → GET /compare → best_sharpe 식별."""
        pos_id = TestBacktestE2E._position_run_id
        swing_id = TestBacktestE2E._swing_run_id
        assert pos_id and swing_id, "Scenarios 1 & 2 must run first"

        resp = await client.get(
            "/api/backtest/compare",
            params={"run_ids": f"{pos_id},{swing_id}"},
        )
        assert resp.status_code == 200
        body = resp.json()

        assert len(body["runs"]) == 2
        assert body["best_sharpe_run_id"] is not None
        assert body["best_return_run_id"] is not None
        assert body["lowest_mdd_run_id"] is not None

        # best 값들이 두 run_id 중 하나여야 함
        valid_ids = {pos_id, swing_id}
        assert body["best_sharpe_run_id"] in valid_ids
        assert body["best_return_run_id"] in valid_ids
        assert body["lowest_mdd_run_id"] in valid_ids

    # ── 5. IS/OOS 분리 ─────────────────────────────────────────────

    async def test_is_oos_split(self):
        """BacktestReporter.generate_report() 직접 호출 → oos_split 검증."""
        from uuid import uuid4

        from src.backtest.reporter import BacktestReporter
        from src.core.enums import BacktestStatus, StrategyType
        from src.core.models import BacktestConfig, BacktestResult, PerformanceMetrics

        config = BacktestConfig(
            strategy_type=StrategyType.POSITION,
            start_date=date(2024, 7, 1),
            end_date=date(2024, 12, 31),
            symbols=["005930"],
        )

        metrics = PerformanceMetrics(
            period_start=date(2024, 7, 1),
            period_end=date(2024, 12, 31),
            total_return_pct=Decimal("5.50"),
            max_drawdown_pct=Decimal("3.20"),
            win_rate_pct=Decimal("60.00"),
            avg_win_pct=Decimal("4.00"),
            avg_loss_pct=Decimal("2.00"),
            total_trades=10,
            winning_trades=6,
            losing_trades=4,
        )

        result = BacktestResult(
            run_id=uuid4(),
            config=config,
            status=BacktestStatus.COMPLETED,
            metrics=metrics,
            trades=[],
            total_trades=10,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )

        # 10개 mock closed positions (exit_date 분산)
        positions = []
        for i in range(10):
            p = MagicMock()
            p.exit_date = date(2024, 7, 1) + timedelta(days=i * 15)
            p.entry_price = Decimal("70000")
            p.exit_price = Decimal("72000") if i % 3 != 0 else Decimal("68000")
            p.realized_pnl = Decimal("20000") if i % 3 != 0 else Decimal("-20000")
            p.strategy_type = "position"
            positions.append(p)

        # 20개 mock snapshots
        snapshots = []
        base_val = 10_000_000
        for i in range(20):
            s = MagicMock()
            s.snapshot_date = date(2024, 7, 1) + timedelta(days=i * 7)
            val = base_val + int(50_000 * math.sin(i * 0.5))
            s.total_value = Decimal(str(val))
            snapshots.append(s)

        report = BacktestReporter.generate_report(
            result=result,
            closed_positions=positions,
            snapshots=snapshots,
            is_ratio=Decimal("0.7"),
        )

        assert report.oos_split is not None, "oos_split should not be None with 20 snapshots"
        assert isinstance(report.oos_split.is_overfit, bool)
        assert report.oos_split.in_sample_metrics is not None
        assert report.oos_split.out_of_sample_metrics is not None
        assert report.oos_split.is_ratio == Decimal("0.7")
        assert report.oos_split.split_date is not None

    # ── 6. Walk-forward 분석 ───────────────────────────────────────

    async def test_walk_forward(self, session_factory, seed_ohlcv_data):
        """WalkForwardAnalyzer 직접 실행 → WalkForwardResult 구조 검증."""
        from src.backtest.data_loader import HistoricalDataLoader
        from src.backtest.engine import BacktestEngine
        from src.backtest.simulator import SimulatedBroker
        from src.backtest.walk_forward import WalkForwardAnalyzer
        from src.config import get_settings
        from src.core.enums import StrategyType
        from src.core.models import BacktestConfig

        info = seed_ohlcv_data
        settings = get_settings()

        loader = HistoricalDataLoader(session_factory)
        await loader.load(
            symbols=info["symbols"],
            start_date=info["start_date"],
            end_date=info["end_date"],
        )

        broker = SimulatedBroker(
            data_loader=loader,
            initial_capital=Decimal("10000000"),
        )
        engine = BacktestEngine(
            data_loader=loader,
            broker=broker,
            settings=settings,
            session_factory=session_factory,
        )

        config = BacktestConfig(
            strategy_type=StrategyType.SWING,
            start_date=info["start_date"],
            end_date=info["end_date"],
            symbols=info["symbols"],
            initial_capital=Decimal("10000000"),
        )

        analyzer = WalkForwardAnalyzer(engine)
        result = await analyzer.run(config, window_months=2, oos_months=1)

        assert result.windows, "Walk-forward should produce at least 1 window"
        assert result.consistency_ratio >= Decimal("0")
        assert result.consistency_ratio <= Decimal("1")
        assert isinstance(result.is_robust, bool)
        assert result.window_months == 2
        assert result.oos_months == 1

        # 각 윈도우 구조 검증
        for w in result.windows:
            assert w.is_start < w.is_end
            assert w.oos_start < w.oos_end
            assert w.is_end < w.oos_start
            assert w.is_metrics is not None
            assert w.oos_metrics is not None

    # ── 7. 거래 내역 DB 저장 ───────────────────────────────────────

    async def test_trades_persisted(self, client):
        """GET /runs/{id}?include_trades=true → 거래 내역 존재."""
        run_id = TestBacktestE2E._position_run_id
        assert run_id is not None, "Scenario 1 must run first"

        resp = await client.get(
            f"/api/backtest/runs/{run_id}",
            params={"include_trades": "true"},
        )
        assert resp.status_code == 200
        data = resp.json()

        assert "trades" in data
        assert isinstance(data["trades"], list)

        # 거래가 있으면 구조 검증
        if data["trades"]:
            trade = data["trades"][0]
            for key in ("symbol", "side", "quantity", "price", "commission", "trade_date"):
                assert key in trade, f"Trade missing key: {key}"

    # ── 8. 빈 기간 처리 ────────────────────────────────────────────

    async def test_empty_date_range(self, client):
        """데이터 없는 기간 → COMPLETED, 0 trades, 에러 없음."""
        resp = await client.post(
            "/api/backtest/run",
            json={
                "strategy_type": "position",
                "mode": "technical",
                "start_date": "2020-01-01",
                "end_date": "2020-01-31",
                "symbols": ["005930"],
                "initial_capital": "10000000",
            },
        )
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]

        data = await _poll_until_complete(client, run_id)
        run = data["run"]
        assert run["status"] == "completed", f"Expected completed, got {run['status']}: {run.get('error_message')}"
        assert run["total_trades"] == 0
        assert run["error_message"] is None
