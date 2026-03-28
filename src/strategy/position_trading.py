"""PositionTradingStrategy — 포지션 트레이딩 전략 (주~월 단위).

Strategy ABC를 상속하여 중장기 포지션 트레이딩을 수행한다.
KOSPI/KOSDAQ 대형주 중 유동성이 충분한 종목을 대상으로,
LLM 분석 + 기술/펀더멘털 조건을 결합하여 매매 시그널을 생성한다.

전략 특성:
    - 분석 주기: 주 1~2회
    - 유니버스: 시총 5000억↑, 20일 평균 거래대금 10억↑
    - 진입 조건: LLM BUY (confidence ≥ 0.60) + SMA20 > SMA60 + 펀더멘털 점수 50↑
    - 손절: ATR × 2
    - 익절: ATR × 3
    - 트레일링: 수익 5% 후 활성화, ATR × 1.5 기반 트레일
    - 최대 보유: 60 거래일
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd
import structlog
from sqlalchemy import func, select

from src.analysis.technical.indicators import calculate_sma
from src.core.enums import (
    AgentType,
    DecisionAction,
    SignalAction,
    StrategyType,
)
from src.core.models import OHLCV, ExitSignal, PipelineResult, Signal
from src.db.models.market_data import DailyOHLCV, StockMaster
from src.strategy.base import Strategy
from src.strategy.exit_calculator import ExitPriceCalculator
from src.strategy.registry import register_strategy
from src.strategy.sizing import PositionSizer

if TYPE_CHECKING:
    from src.db.models.strategy import PositionRecord

logger = structlog.get_logger(__name__)


@register_strategy(StrategyType.POSITION)
class PositionTradingStrategy(Strategy):
    """포지션 트레이딩 전략 — 주~월 단위 중장기 매매.

    대형주 중심의 추세 추종 전략으로, LLM 정성적 분석과 기술/펀더멘털
    정량적 조건을 결합하여 높은 확신도의 매매 시그널만 실행한다.
    ATR 기반 동적 손절/익절로 변동성에 적응하며, 트레일링 스톱으로
    수익을 보호한다.
    """

    # ── 전략 파라미터 (상수) ──────────────────────────────────────────
    # 유니버스 필터링 기준
    MIN_MARKET_CAP: int = 500_000_000_000  # 시총 5000억원 이상
    MIN_AVG_TRADING_VALUE: int = 1_000_000_000  # 20일 평균 거래대금 10억원 이상
    TRADING_VALUE_LOOKBACK: int = 20  # 거래대금 평균 계산 기간 (거래일)

    # 진입 조건 임계값
    MIN_CONFIDENCE: Decimal = Decimal("0.60")  # LLM confidence 최소 0.60
    MIN_FUNDAMENTAL_SCORE: Decimal = Decimal("50")  # 펀더멘털 점수 최소 50점
    # SMA20 > SMA60: 상승 추세 확인 (20일 이동평균 > 60일 이동평균)

    # ATR 기반 손절/익절 배수
    ATR_PERIOD: int = 14  # ATR 계산 기간
    ATR_STOP_MULT: Decimal = Decimal("2.0")  # 손절 = 진입가 - ATR × 2
    ATR_TP_MULT: Decimal = Decimal("3.0")  # 익절 = 진입가 + ATR × 3

    # 트레일링 스톱 설정
    # 수익률이 5% 이상일 때 트레일링 스톱 활성화
    TRAILING_ACTIVATE_PCT: Decimal = Decimal("5.0")
    # 트레일링 폭 = ATR × 1.5를 진입가 대비 비율(%)로 변환하여 사용
    TRAILING_ATR_MULT: Decimal = Decimal("1.5")

    # 최대 보유 기간 (거래일 기준)
    MAX_HOLDING_DAYS: int = 60

    # OHLCV 조회 기간 (SMA60 + 여유분)
    OHLCV_LOOKBACK_DAYS: int = 120

    # ── Abstract 메서드 구현 ──────────────────────────────────────────

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.POSITION

    async def scan_universe(self) -> list[str]:
        """유니버스 스캔 — 포지션 트레이딩 대상 종목 필터링.

        # 상세 로직:
        # 1. StockMaster에서 KOSPI/KOSDAQ 활성 종목 중 시총 5000억↑ 조회
        # 2. DailyOHLCV에서 최근 20일 평균 거래대금 10억↑ 필터 (DB 집계)
        # 3. 이미 보유 중인 포지션 트레이딩 종목 제외
        # 4. 최종 종목 코드 리스트 반환
        """
        async with self._session_factory() as session:
            # Step 1: 시총 조건을 만족하는 활성 종목 조회
            # KOSPI/KOSDAQ 시장의 시총 5000억원 이상 종목만 선별
            master_stmt = (
                select(StockMaster.symbol)
                .where(
                    StockMaster.is_active.is_(True),
                    StockMaster.market_type.in_(["kospi", "kosdaq"]),
                    StockMaster.market_cap_krw >= self.MIN_MARKET_CAP,
                )
            )
            master_result = await session.execute(master_stmt)
            large_cap_symbols = [row[0] for row in master_result.all()]

            if not large_cap_symbols:
                logger.info("scan_universe.no_large_cap", strategy="position")
                return []

            # Step 2: 최근 20일 평균 거래대금 필터 (DB aggregate)
            # trading_value가 NULL인 경우를 고려하여 COALESCE 사용
            # 전체 OHLCV를 Python에 로딩하지 않고 DB에서 집계
            ohlcv_stmt = (
                select(
                    DailyOHLCV.symbol,
                    func.avg(func.coalesce(DailyOHLCV.trading_value, 0)).label(
                        "avg_value"
                    ),
                )
                .where(
                    DailyOHLCV.symbol.in_(large_cap_symbols),
                    DailyOHLCV.date
                    >= func.current_date() - self.TRADING_VALUE_LOOKBACK,
                )
                .group_by(DailyOHLCV.symbol)
                .having(
                    func.avg(func.coalesce(DailyOHLCV.trading_value, 0))
                    >= self.MIN_AVG_TRADING_VALUE
                )
            )
            ohlcv_result = await session.execute(ohlcv_stmt)
            liquid_symbols = [row[0] for row in ohlcv_result.all()]

        if not liquid_symbols:
            logger.info("scan_universe.no_liquid", strategy="position")
            return []

        # Step 3: 이미 보유 중인 포지션 트레이딩 종목 제외
        # 동일 전략 타입의 오픈 포지션을 조회하여 중복 진입 방지
        open_positions = await self.get_open_positions(
            strategy_type=StrategyType.POSITION
        )
        held_symbols = {pos.symbol for pos in open_positions}
        candidates = [s for s in liquid_symbols if s not in held_symbols]

        logger.info(
            "scan_universe.result",
            strategy="position",
            large_cap=len(large_cap_symbols),
            liquid=len(liquid_symbols),
            held=len(held_symbols),
            candidates=len(candidates),
        )
        return candidates

    async def generate_signals(
        self, pipeline_result: PipelineResult
    ) -> list[Signal]:
        """매매 시그널 생성 — PipelineResult를 분석하여 진입 시그널 산출.

        # 상세 로직:
        # 1. pipeline_result.trade_decisions에서 BUY 결정만 추출
        # 2. 대응하는 stock_analysis에서 confidence ≥ 0.60, fundamental_score ≥ 50 확인
        # 3. 기술 지표: SMA20 > SMA60 (상승 추세 확인)
        # 4. ATR 계산 → ExitPriceCalculator로 손절/익절가 산출
        # 5. PositionSizer로 포지션 크기 계산
        # 6. AlgoRiskManager로 정량적 리스크 체크
        # 7. 모든 조건 통과 시 Signal 생성
        """
        signals: list[Signal] = []

        # trade_decisions → stock_analyses 매핑 (symbol 기준)
        analysis_map = {a.symbol: a for a in pipeline_result.stock_analyses}

        # 포트폴리오 상태 조회 (사이징과 리스크 체크에 필요)
        portfolio_state = await self._portfolio_service.get_current_state()
        sizer = PositionSizer(self._settings)

        for decision in pipeline_result.trade_decisions:
            # Step 1: BUY 결정만 처리
            if decision.action != DecisionAction.BUY:
                continue

            analysis = analysis_map.get(decision.symbol)
            if analysis is None:
                logger.warning(
                    "generate_signals.no_analysis",
                    symbol=decision.symbol,
                )
                continue

            # Step 2: confidence 체크 — LLM이 0.60 이상 확신해야 진입
            if analysis.confidence < self.MIN_CONFIDENCE:
                logger.debug(
                    "generate_signals.low_confidence",
                    symbol=decision.symbol,
                    confidence=str(analysis.confidence),
                )
                continue

            # Step 2: 펀더멘털 점수 체크 — 재무 건전성 50점 이상
            if analysis.fundamental_score < self.MIN_FUNDAMENTAL_SCORE:
                logger.debug(
                    "generate_signals.low_fundamental",
                    symbol=decision.symbol,
                    score=str(analysis.fundamental_score),
                )
                continue

            # Step 3: SMA 추세 확인 — SMA20 > SMA60이면 상승 추세
            # 브로커에서 OHLCV 조회 후 SMA 계산
            ohlcv_list = await self._broker.get_daily_ohlcv(
                decision.symbol, period_days=self.OHLCV_LOOKBACK_DAYS
            )
            if len(ohlcv_list) < 60:
                # SMA60 계산에 최소 60일 데이터 필요
                logger.debug(
                    "generate_signals.insufficient_data",
                    symbol=decision.symbol,
                    bars=len(ohlcv_list),
                )
                continue

            close_series = pd.Series(
                [float(bar.close) for bar in ohlcv_list],
                index=[bar.date for bar in ohlcv_list],
            )
            sma_dict = calculate_sma(close_series, periods=[20, 60])
            latest_sma20 = sma_dict[20].iloc[-1]
            latest_sma60 = sma_dict[60].iloc[-1]

            if latest_sma20 <= latest_sma60:
                # SMA20이 SMA60 이하이면 하락/횡보 추세 → 진입 보류
                logger.debug(
                    "generate_signals.downtrend",
                    symbol=decision.symbol,
                    sma20=f"{latest_sma20:.0f}",
                    sma60=f"{latest_sma60:.0f}",
                )
                continue

            # Step 4: ATR 계산 → 손절/익절가 산출
            # ATR은 변동성 지표로, 이를 기반으로 동적 손절/익절 설정
            atr = self._calculate_atr(ohlcv_list, period=self.ATR_PERIOD)
            if atr <= Decimal("0"):
                logger.warning(
                    "generate_signals.zero_atr",
                    symbol=decision.symbol,
                )
                continue

            # 진입가 결정: LLM이 지정한 가격 또는 현재가 사용
            entry_price = decision.price or analysis.current_price
            if entry_price is None or entry_price <= Decimal("0"):
                logger.warning(
                    "generate_signals.no_price",
                    symbol=decision.symbol,
                )
                continue

            # ATR 기반 손절/익절 계산
            # 손절 = 진입가 - ATR × 2 (변동성의 2배 하락 시 손절)
            # 익절 = 진입가 + ATR × 3 (Risk:Reward = 1:1.5)
            stop_loss, take_profit = ExitPriceCalculator.atr_based(
                entry_price=entry_price,
                atr=atr,
                stop_mult=self.ATR_STOP_MULT,
                tp_mult=self.ATR_TP_MULT,
            )

            # Step 5: 포지션 사이징 — 리스크 기반 수량 계산
            # Fixed Fractional 방식: 1건당 리스크 비율에 맞춰 수량 결정
            sizing = sizer.calculate(
                symbol=decision.symbol,
                entry_price=entry_price,
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
                total_portfolio_value=portfolio_state.total_value,
            )

            if sizing.quantity <= 0:
                logger.debug(
                    "generate_signals.zero_quantity",
                    symbol=decision.symbol,
                )
                continue

            # Step 6: 알고리즘 리스크 체크 — 8개 정량적 규칙 검증
            # Phase 3 LLM 정성적 리스크를 통과한 후, Phase 4 알고리즘 정량적 체크
            # 섹터 정보 조회 (리스크 매니저의 섹터 집중도 체크에 필요)
            sector = await self._get_sector(decision.symbol)

            risk_result = await self._risk_manager.check(
                symbol=decision.symbol,
                action=SignalAction.BUY,
                quantity=sizing.quantity,
                price=entry_price,
                stop_loss_price=stop_loss,
                sector=sector,
            )

            if not risk_result.passed or risk_result.adjusted_quantity <= 0:
                logger.info(
                    "generate_signals.risk_blocked",
                    symbol=decision.symbol,
                    violations=risk_result.violations,
                    adjusted_qty=risk_result.adjusted_quantity,
                )
                continue

            # 리스크 매니저가 수량을 조정한 경우 반영
            final_quantity = risk_result.adjusted_quantity
            final_value = entry_price * final_quantity

            # Step 7: Signal 생성 — 모든 조건 통과
            signal = Signal(
                symbol=decision.symbol,
                action=SignalAction.BUY,
                confidence=analysis.confidence,
                target_price=take_profit,
                stop_loss_price=stop_loss,
                quantity=final_quantity,
                position_value_krw=final_value,
                reasoning=(
                    f"Position 전략 진입: confidence={analysis.confidence}, "
                    f"fundamental={analysis.fundamental_score}, "
                    f"SMA20({latest_sma20:.0f})>SMA60({latest_sma60:.0f}), "
                    f"ATR={atr:.0f}, SL={stop_loss:.0f}, TP={take_profit:.0f}, "
                    f"qty={final_quantity}"
                ),
                source_agent=AgentType.TRADER,
                timestamp=datetime.now(),
            )
            signals.append(signal)

            logger.info(
                "generate_signals.signal_created",
                symbol=decision.symbol,
                quantity=final_quantity,
                entry=str(entry_price),
                stop_loss=str(stop_loss),
                take_profit=str(take_profit),
            )

        logger.info(
            "generate_signals.complete",
            strategy="position",
            decisions=len(pipeline_result.trade_decisions),
            signals=len(signals),
        )
        return signals

    async def check_exit_conditions(
        self, position: PositionRecord
    ) -> ExitSignal | None:
        """청산 조건 체크 — 포지션별 손절/익절/트레일링/시간 기반 청산 판단.

        # 상세 로직 (우선순위 순서):
        # 1. 손절 체크 (최우선) — 현재가 ≤ 손절가이면 즉시 청산
        # 2. 트레일링 스톱 — 수익 5%↑ 후 고점 대비 ATR×1.5 하락 시 청산
        # 3. 익절 체크 — 현재가 ≥ 익절가이면 장 마감 시 청산
        # 4. 시간 기반 청산 — 60 거래일 초과 보유 시 다음 세션에 청산
        # 5. 모두 해당 없으면 None 반환 (포지션 유지)
        """
        # 현재가 조회
        price_info = await self._broker.get_price(position.symbol)
        current_price = price_info.current_price

        # 미실현 손익률 계산 (%)
        unrealized_pnl_pct = (
            (current_price - position.entry_price) / position.entry_price
        ) * Decimal("100")

        checker = self._exit_checker

        # ── 1. 손절 체크 (우선순위 최고) ──
        # 현재가가 손절가 이하이면 즉시 청산하여 추가 손실 방지
        signal = checker.check_stop_loss(position, current_price, unrealized_pnl_pct)
        if signal is not None:
            return signal

        # ── 2. 트레일링 스톱 ──
        # 수익률이 TRAILING_ACTIVATE_PCT(5%) 이상일 때만 활성화
        # 진입 후 최고가 대비 일정 비율 하락하면 수익 보호를 위해 청산
        if unrealized_pnl_pct >= self.TRAILING_ACTIVATE_PCT:
            trailing_stop = await self._calculate_trailing_stop(position)
            if trailing_stop is not None:
                signal = checker.check_trailing_stop(
                    position, current_price, unrealized_pnl_pct, trailing_stop
                )
                if signal is not None:
                    return signal

        # ── 3. 익절 체크 ──
        # 현재가가 익절가 이상이면 장 마감 시 청산 (급하지 않음)
        signal = checker.check_take_profit(position, current_price, unrealized_pnl_pct)
        if signal is not None:
            return signal

        # ── 4. 시간 기반 청산 ──
        # 최대 보유 기간(60 거래일) 초과 시 다음 세션에 정리
        signal = checker.check_time_based(
            position, current_price, unrealized_pnl_pct, date.today(),
            time_urgency="next_session",
        )
        if signal is not None:
            return signal

        # 5. 모든 청산 조건 미해당 → 포지션 유지
        return None

    # ── Private 헬퍼 ─────────────────────────────────────────────────

    async def _calculate_trailing_stop(
        self, position: PositionRecord
    ) -> Decimal | None:
        """트레일링 스톱 가격 계산.

        진입 후 OHLCV에서 최고가를 조회하고, ATR 기반으로 트레일링 비율을
        계산하여 트레일링 스톱 가격을 반환한다.

        Returns
        -------
        Decimal | None — 트레일링 스톱 가격. 데이터 부족 시 None.
        """
        # 진입 후 OHLCV 조회
        ohlcv_list = await self._broker.get_daily_ohlcv(
            position.symbol, period_days=self.OHLCV_LOOKBACK_DAYS
        )

        # 진입일 이후의 데이터만 필터링
        entry_bars = [bar for bar in ohlcv_list if bar.date >= position.entry_date]
        if not entry_bars:
            return None

        # 진입 후 최고가
        highest_since_entry = max(bar.high for bar in entry_bars)

        # 트레일링 비율 계산
        # 고정 비율 대신 ATR 기반 동적 비율 사용:
        # trailing_pct = (ATR × TRAILING_ATR_MULT) / 진입가 × 100
        if len(ohlcv_list) >= self.ATR_PERIOD + 1:
            atr = self._calculate_atr(ohlcv_list, period=self.ATR_PERIOD)
            trailing_amount = atr * self.TRAILING_ATR_MULT
            # 진입가 대비 트레일링 비율(%)로 변환
            trailing_pct = (trailing_amount / position.entry_price) * Decimal("100")
        elif position.trailing_stop_pct is not None:
            # ATR 계산 불가 시 DB에 저장된 trailing_stop_pct 사용
            trailing_pct = position.trailing_stop_pct
        else:
            return None

        return ExitPriceCalculator.trailing_stop_price(
            highest_since_entry=highest_since_entry,
            trailing_pct=trailing_pct,
        )

    async def _get_sector(self, symbol: str) -> str:
        """종목의 섹터 정보를 DB에서 조회한다."""
        async with self._session_factory() as session:
            stmt = select(StockMaster.sector).where(StockMaster.symbol == symbol)
            result = await session.execute(stmt)
            sector = result.scalar_one_or_none()
            return sector or "기타"

    @staticmethod
    def _calculate_atr(ohlcv_list: list[OHLCV], period: int = 14) -> Decimal:
        """ATR(Average True Range) 계산 — SMA 방식.

        # ATR 계산 상세:
        # 1. True Range = max(고가-저가, |고가-전일종가|, |저가-전일종가|)
        #    - 고가-저가: 당일 변동 폭
        #    - |고가-전일종가|: 갭업 후 변동
        #    - |저가-전일종가|: 갭다운 후 변동
        # 2. ATR = 최근 period일 TR의 단순 이동 평균 (SMA)
        # 3. 변동성이 클수록 ATR이 높아져 손절/익절 폭이 넓어짐

        Parameters
        ----------
        ohlcv_list : 날짜 오름차순 정렬된 OHLCV 리스트
        period : ATR 평균 기간 (기본 14일)

        Returns
        -------
        Decimal — ATR 값 (소수점 이하 포함)
        """
        if len(ohlcv_list) < period + 1:
            # ATR 계산에 최소 period+1개 봉 필요 (전일 종가 참조)
            return Decimal("0")

        true_ranges: list[Decimal] = []
        for i in range(1, len(ohlcv_list)):
            high = ohlcv_list[i].high
            low = ohlcv_list[i].low
            prev_close = ohlcv_list[i - 1].close

            # True Range: 세 값 중 최대값
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
            true_ranges.append(tr)

        # 최근 period일의 TR만 사용하여 SMA 계산
        recent_trs = true_ranges[-period:]
        atr = sum(recent_trs) / Decimal(str(period))
        return atr
