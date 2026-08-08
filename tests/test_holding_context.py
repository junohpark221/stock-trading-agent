"""보유 컨텍스트 렌더러 테스트 (PRJ-04 §8).

핵심 회귀 방지 포인트는 **3-state 구분**이다 — 조회 실패(`known=False`)를 "미보유"로
렌더링하면 과거의 상시 "신규 포트폴리오" 오정보를 다른 형태로 되살리게 된다.
"""

from __future__ import annotations

from decimal import Decimal

from src.agent.prompts.holding_context import (
    close_price,
    render_holding_section,
    render_portfolio_section,
)


def _holding(**overrides) -> dict:
    base = {
        "position_id": 11,
        "symbol": "005930",
        "strategy_type": "position",
        "quantity": 10,
        "avg_cost": Decimal("70000"),
        "entry_date": "2026-08-01",
        "holding_days": 7,
        "max_holding_days": 30,
        "stop_loss_price": Decimal("67900"),
        "take_profit_price": Decimal("77000"),
        "trailing_stop_pct": None,
        "highest_price": None,
        "realized_pnl": Decimal("0"),
        "is_runner": False,
        "entry_trigger": ["rsi_oversold_reversal"],
        "entry_thesis": {
            "action": "buy",
            "confidence": "0.8",
            "key_factors": ["HBM 수요", "외국인 순매수 지속"],
        },
    }
    base.update(overrides)
    return base


# ── 3-state ────────────────────────────────────────────────────────────────


def test_unknown_state_never_claims_no_holdings() -> None:
    """미조회는 '미보유'로 단정하지 않는다 (상시 '신규' 오정보 회귀 방지)."""
    text = render_holding_section({"known": False, "position": None})

    assert "미조회" in text
    assert "미보유" not in text
    assert "신규" not in text


def test_none_context_is_treated_as_unknown() -> None:
    """컨텍스트 자체가 없으면 미조회로 처리한다."""
    assert "미조회" in render_holding_section(None)


def test_known_and_not_held() -> None:
    text = render_holding_section({"known": True, "position": None})

    assert "미보유" in text
    assert "신규 진입 후보" in text


def test_held_renders_core_fields() -> None:
    text = render_holding_section({"known": True, "position": _holding()})

    assert "보유 중" in text
    assert "10주" in text
    assert "70,000원" in text          # 평균단가
    assert "보유 7일" in text
    assert "시간손절 한도 30일" in text
    assert "손절가 67,900원" in text
    assert "익절가 77,000원" in text
    assert "rsi_oversold_reversal" in text
    assert "HBM 수요" in text
    # 병합 효과 고지
    assert "추가매수" in text and "리셋" in text


# ── 평가손익 (Decimal 산술) ────────────────────────────────────────────────


def test_unrealized_gain_percent() -> None:
    text = render_holding_section(
        {"known": True, "position": _holding()}, current_price=Decimal("77000")
    )

    assert "+10.00%" in text
    assert "70,000원" in text  # 평가이익 (77000-70000)*10
    assert "평가이익" in text


def test_unrealized_loss_percent() -> None:
    text = render_holding_section(
        {"known": True, "position": _holding()}, current_price=Decimal("63000")
    )

    assert "-10.00%" in text
    assert "평가손실" in text


def test_pnl_omitted_without_price() -> None:
    text = render_holding_section({"known": True, "position": _holding()})

    assert "평가손익" not in text


def test_close_price_extracts_from_tool_payload() -> None:
    assert close_price({"current_price": {"close": 71500.0}}) == Decimal("71500.0")
    assert close_price({"current_price": {"error": "no data"}}) is None
    assert close_price({}) is None


# ── 러너 경고 ──────────────────────────────────────────────────────────────


def test_runner_warning_only_when_partially_exited() -> None:
    plain = render_holding_section({"known": True, "position": _holding()})
    runner = render_holding_section(
        {
            "known": True,
            "position": _holding(
                is_runner=True,
                realized_pnl=Decimal("120000"),
                trailing_stop_pct=Decimal("5.0"),
                highest_price=Decimal("82000"),
            ),
        }
    )

    assert "부분익절 이력" not in plain
    assert "부분익절 이력" in runner
    assert "120,000원" in runner
    assert "트레일링 보호" in runner
    assert "트레일링 전환됨" in runner


# ── 포트폴리오 요약 ────────────────────────────────────────────────────────


def test_portfolio_unknown_does_not_claim_empty() -> None:
    text = render_portfolio_section({"known": False})

    assert "미조회" in text
    assert "신규 포트폴리오" not in text


def test_portfolio_empty_is_explicit() -> None:
    text = render_portfolio_section(
        {"known": True, "count": 0, "total_cost_krw": Decimal("0"), "positions": []}
    )

    assert "보유 포지션 없음" in text


def test_portfolio_lists_positions_with_weights() -> None:
    text = render_portfolio_section(
        {
            "known": True,
            "count": 2,
            "total_cost_krw": Decimal("1000000"),
            "positions": [
                {
                    "symbol": "005930",
                    "quantity": 10,
                    "avg_cost": Decimal("70000"),
                    "cost_krw": Decimal("700000"),
                },
                {
                    "symbol": "000660",
                    "quantity": 3,
                    "avg_cost": Decimal("100000"),
                    "cost_krw": Decimal("300000"),
                },
            ],
        }
    )

    assert "보유 2종목" in text
    assert "총 매입금액 1,000,000원" in text
    assert "70.00%" in text
    assert "30.00%" in text
