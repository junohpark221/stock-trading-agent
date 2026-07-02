"""종목코드→종목명 배치 리졸버.

여러 계층(스케줄러 결정/뉴스 잡, 백오피스 라우트)이 공유하는 단일 정본.
``StockMaster.name``(NOT NULL)을 원천으로 하며, PK(symbol) IN 조회 한 번으로
N+1을 회피한다. ``is_active`` 필터 없이 조회하므로 상장폐지(soft-delete) 종목의
이름도 반환한다. 미존재 종목은 결과 dict에 키가 없으므로, 호출부에서
``names.get(symbol, symbol)``로 코드 폴백한다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from src.db.models.market_data import StockMaster

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_symbol_names(
    session: AsyncSession, symbols: list[str]
) -> dict[str, str]:
    """``symbols``에 대한 {종목코드: 종목명} 매핑을 한 번의 쿼리로 반환.

    미존재 종목은 결과에서 누락되며, 호출부에서 코드로 폴백한다.
    """
    if not symbols:
        return {}
    result = await session.execute(
        select(StockMaster.symbol, StockMaster.name).where(
            StockMaster.symbol.in_(symbols)
        )
    )
    return {symbol: name for symbol, name in result.all()}
