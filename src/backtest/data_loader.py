"""Historical data loader for backtesting.

DB에서 일봉 데이터를 일괄 로드하여 메모리 DataFrame으로 캐싱.
시뮬레이션 중 DB 호출 없이 O(1) 가격 조회.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.models.market_data import DailyOHLCV


class HistoricalDataLoader:
    """백테스트용 일봉 데이터 로더.

    DB에서 일괄 로드 → 메모리 DataFrame 캐싱.
    시뮬레이션 중 DB 호출 없이 O(1) 가격 조회.
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
