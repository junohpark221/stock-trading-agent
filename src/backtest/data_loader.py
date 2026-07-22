"""Historical data loader for backtesting.

DB에서 일봉 데이터를 일괄 로드하여 메모리 DataFrame으로 캐싱.
시뮬레이션 중 DB 호출 없이 O(1) 가격 조회.

수급(PRJ-03) 캐시는 확정 14(전 유니버스 5년 일괄 메모리 적재 금지)에 따라
행수 상한 가드 + 심볼 청크 재로드 워크플로우로 적재한다.
"""

from __future__ import annotations

import bisect
from datetime import date
from decimal import Decimal

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.models import InvestorFlowRecord, MarketInvestorFlowRecord
from src.db.models.investor_flow import InvestorFlowDaily, MarketInvestorFlowDaily
from src.db.models.market_data import DailyOHLCV

# 종목 수급 적재 행수 상한 기본값 — 행당 ~10KB 추정(krx 백필 행은 90필드,
# Decimal 45개) → 50k행 ≈ 최악 500MB (EC2 4GB 공유 환경 기준).
# 5년(~1,230거래일) × 40종목 또는 1년 × 200종목 상당.
_MAX_FLOW_ROWS = 50_000
# 심볼 IN 배치 크기 — 단일 결과셋/asyncpg 버퍼 피크 억제(총량은 COUNT 가드 책임).
_FLOW_SYMBOL_BATCH = 200


