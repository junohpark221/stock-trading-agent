"""resolve_symbol_names 배치 리졸버 단위 테스트 (F-19/B-11 공용 헬퍼)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.data.stock_names import resolve_symbol_names


@pytest.mark.asyncio
async def test_resolve_symbol_names_empty_shortcircuits():
    """빈 심볼 리스트는 쿼리 없이 빈 dict 반환."""
    session = MagicMock()
    session.execute = AsyncMock()

    result = await resolve_symbol_names(session, [])

    assert result == {}
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_symbol_names_maps_symbol_to_name():
    """조회 결과 (symbol, name) 행을 dict로 매핑. 미존재 종목은 누락(호출부 폴백)."""
    rows = [("005930", "삼성전자"), ("000660", "SK하이닉스")]
    execute_result = MagicMock()
    execute_result.all.return_value = rows
    session = MagicMock()
    session.execute = AsyncMock(return_value=execute_result)

    result = await resolve_symbol_names(session, ["005930", "000660", "999999"])

    assert result == {"005930": "삼성전자", "000660": "SK하이닉스"}
    assert "999999" not in result  # 미존재 → 호출부에서 코드 폴백
