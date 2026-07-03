"""Performance metrics calculator — stateless, Phase 7 reusable.

모든 계산은 Decimal 정밀도로 수행. float 변환 없음.
입력: PositionRecord(청산 포지션) + PortfolioSnapshot(일별 스냅샷).
출력: PerformanceMetrics.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from src.core.models import PerformanceMetrics

if TYPE_CHECKING:
    from src.db.models.strategy import PortfolioSnapshot, PositionRecord

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_Q2 = Decimal("0.01")
_Q4 = Decimal("0.0001")


class PerformanceCalculator:
    """Stateless 성과 지표 계산기.

    Phase 7 백테스팅에서 동일 모듈 재사용.
    모든 메서드는 @staticmethod.
    """

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------

    @staticmethod
    def calculate(
        *,
        closed_positions: list[PositionRecord],
        snapshots: list[PortfolioSnapshot],
        period_start: date,
        period_end: date,
        risk_free_rate_pct: Decimal = Decimal("3.5"),
    ) -> PerformanceMetrics:
        """전체 성과 지표 한 번에 계산.

        1. closed_positions → 승률, 평균손익, Profit Factor
        2. snapshots → 일별 수익률 시리즈 → Sharpe, Sortino, MDD
        3. 총 수익률, 연환산 수익률
        """
        daily_returns = PerformanceCalculator._daily_returns_from_snapshots(snapshots)

        sharpe = PerformanceCalculator.calculate_sharpe_ratio(
            daily_returns, risk_free_rate_annual_pct=risk_free_rate_pct,
        )
        sortino = PerformanceCalculator.calculate_sortino_ratio(
            daily_returns, risk_free_rate_annual_pct=risk_free_rate_pct,
        )
        mdd = PerformanceCalculator.calculate_max_drawdown(snapshots)

        win_rate, wins, losses = PerformanceCalculator.calculate_win_rate(closed_positions)
        profit_factor = PerformanceCalculator.calculate_profit_factor(closed_positions)
        avg_win, avg_loss = PerformanceCalculator.calculate_avg_win_loss(closed_positions)

        # 총 수익률
        sorted_snaps = sorted(snapshots, key=lambda s: s.snapshot_date)
        if len(sorted_snaps) >= 2 and sorted_snaps[0].total_value != _ZERO:
            total_return = (
                (sorted_snaps[-1].total_value - sorted_snaps[0].total_value)
                / sorted_snaps[0].total_value
                * _HUNDRED
            ).quantize(_Q2, rounding=ROUND_HALF_UP)
        else:
            total_return = _ZERO

        # 연환산 수익률
        annualized: Decimal | None = None
        days = (period_end - period_start).days
        if days > 0 and len(sorted_snaps) >= 2 and total_return != _ZERO:
            base = _ONE + total_return / _HUNDRED
            exponent = Decimal("365") / Decimal(days)
            annualized = ((base ** exponent - _ONE) * _HUNDRED).quantize(
                _Q2, rounding=ROUND_HALF_UP,
            )

        return PerformanceMetrics(
            period_start=period_start,
            period_end=period_end,
            total_return_pct=total_return,
            annualized_return_pct=annualized,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            max_drawdown_pct=mdd,
            win_rate_pct=win_rate,
            avg_win_pct=avg_win,
            avg_loss_pct=avg_loss,
            profit_factor=profit_factor,
            total_trades=wins + losses,
            winning_trades=wins,
            losing_trades=losses,
        )

    # ------------------------------------------------------------------
    # Sharpe / Sortino
    # ------------------------------------------------------------------

    @staticmethod
    def _rf_daily_pct(
        risk_free_rate_annual_pct: Decimal,
        trading_days: int,
    ) -> Decimal:
        """연간 무위험수익률(%) → 일간 무위험수익률(%)."""
        return (
            (_ONE + risk_free_rate_annual_pct / _HUNDRED)
            ** (_ONE / Decimal(trading_days))
            - _ONE
        ) * _HUNDRED

    @staticmethod
    def calculate_sharpe_ratio(
        daily_returns: list[Decimal],
        *,
        risk_free_rate_annual_pct: Decimal = Decimal("3.5"),
        trading_days: int = 252,
    ) -> Decimal | None:
        """Sharpe Ratio = (mean_return - rf_daily) / std(returns) * sqrt(trading_days).

        daily_returns가 2개 미만이면 None 반환.
        """
        n = len(daily_returns)
        if n < 2:
            return None

        rf_daily = PerformanceCalculator._rf_daily_pct(
            risk_free_rate_annual_pct, trading_days,
        )
        n_dec = Decimal(n)
        mean_ret = sum(daily_returns) / n_dec

        variance = sum((r - mean_ret) ** 2 for r in daily_returns) / n_dec
        std_dev = variance.sqrt()

        if std_dev == _ZERO:
            return None

        sharpe = (mean_ret - rf_daily) / std_dev * Decimal(trading_days).sqrt()
        return sharpe.quantize(_Q4, rounding=ROUND_HALF_UP)

    @staticmethod
    def calculate_sortino_ratio(
        daily_returns: list[Decimal],
        *,
        risk_free_rate_annual_pct: Decimal = Decimal("3.5"),
        trading_days: int = 252,
    ) -> Decimal | None:
        """Sortino Ratio = (mean_return - rf_daily) / downside_std * sqrt(trading_days).

        downside_std: rf_daily 미만인 수익률만 편차 계산.
        하방 수익률이 없거나 daily_returns < 2이면 None.
        """
        n = len(daily_returns)
        if n < 2:
            return None

        rf_daily = PerformanceCalculator._rf_daily_pct(
            risk_free_rate_annual_pct, trading_days,
        )

        downside = [r for r in daily_returns if r < rf_daily]
        if not downside:
            return None

        downside_variance = sum(
            (r - rf_daily) ** 2 for r in downside
        ) / Decimal(len(downside))
        downside_std = downside_variance.sqrt()

        if downside_std == _ZERO:
            return None

        mean_ret = sum(daily_returns) / Decimal(n)
        sortino = (mean_ret - rf_daily) / downside_std * Decimal(trading_days).sqrt()
        return sortino.quantize(_Q4, rounding=ROUND_HALF_UP)

    # ------------------------------------------------------------------
    # Max Drawdown
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_max_drawdown(
        snapshots: list[PortfolioSnapshot],
    ) -> Decimal:
        """MDD = max((peak - trough) / peak * 100).

        snapshots가 비어있으면 Decimal("0") 반환.
        """
        if not snapshots:
            return _ZERO

        sorted_snaps = sorted(snapshots, key=lambda s: s.snapshot_date)
        peak = sorted_snaps[0].total_value
        max_dd = _ZERO

        for snap in sorted_snaps:
            if snap.total_value > peak:
                peak = snap.total_value
            if peak > _ZERO:
                dd = (peak - snap.total_value) / peak * _HUNDRED
                if dd > max_dd:
                    max_dd = dd

        return max_dd.quantize(_Q2, rounding=ROUND_HALF_UP)

    # ------------------------------------------------------------------
    # Win Rate / Profit Factor / Avg Win-Loss
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_win_rate(
        closed_positions: list[PositionRecord],
    ) -> tuple[Decimal, int, int]:
        """승률 계산.

        Returns: (win_rate_pct, winning_count, losing_count)
        realized_pnl > 0 → 승, <= 0 → 패.
        """
        wins = 0
        losses = 0
        for p in closed_positions:
            if p.realized_pnl is not None and p.realized_pnl > _ZERO:
                wins += 1
            elif p.realized_pnl is not None:
                losses += 1

        total = wins + losses
        if total == 0:
            return (_ZERO, 0, 0)

        rate = (Decimal(wins) / Decimal(total) * _HUNDRED).quantize(
            _Q2, rounding=ROUND_HALF_UP,
        )
        return (rate, wins, losses)

    @staticmethod
    def calculate_profit_factor(
        closed_positions: list[PositionRecord],
    ) -> Decimal | None:
        """Profit Factor = 총 이익 / |총 손실|.

        손실이 0이면 None (무한). 이익이 0이면 Decimal("0").
        """
        total_profit = _ZERO
        total_loss = _ZERO

        for p in closed_positions:
            if p.realized_pnl is not None and p.realized_pnl > _ZERO:
                total_profit += p.realized_pnl
            elif p.realized_pnl is not None and p.realized_pnl < _ZERO:
                total_loss += abs(p.realized_pnl)

        if total_profit == _ZERO and total_loss == _ZERO:
            return None
        if total_loss == _ZERO:
            return None
        if total_profit == _ZERO:
            return _ZERO

        return (total_profit / total_loss).quantize(_Q2, rounding=ROUND_HALF_UP)

    @staticmethod
    def calculate_avg_win_loss(
        closed_positions: list[PositionRecord],
    ) -> tuple[Decimal, Decimal]:
        """평균 수익/손실 비율 (%) 계산.

        Returns: (avg_win_pct, avg_loss_pct)
        avg_loss_pct는 절대값(양수).
        """
        win_pcts: list[Decimal] = []
        loss_pcts: list[Decimal] = []

        for p in closed_positions:
            if p.entry_price is None or p.entry_price == _ZERO or p.exit_price is None:
                continue
            pct = (p.exit_price - p.entry_price) / p.entry_price * _HUNDRED
            if p.realized_pnl is not None and p.realized_pnl > _ZERO:
                win_pcts.append(pct)
            elif p.realized_pnl is not None:
                loss_pcts.append(abs(pct))

        avg_win = _ZERO
        if win_pcts:
            avg_win = (sum(win_pcts) / Decimal(len(win_pcts))).quantize(
                _Q2, rounding=ROUND_HALF_UP,
            )

        avg_loss = _ZERO
        if loss_pcts:
            avg_loss = (sum(loss_pcts) / Decimal(len(loss_pcts))).quantize(
                _Q2, rounding=ROUND_HALF_UP,
            )

        return (avg_win, avg_loss)

    # ------------------------------------------------------------------
    # Daily Returns Helper
    # ------------------------------------------------------------------

    @staticmethod
    def _daily_returns_from_snapshots(
        snapshots: list[PortfolioSnapshot],
    ) -> list[Decimal]:
        """포트폴리오 스냅샷 → 일별 수익률(%) 시리즈.

        return_i = (total_value_i - total_value_{i-1}) / total_value_{i-1} * 100
        첫 번째 스냅샷은 기준점이므로 반환 리스트 길이 = len(snapshots) - 1.
        """
        if len(snapshots) < 2:
            return []

        sorted_snaps = sorted(snapshots, key=lambda s: s.snapshot_date)
        returns: list[Decimal] = []

        for i in range(1, len(sorted_snaps)):
            prev_val = sorted_snaps[i - 1].total_value
            if prev_val == _ZERO:
                continue
            curr_val = sorted_snaps[i].total_value
            ret = (curr_val - prev_val) / prev_val * _HUNDRED
            returns.append(ret)

        return returns

    # ------------------------------------------------------------------
    # Breakdown Methods
    # ------------------------------------------------------------------

    @staticmethod
    def breakdown_by_strategy(
        closed_positions: list[PositionRecord],
    ) -> dict[str, dict]:
        """전략별(position/swing) 성과 분석.

        Returns: {
            "position": {"trade_count": int, "win_rate_pct": Decimal,
                         "avg_pnl_pct": Decimal, "total_pnl": Decimal},
            "swing": {...},
        }
        """
        groups: dict[str, list[PositionRecord]] = defaultdict(list)
        for p in closed_positions:
            groups[p.strategy_type].append(p)

        result: dict[str, dict] = {}
        for strategy, positions in groups.items():
            win_rate, wins, losses = PerformanceCalculator.calculate_win_rate(positions)
            total_pnl = sum(
                p.realized_pnl for p in positions if p.realized_pnl is not None
            )

            pnl_pcts: list[Decimal] = []
            for p in positions:
                if p.entry_price and p.entry_price != _ZERO and p.exit_price is not None:
                    pnl_pcts.append(
                        (p.exit_price - p.entry_price) / p.entry_price * _HUNDRED,
                    )

            avg_pnl = _ZERO
            if pnl_pcts:
                avg_pnl = (sum(pnl_pcts) / Decimal(len(pnl_pcts))).quantize(
                    _Q2, rounding=ROUND_HALF_UP,
                )

            result[strategy] = {
                "trade_count": len(positions),
                "win_rate_pct": win_rate,
                "avg_pnl_pct": avg_pnl,
                "total_pnl": total_pnl,
            }

        return result

    @staticmethod
    def breakdown_by_trigger(
        closed_positions: list[PositionRecord],
    ) -> dict[str, dict]:
        """진입 트리거 태그별 성과 분석 (F-14, 스윙 기술 셋업).

        한 포지션이 복수 태그(예: RSI과매도반전+MACD골든크로스)를 가지면 각 태그
        버킷에 **중복 집계**된다(태그 단독 효과가 아니라 "해당 셋업이 존재한 진입"의
        성과). entry_trigger가 비어 있는 포지션은 스킵한다. LLM이 실제 진입을 결정하므로
        인과가 아닌 상관 관측이다.

        Returns: {
            "RSI과매도반전": {"trade_count": int, "win_rate_pct": Decimal,
                            "avg_pnl_pct": Decimal, "total_pnl": Decimal},
            "MACD골든크로스": {...},
        }
        """
        groups: dict[str, list[PositionRecord]] = defaultdict(list)
        for p in closed_positions:
            for tag in p.entry_trigger or []:
                groups[tag].append(p)

        result: dict[str, dict] = {}
        for tag in sorted(groups):
            positions = groups[tag]
            win_rate, _, _ = PerformanceCalculator.calculate_win_rate(positions)
            total_pnl = sum(
                p.realized_pnl for p in positions if p.realized_pnl is not None
            )

            pnl_pcts: list[Decimal] = []
            for p in positions:
                if p.entry_price and p.entry_price != _ZERO and p.exit_price is not None:
                    pnl_pcts.append(
                        (p.exit_price - p.entry_price) / p.entry_price * _HUNDRED,
                    )

            avg_pnl = _ZERO
            if pnl_pcts:
                avg_pnl = (sum(pnl_pcts) / Decimal(len(pnl_pcts))).quantize(
                    _Q2, rounding=ROUND_HALF_UP,
                )

            result[tag] = {
                "trade_count": len(positions),
                "win_rate_pct": win_rate,
                "avg_pnl_pct": avg_pnl,
                "total_pnl": total_pnl,
            }

        return result

    @staticmethod
    def breakdown_by_month(
        closed_positions: list[PositionRecord],
    ) -> dict[str, dict]:
        """월별 성과 분석. exit_date 기준으로 그룹화.

        Returns: {
            "2026-01": {"trade_count": int, "win_rate_pct": Decimal,
                        "total_pnl": Decimal},
            "2026-02": {...},
        }
        """
        groups: dict[str, list[PositionRecord]] = defaultdict(list)
        for p in closed_positions:
            if p.exit_date is None:
                continue
            key = p.exit_date.strftime("%Y-%m")
            groups[key].append(p)

        result: dict[str, dict] = {}
        for month_key in sorted(groups):
            positions = groups[month_key]
            win_rate, _, _ = PerformanceCalculator.calculate_win_rate(positions)
            total_pnl = sum(
                p.realized_pnl for p in positions if p.realized_pnl is not None
            )

            result[month_key] = {
                "trade_count": len(positions),
                "win_rate_pct": win_rate,
                "total_pnl": total_pnl,
            }

        return result