class HistoricalDataLoader:
    """백테스트용 일봉·수급 데이터 로더.

    DB에서 일괄 로드 → 메모리 캐싱(OHLCV는 DataFrame, 수급은 도메인 레코드).
    시뮬레이션 중 DB 호출 없이 O(1)/O(log n) 조회.

    캐시 3종은 상호 독립: OHLCV(``load``) / 종목 수급(``load_investor_flow``) /
    시장 수급(``load_market_investor_flow``). 각 load는 재호출 시 해당 캐시만
    전체 교체(replace)한다 — 종목 수급을 심볼 청크로 load→계산→재로드하는 동안
    OHLCV·시장 수급 캐시는 유지된다. 누적(append) 시맨틱은 행수 상한 가드를
    우회하므로 제공하지 않는다.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory
        self._data: dict[str, pd.DataFrame] = {}
        self._trading_dates: list[date] = []
        self._loaded = False
        self._total_records = 0
        # 수급 캐시 — 심볼/시장별 날짜 오름차순 (지표 모듈 rows[-window:] 계약)
        self._flow: dict[str, list[InvestorFlowRecord]] = {}
        self._market_flow: dict[str, list[MarketInvestorFlowRecord]] = {}
        self._flow_total = 0

    async def load(
        self,
        *,
        symbols: list[str],
        start_date: date,
        end_date: date,
    ) -> int:
        """DB에서 일봉 데이터 일괄 로드.

        Returns:
            로드된 총 레코드 수.
        """
        stmt = (
            select(DailyOHLCV)
            .where(DailyOHLCV.symbol.in_(symbols))
            .where(DailyOHLCV.date.between(start_date, end_date))
            .order_by(DailyOHLCV.date)
        )

        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        # symbol별 DataFrame 구성
        self._data.clear()
        symbol_rows: dict[str, list[DailyOHLCV]] = {}
        for row in rows:
            symbol_rows.setdefault(row.symbol, []).append(row)

        all_dates: set[date] = set()
        for symbol, sym_rows in symbol_rows.items():
            df = pd.DataFrame(
                {
                    "open": [row.open for row in sym_rows],
                    "high": [row.high for row in sym_rows],
                    "low": [row.low for row in sym_rows],
                    "close": [row.close for row in sym_rows],
                    "volume": [int(row.volume) for row in sym_rows],
                    "trading_value": [row.trading_value for row in sym_rows],
                },
                index=pd.Index([row.date for row in sym_rows], name="date"),
            )
            self._data[symbol] = df
            all_dates.update(row.date for row in sym_rows)

        self._trading_dates = sorted(all_dates)
        self._total_records = len(rows)
        self._loaded = True
        return self._total_records

    def get_ohlcv(self, symbol: str, target_date: date) -> dict | None:
        """특정 날짜의 OHLCV 반환.

        Returns:
            {"open": Decimal, "high": Decimal, "low": Decimal,
             "close": Decimal, "volume": int} 또는 None.
        """
        df = self._data.get(symbol)
        if df is None or target_date not in df.index:
            return None
        row = df.loc[target_date]
        return {
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": int(row["volume"]),
        }

    def get_ohlcv_range(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        """날짜 범위의 OHLCV DataFrame 반환.

        기술 지표 계산에 사용. 데이터 없으면 빈 DataFrame.
        """
        df = self._data.get(symbol)
        if df is None:
            return pd.DataFrame()
        mask = (df.index >= start_date) & (df.index <= end_date)
        return df.loc[mask]

    def get_trading_values(
        self, symbol: str, start_date: date, end_date: date
    ) -> dict[date, Decimal | None]:
        """날짜 범위의 거래대금 매핑 반환 — flow_intensity/compute_flow_summary 분모.

        {날짜: 거래대금(원) | None}. 심볼 미적재 시 빈 dict.
        """
        df = self._data.get(symbol)
        if df is None:
            return {}
        mask = (df.index >= start_date) & (df.index <= end_date)
        sub = df.loc[mask, "trading_value"]
        return {d: (None if pd.isna(v) else v) for d, v in sub.items()}

    async def load_investor_flow(
        self,
        *,
        symbols: list[str],
        start_date: date,
        end_date: date,
        max_rows: int = _MAX_FLOW_ROWS,
    ) -> int:
        """종목 수급 데이터 적재 (재호출 시 종목 수급 캐시 전체 교체).

        확정 14: 적재 전 COUNT로 예상 행수를 검사해 max_rows 초과 시
        ValueError로 즉시 거부한다(기존 캐시 보존 — clear 전에 raise).
        호출자는 심볼 청크로 나눠 load→계산→재로드한다. SELECT는 심볼
        _FLOW_SYMBOL_BATCH개씩 분할.

        Returns:
            적재된 총 레코드 수.
        """
        count_stmt = (
            select(func.count())
            .select_from(InvestorFlowDaily)
            .where(InvestorFlowDaily.symbol.in_(symbols))
            .where(InvestorFlowDaily.date.between(start_date, end_date))
        )

        async with self._session_factory() as session:
            expected = (await session.execute(count_stmt)).scalar_one()
            if expected > max_rows:
                raise ValueError(
                    f"수급 데이터 예상 {expected}행 > 상한 {max_rows}행 — "
                    "심볼 청크를 줄이거나 max_rows를 오버라이드하세요"
                )

            self._flow.clear()
            total = 0
            for i in range(0, len(symbols), _FLOW_SYMBOL_BATCH):
                batch = symbols[i : i + _FLOW_SYMBOL_BATCH]
                stmt = (
                    select(InvestorFlowDaily)
                    .where(InvestorFlowDaily.symbol.in_(batch))
                    .where(InvestorFlowDaily.date.between(start_date, end_date))
                    .order_by(InvestorFlowDaily.symbol, InvestorFlowDaily.date)
                )
                result = await session.execute(stmt)
                for row in result.scalars().all():
                    record = InvestorFlowRecord.model_validate(row)
                    self._flow.setdefault(record.symbol, []).append(record)
                    total += 1

        for records in self._flow.values():
            records.sort(key=lambda r: r.date)  # 오름차순 보험 — 지표 계약 전제

        self._flow_total = total
        return total

    async def load_market_investor_flow(
        self,
        *,
        start_date: date,
        end_date: date,
        markets: list[str] | None = None,
    ) -> int:
        """시장 단위 수급 적재 (재호출 시 시장 수급 캐시 전체 교체).

        기본 대상 kospi+kosdaq. 5년 전체가 시장당 ~1,230행이라
        COUNT 가드·배치 분할 없이 단일 SELECT.

        Returns:
            적재된 총 레코드 수.
        """
        targets = markets if markets is not None else ["kospi", "kosdaq"]
        stmt = (
            select(MarketInvestorFlowDaily)
            .where(MarketInvestorFlowDaily.market.in_(targets))
            .where(MarketInvestorFlowDaily.date.between(start_date, end_date))
            .order_by(MarketInvestorFlowDaily.market, MarketInvestorFlowDaily.date)
        )

        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        self._market_flow.clear()
        for row in rows:
            record = MarketInvestorFlowRecord.model_validate(row)
            self._market_flow.setdefault(record.market, []).append(record)

        for records in self._market_flow.values():
            records.sort(key=lambda r: r.date)

        return len(rows)

    def get_investor_flow_range(
        self, symbol: str, start_date: date, end_date: date
    ) -> list[InvestorFlowRecord]:
        """날짜 범위의 종목 수급 레코드 반환 (오름차순 보장).

        지표 모듈(src/analysis/investor_flow.py)에 그대로 투입 가능.
        미적재 심볼은 빈 리스트.
        """
        rows = self._flow.get(symbol)
        if not rows:
            return []
        lo = bisect.bisect_left(rows, start_date, key=lambda r: r.date)
        hi = bisect.bisect_right(rows, end_date, key=lambda r: r.date)
        return rows[lo:hi]

    def get_market_investor_flow_range(
        self, market: str, start_date: date, end_date: date
    ) -> list[MarketInvestorFlowRecord]:
        """날짜 범위의 시장 수급 레코드 반환 (오름차순 보장). 미적재 시 []."""
        rows = self._market_flow.get(market)
        if not rows:
            return []
        lo = bisect.bisect_left(rows, start_date, key=lambda r: r.date)
        hi = bisect.bisect_right(rows, end_date, key=lambda r: r.date)
        return rows[lo:hi]

    def get_close_price(self, symbol: str, target_date: date) -> Decimal | None:
        """특정 날짜의 종가 반환."""
        ohlcv = self.get_ohlcv(symbol, target_date)
        if ohlcv is None:
            return None
        return ohlcv["close"]

    def get_open_price(self, symbol: str, target_date: date) -> Decimal | None:
        """특정 날짜의 시가 반환. 체결 가격 산정에 사용."""
        ohlcv = self.get_ohlcv(symbol, target_date)
        if ohlcv is None:
            return None
        return ohlcv["open"]

    def get_trading_dates(self) -> list[date]:
        """전체 거래일 목록 (정렬됨). 시뮬레이션 루프에서 사용."""
        return list(self._trading_dates)

    def get_symbols(self) -> list[str]:
        """로드된 종목 목록."""
        return list(self._data.keys())

    @property
    def loaded(self) -> bool:
        """데이터 로드 완료 여부."""
        return self._loaded

    @property
    def total_records(self) -> int:
        """로드된 총 레코드 수."""
        return self._total_records
