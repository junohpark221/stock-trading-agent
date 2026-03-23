"""SwingTradingStrategy — 스윙 트레이딩 전략 (일~주 단위).

Strategy ABC를 상속하여 단기 스윙 트레이딩을 수행한다.
거래대금 상위 종목 중 적정 변동성 범위의 종목을 대상으로,
기술 지표 조합 + LLM 분석을 결합하여 매매 시그널을 생성한다.

전략 특성:
    - 분석 주기: 매일 장 마감 후
    - 유니버스: 거래대금 상위 50, 시총 1000억↑, 변동성 2~8%
    - 진입 조건: 기술 지표 3개 중 2개↑ 충족 + LLM BUY (confidence ≥ 0.50)
    - 기술 조건: RSI 과매도 반전, MACD 골든크로스, BB 하단 반등
    - 손절: 고정 3%
    - 익절: 고정 5%
    - 트레일링: 수익 3% 후 고점 대비 2% 트레일
    - 최대 보유: 10 거래일
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd
import structlog
from sqlalchemy import func, select

from src.analysis.technical.indicators import (
    calculate_bollinger_bands,
    calculate_macd,
    calculate_rsi,
)
from src.core.enums import (
    AgentType,
    DecisionAction,
    ExitReason,
    SignalAction,
    StrategyType,
)
from src.core.models import OHLCV, ExitSignal, PipelineResult, Signal
from src.db.models.market_data import DailyOHLCV, StockMaster
from src.strategy.base import Strategy
from src.strategy.exit_calculator import ExitPriceCalculator
from src.strategy.sizing import PositionSizer

if TYPE_CHECKING:
    from src.db.models.strategy import PositionRecord

logger = structlog.get_logger(__name__)


class SwingTradingStrategy(Strategy):
    """스윙 트레이딩 전략 — 일~주 단위 단기 매매.

    거래대금 상위 종목 중 적정 변동성(2~8%)을 보이는 종목을 대상으로,
    기술 지표 조합(RSI 과매도 반전, MACD 골든크로스, BB 하단 반등)과
    LLM 분석을 결합하여 단기 반등 매수 시그널을 생성한다.
    고정 비율 손절/익절과 트레일링 스톱으로 리스크를 관리한다.
    """

    # ── 전략 파라미터 (상수) ──────────────────────────────────────────

    # 유니버스 필터링 기준
    MIN_MARKET_CAP: int = 100_000_000_000  # 시총 1000억원 이상
    VOLUME_TOP_N: int = 50  # 거래대금 상위 50 종목
    MIN_VOLATILITY_PCT: Decimal = Decimal("2.0")  # 최소 변동성 2%
    MAX_VOLATILITY_PCT: Decimal = Decimal("8.0")  # 최대 변동성 8%
    VOLATILITY_LOOKBACK: int = 20  # 변동성 계산 기간 (거래일)

    # 진입 조건 임계값
    MIN_CONFIDENCE: Decimal = Decimal("0.50")  # LLM confidence 최소 0.50
    MIN_TECHNICAL_CONDITIONS: int = 2  # 기술 조건 최소 2개 충족

    # 손절/익절 (고정 비율)
    STOP_LOSS_PCT: Decimal = Decimal("3.0")  # 고정 3% 손절
    TAKE_PROFIT_PCT: Decimal = Decimal("5.0")  # 고정 5% 익절

    # 트레일링 스톱 설정
    # 수익률이 3% 이상일 때 트레일링 스톱 활성화
    TRAILING_ACTIVATE_PCT: Decimal = Decimal("3.0")
    # 진입 후 최고가 대비 2% 하락 시 청산 (고정 비율)
    TRAILING_TRAIL_PCT: Decimal = Decimal("2.0")

    # 최대 보유 기간 (거래일 기준)
    MAX_HOLDING_DAYS: int = 10

    # OHLCV 조회 기간 (BB 20일 + MACD 26일 EMA + 여유분)
    OHLCV_LOOKBACK_DAYS: int = 60

    # ── Abstract 메서드 구현 ──────────────────────────────────────────

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.SWING

    async def scan_universe(self) -> list[str]:
        """유니버스 스캔 — 스윙 트레이딩 대상 종목 필터링.

        # 상세 로직:
        # 1. StockMaster에서 KOSPI/KOSDAQ 활성 종목 중 시총 1000억↑ 조회
        # 2. DailyOHLCV에서 최근 20일 평균 거래대금 상위 50 종목 선별 (DB 집계)
        # 3. 상위 50 종목의 20일 변동성(일간수익률 표준편차) 계산
        #    - 변동성 = 일간 수익률(pct_change)의 표준편차 × 100 (%)
        #    - 2% ≤ 변동성 ≤ 8% 범위만 포함
        #    - 변동성이 너무 낮으면 수익 기회 부족, 너무 높으면 리스크 과다
        # 4. 이미 보유 중인 스윙 트레이딩 종목 제외
        """
        async with self._session_factory() as session:
            # Step 1: 시총 조건을 만족하는 활성 종목 조회
            # KOSPI/KOSDAQ 시장의 시총 1000억원 이상 종목만 선별
            master_stmt = select(StockMaster.symbol).where(
                StockMaster.is_active.is_(True),
                StockMaster.market_type.in_(["kospi", "kosdaq"]),
                StockMaster.market_cap_krw >= self.MIN_MARKET_CAP,
            )
            master_result = await session.execute(master_stmt)
            large_cap_symbols = [row[0] for row in master_result.all()]

            if not large_cap_symbols:
                logger.info("scan_universe.no_large_cap", strategy="swing")
                return []

            # Step 2: 최근 20일 평균 거래대금 상위 50 종목 선별
            # 거래대금(trading_value) = 거래량 × 가격으로, 유동성의 직접 지표
            # 상위 50으로 제한하여 충분한 유동성을 확보하면서도 과도한 종목 수 방지
            ohlcv_stmt = (
                select(
                    DailyOHLCV.symbol,
                    func.avg(func.coalesce(DailyOHLCV.trading_value, 0)).label("avg_value"),
                )
                .where(
                    DailyOHLCV.symbol.in_(large_cap_symbols),
                    DailyOHLCV.date >= func.current_date() - self.VOLATILITY_LOOKBACK,
                )
                .group_by(DailyOHLCV.symbol)
                .order_by(func.avg(func.coalesce(DailyOHLCV.trading_value, 0)).desc())
                .limit(self.VOLUME_TOP_N)
            )
            ohlcv_result = await session.execute(ohlcv_stmt)
            top_volume_symbols = [row[0] for row in ohlcv_result.all()]

            if not top_volume_symbols:
                logger.info("scan_universe.no_top_volume", strategy="swing")
                return []

            # Step 3: 20일 변동성 필터
            # 상위 50개 종목의 최근 20일 종가를 조회하여 Python에서 변동성 계산
            # (일간 수익률의 표준편차는 DB aggregate로 계산하기 어려움)
            close_stmt = (
                select(DailyOHLCV.symbol, DailyOHLCV.date, DailyOHLCV.close)
                .where(
                    DailyOHLCV.symbol.in_(top_volume_symbols),
                    DailyOHLCV.date >= func.current_date() - self.VOLATILITY_LOOKBACK,
                )
                .order_by(DailyOHLCV.symbol, DailyOHLCV.date)
            )
            close_result = await session.execute(close_stmt)
            close_rows = close_result.all()

        # 종목별 종가 시리즈 구성
        symbol_closes: dict[str, list[float]] = {}
        for symbol, _dt, close in close_rows:
            symbol_closes.setdefault(symbol, []).append(float(close))

        # 변동성 필터링: 일간 수익률 표준편차 × 100 (%)
        # pct_change = (close[i] - close[i-1]) / close[i-1]
        # volatility = std(pct_change) × 100
        volatile_symbols: list[str] = []
        for symbol, closes in symbol_closes.items():
            if len(closes) < 5:
                # 최소 5일 데이터 필요 (표준편차 유의미성)
                continue

            series = pd.Series(closes)
            pct_changes = series.pct_change().dropna()
            if pct_changes.empty:
                continue

            volatility = Decimal(str(pct_changes.std() * 100))
            if self.MIN_VOLATILITY_PCT <= volatility <= self.MAX_VOLATILITY_PCT:
                volatile_symbols.append(symbol)

        if not volatile_symbols:
            logger.info("scan_universe.no_volatile", strategy="swing")
            return []

        # Step 4: 이미 보유 중인 스윙 트레이딩 종목 제외
        # 동일 전략 타입의 오픈 포지션을 조회하여 중복 진입 방지
        open_positions = await self.get_open_positions(strategy_type=StrategyType.SWING)
        held_symbols = {pos.symbol for pos in open_positions}
        candidates = [s for s in volatile_symbols if s not in held_symbols]

        logger.info(
            "scan_universe.result",
            strategy="swing",
            large_cap=len(large_cap_symbols),
            top_volume=len(top_volume_symbols),
            volatile=len(volatile_symbols),
            held=len(held_symbols),
            candidates=len(candidates),
        )
        return candidates

    async def generate_signals(self, pipeline_result: PipelineResult) -> list[Signal]:
        """매매 시그널 생성 — 기술 지표 조합 + LLM 분석 결합.

        # 상세 로직:
        # 1. pipeline_result.trade_decisions에서 BUY 결정만 추출
        # 2. 대응하는 stock_analysis에서 confidence ≥ 0.50 확인
        # 3. OHLCV 60일 조회 → 기술 지표 3개 조건 체크:
        #    a. RSI 과매도 반전: 전일 RSI(14) < 30 → 당일 RSI ≥ 30
        #    b. MACD 골든크로스: MACD선이 시그널선을 상향 돌파
        #    c. 볼린저밴드 하단 반등: 전일 종가 ≤ 하단 밴드, 당일 종가 > 하단 밴드
        # 4. 3개 조건 중 2개 이상 충족 필터
        # 5. ExitPriceCalculator.fixed_percentage로 고정 비율 손절(3%)/익절(5%) 산출
        # 6. PositionSizer로 포지션 크기 계산
        # 7. AlgoRiskManager로 정량적 리스크 체크
        # 8. 모든 조건 통과 시 Signal 생성
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

            # Step 2: confidence 체크 — LLM이 0.50 이상 확신해야 진입
            if analysis.confidence < self.MIN_CONFIDENCE:
                logger.debug(
                    "generate_signals.low_confidence",
                    symbol=decision.symbol,
                    confidence=str(analysis.confidence),
                )
                continue

            # Step 3: OHLCV 조회 → 기술 지표 조건 체크
            ohlcv_list = await self._broker.get_daily_ohlcv(
                decision.symbol, period_days=self.OHLCV_LOOKBACK_DAYS
            )
            if len(ohlcv_list) < 30:
                # RSI(14) + MACD(26) + BB(20) 계산에 최소 30일 데이터 필요
                logger.debug(
                    "generate_signals.insufficient_data",
                    symbol=decision.symbol,
                    bars=len(ohlcv_list),
                )
                continue

            # 기술 조건 체크 (3개 중 2개 이상 충족 필요)
            conditions_met, condition_names = self._check_technical_conditions(ohlcv_list)
            if conditions_met < self.MIN_TECHNICAL_CONDITIONS:
                logger.debug(
                    "generate_signals.insufficient_technical",
                    symbol=decision.symbol,
                    met=conditions_met,
                    required=self.MIN_TECHNICAL_CONDITIONS,
                    conditions=condition_names,
                )
                continue

            # Step 4: 진입가 결정
            entry_price = decision.price or analysis.current_price
            if entry_price is None or entry_price <= Decimal("0"):
                logger.warning(
                    "generate_signals.no_price",
                    symbol=decision.symbol,
                )
                continue

            # Step 5: 고정 비율 손절/익절 계산
            # 스윙 전략은 단기 매매이므로 ATR 동적 비율 대신 고정 비율 사용
            # 손절 3%: 단기 변동 내에서 손실 제한
            # 익절 5%: Risk:Reward ≈ 1:1.67
            stop_loss, take_profit = ExitPriceCalculator.fixed_percentage(
                entry_price=entry_price,
                stop_pct=self.STOP_LOSS_PCT,
                tp_pct=self.TAKE_PROFIT_PCT,
            )

            # Step 6: 포지션 사이징 — 리스크 기반 수량 계산
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

            # Step 7: 알고리즘 리스크 체크 — 8개 정량적 규칙 검증
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

            # Step 8: Signal 생성 — 모든 조건 통과
            signal = Signal(
                symbol=decision.symbol,
                action=SignalAction.BUY,
                confidence=analysis.confidence,
                target_price=take_profit,
                stop_loss_price=stop_loss,
                quantity=final_quantity,
                position_value_krw=final_value,
                reasoning=(
                    f"Swing 전략 진입: confidence={analysis.confidence}, "
                    f"기술조건={'/'.join(condition_names)}({conditions_met}개), "
                    f"SL={stop_loss:.0f}(-{self.STOP_LOSS_PCT}%), "
                    f"TP={take_profit:.0f}(+{self.TAKE_PROFIT_PCT}%), "
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
                conditions=condition_names,
            )

        logger.info(
            "generate_signals.complete",
            strategy="swing",
            decisions=len(pipeline_result.trade_decisions),
            signals=len(signals),
        )
        return signals

    async def check_exit_conditions(self, position: PositionRecord) -> ExitSignal | None:
        """청산 조건 체크 — 포지션별 손절/익절/트레일링/시간 기반 청산 판단.

        # 상세 로직 (우선순위 순서):
        # 1. 손절 체크 (최우선) — 현재가 ≤ 손절가이면 즉시 청산
        # 2. 트레일링 스톱 — 수익 3%↑ 후 진입 후 최고가 대비 2% 하락 시 청산
        #    - 포지션 트레이딩의 ATR 동적 비율과 달리 고정 2% 트레일 사용
        #    - 단기 매매이므로 빠른 수익 보호가 중요
        # 3. 익절 체크 — 현재가 ≥ 익절가이면 장 마감 시 청산
        # 4. 시간 기반 청산 — 10 거래일 초과 보유 시 장 마감 시 청산
        # 5. 모두 해당 없으면 None 반환 (포지션 유지)
        """
        # 현재가 조회
        price_info = await self._broker.get_price(position.symbol)
        current_price = price_info.current_price

        # 미실현 손익률 계산 (%)
        unrealized_pnl_pct = (
            (current_price - position.entry_price) / position.entry_price
        ) * Decimal("100")

        # ── 1. 손절 체크 (우선순위 최고) ──
        # 현재가가 손절가 이하이면 즉시 청산하여 추가 손실 방지
        # 스윙 전략은 고정 3% 손절 → 빠른 손절로 자본 보존
        if current_price <= position.stop_loss_price:
            return ExitSignal(
                symbol=position.symbol,
                reason=ExitReason.STOP_LOSS,
                urgency="immediate",
                current_price=current_price,
                trigger_price=position.stop_loss_price,
                unrealized_pnl_pct=unrealized_pnl_pct,
                recommended_action=DecisionAction.STOP_LOSS,
                reasoning=(
                    f"손절가 도달: 현재가 {current_price:,} ≤ "
                    f"손절가 {position.stop_loss_price:,} "
                    f"(손실률 {unrealized_pnl_pct:.1f}%)"
                ),
            )

        # ── 2. 트레일링 스톱 ──
        # 수익률이 TRAILING_ACTIVATE_PCT(3%) 이상일 때 활성화
        # 진입 후 최고가 대비 TRAILING_TRAIL_PCT(2%) 하락하면 수익 보호 청산
        # 포지션 트레이딩(ATR 기반 동적)과 달리 고정 2% 비율 사용
        if unrealized_pnl_pct >= self.TRAILING_ACTIVATE_PCT:
            # 진입 후 최고가 조회
            ohlcv_list = await self._broker.get_daily_ohlcv(
                position.symbol, period_days=self.OHLCV_LOOKBACK_DAYS
            )
            entry_bars = [bar for bar in ohlcv_list if bar.date >= position.entry_date]
            if entry_bars:
                highest_since_entry = max(bar.high for bar in entry_bars)
                # 고정 2% 트레일링 스톱 가격 계산
                trailing_stop = ExitPriceCalculator.trailing_stop_price(
                    highest_since_entry=highest_since_entry,
                    trailing_pct=self.TRAILING_TRAIL_PCT,
                )
                if current_price <= trailing_stop:
                    return ExitSignal(
                        symbol=position.symbol,
                        reason=ExitReason.TRAILING_STOP,
                        urgency="immediate",
                        current_price=current_price,
                        trigger_price=trailing_stop,
                        unrealized_pnl_pct=unrealized_pnl_pct,
                        recommended_action=DecisionAction.SELL,
                        reasoning=(
                            f"트레일링 스톱 발동: 현재가 {current_price:,} ≤ "
                            f"트레일링가 {trailing_stop:,} "
                            f"(최고가 {highest_since_entry:,}의 -2%, "
                            f"수익률 {unrealized_pnl_pct:.1f}%)"
                        ),
                    )

        # ── 3. 익절 체크 ──
        # 현재가가 익절가 이상이면 장 마감 시 청산 (급하지 않음)
        # 스윙 전략은 고정 5% 익절
        if position.take_profit_price is not None and current_price >= position.take_profit_price:
            return ExitSignal(
                symbol=position.symbol,
                reason=ExitReason.TAKE_PROFIT,
                urgency="end_of_day",
                current_price=current_price,
                trigger_price=position.take_profit_price,
                unrealized_pnl_pct=unrealized_pnl_pct,
                recommended_action=DecisionAction.TAKE_PROFIT,
                reasoning=(
                    f"익절가 도달: 현재가 {current_price:,} ≥ "
                    f"익절가 {position.take_profit_price:,} "
                    f"(수익률 {unrealized_pnl_pct:.1f}%)"
                ),
            )

        # ── 4. 시간 기반 청산 ──
        # 최대 보유 기간(10 거래일) 초과 시 장 마감에 정리
        # 단기 전략이므로 보유 기간이 길어지면 기회비용 발생
        days_held = (date.today() - position.entry_date).days
        max_days = position.max_holding_days or self.MAX_HOLDING_DAYS
        if days_held >= max_days:
            return ExitSignal(
                symbol=position.symbol,
                reason=ExitReason.TIME_BASED,
                urgency="end_of_day",
                current_price=current_price,
                trigger_price=None,
                unrealized_pnl_pct=unrealized_pnl_pct,
                recommended_action=DecisionAction.SELL,
                reasoning=(
                    f"최대 보유 기간 초과: {days_held}일 ≥ {max_days}일 "
                    f"(수익률 {unrealized_pnl_pct:.1f}%)"
                ),
            )

        # 5. 모든 청산 조건 미해당 → 포지션 유지
        return None

    # ── Private 헬퍼 ─────────────────────────────────────────────────

    @staticmethod
    def _check_technical_conditions(
        ohlcv_list: list[OHLCV],
    ) -> tuple[int, list[str]]:
        """기술 지표 3개 조건 체크 — RSI/MACD/BB.

        스윙 트레이딩의 핵심 진입 로직으로, 과매도 반전 시점을 포착한다.
        세 가지 독립적인 기술 지표가 각각 반전 신호를 보이는지 확인하며,
        2개 이상 동시 충족 시 신뢰도 높은 매수 기회로 판단한다.

        Parameters
        ----------
        ohlcv_list : 날짜 오름차순 정렬된 OHLCV 리스트 (최소 30일)

        Returns
        -------
        (충족 조건 수, 충족된 조건명 리스트)
        """
        conditions_met = 0
        condition_names: list[str] = []

        # pandas Series 구성
        close_series = pd.Series(
            [float(bar.close) for bar in ohlcv_list],
            index=[bar.date for bar in ohlcv_list],
        )

        # ── 조건 1: RSI 과매도 반전 ──
        # RSI(14)가 30 미만(과매도)에서 30 이상으로 반등하는 시점
        # 매도 압력이 소진되고 매수세가 유입되기 시작하는 전환점
        # 전일 RSI < 30 (과매도 진입) → 당일 RSI ≥ 30 (반전 확인)
        rsi = calculate_rsi(close_series, period=14)
        rsi_valid = rsi.dropna()
        if len(rsi_valid) >= 2:
            prev_rsi = rsi_valid.iloc[-2]
            curr_rsi = rsi_valid.iloc[-1]
            if prev_rsi < 30 and curr_rsi >= 30:
                conditions_met += 1
                condition_names.append("RSI과매도반전")

        # ── 조건 2: MACD 골든크로스 ──
        # MACD선이 시그널선을 아래에서 위로 상향 돌파하는 시점
        # 단기 모멘텀이 장기 모멘텀을 추월 → 상승 추세 전환 신호
        # 전일: MACD < Signal (하락 모멘텀) → 당일: MACD ≥ Signal (상승 전환)
        macd_line, macd_signal, _ = calculate_macd(close_series)
        macd_valid = macd_line.dropna()
        signal_valid = macd_signal.dropna()
        # 두 시리즈의 공통 인덱스에서 비교
        common_idx = macd_valid.index.intersection(signal_valid.index)
        if len(common_idx) >= 2:
            prev_macd = macd_valid.loc[common_idx[-2]]
            curr_macd = macd_valid.loc[common_idx[-1]]
            prev_signal = signal_valid.loc[common_idx[-2]]
            curr_signal = signal_valid.loc[common_idx[-1]]
            if prev_macd < prev_signal and curr_macd >= curr_signal:
                conditions_met += 1
                condition_names.append("MACD골든크로스")

        # ── 조건 3: 볼린저밴드 하단 반등 ──
        # 종가가 볼린저밴드 하단(SMA20 - 2σ)을 터치한 후 반등하는 시점
        # 통계적으로 가격의 95%가 밴드 안에 존재하므로,
        # 하단 이탈 후 복귀는 평균 회귀(mean reversion)의 신호
        # 전일: 종가 ≤ 하단밴드 (과매도 터치) → 당일: 종가 > 하단밴드 (반등)
        _, _, bb_lower = calculate_bollinger_bands(close_series, period=20)
        bb_valid = bb_lower.dropna()
        close_for_bb = close_series.loc[bb_valid.index]
        if len(close_for_bb) >= 2 and len(bb_valid) >= 2:
            prev_close = close_for_bb.iloc[-2]
            curr_close = close_for_bb.iloc[-1]
            prev_lower = bb_valid.iloc[-2]
            curr_lower = bb_valid.iloc[-1]
            if prev_close <= prev_lower and curr_close > curr_lower:
                conditions_met += 1
                condition_names.append("BB하단반등")

        return conditions_met, condition_names

    async def _get_sector(self, symbol: str) -> str:
        """종목의 섹터 정보를 DB에서 조회한다."""
        async with self._session_factory() as session:
            stmt = select(StockMaster.sector).where(StockMaster.symbol == symbol)
            result = await session.execute(stmt)
            sector = result.scalar_one_or_none()
            return sector or "기타"
