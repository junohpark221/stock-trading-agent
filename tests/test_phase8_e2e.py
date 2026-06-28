"""Phase 8 E2E 테스트 — 다중 계좌 8개 시나리오.

Prerequisites:
    - Docker: docker compose up -d (PostgreSQL 16 + Redis 7)
    - Migrations: uv run alembic upgrade head

Run:
    uv run pytest tests/test_phase8_e2e.py -v -s --timeout=120

Skip in CI:
    uv run pytest -m "not e2e"
"""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio(loop_scope="module")]

# ── Test constants ────────────────────────────────────────────────────

_FERNET_KEY = Fernet.generate_key().decode()

_ACCOUNT_A = {
    "id": "acct-value",
    "nickname": "가치투자",
    "kis_app_key": "fake_key_a",
    "kis_app_secret": "fake_secret_a",
    "kis_account_no": "50071111-01",
    "kis_is_paper": True,
    "strategy_type": "position",
    "investment_prompt": "가치 투자 원칙을 따릅니다. PER < 15, PBR < 1.5인 저평가 우량주를 선호합니다.",
}

_ACCOUNT_B = {
    "id": "acct-momentum",
    "nickname": "모멘텀",
    "kis_app_key": "fake_key_b",
    "kis_app_secret": "fake_secret_b",
    "kis_account_no": "50072222-01",
    "kis_is_paper": True,
    "strategy_type": "swing",
    "investment_prompt": "모멘텀 투자를 합니다. 52주 신고가 돌파, 거래량 급증 종목 위주로 투자합니다.",
}


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def env_override():
    """E2E 환경변수 설정 — 모듈 시작 전에 적용."""
    overrides = {
        "USE_MOCK_BROKER": "true",
        "SCHEDULER_ENABLED": "true",
        "HUMAN_APPROVAL_REQUIRED": "false",
        "WEB_VERIFY_ENABLED": "false",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "test_chat",
        "ACCOUNT_ENCRYPTION_KEY": _FERNET_KEY,
        # 레거시 default 합성 방지 — DB 계좌만 사용
        "KIS_APP_KEY": "",
        "KIS_APP_SECRET": "",
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
    """httpx AsyncClient — lifespan 수동 관리 + TelegramBot mock."""
    from src.main import app, lifespan

    async with lifespan(app):
        from src.main import get_telegram_bot

        bot = get_telegram_bot()
        bot._disabled = False
        bot.send_message = AsyncMock(return_value=12345)
        bot.send_message_with_buttons = AsyncMock(return_value=12346)
        bot.update_message = AsyncMock()

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            timeout=90.0,
        ) as c:
            yield c


@pytest.fixture(scope="module")
def session_factory(client):
    """DB session factory."""
    from src.db.session import get_session_factory

    return get_session_factory()


@pytest.fixture(scope="module")
def mock_send(client):
    """TelegramBot.send_message AsyncMock."""
    from src.main import get_telegram_bot

    return get_telegram_bot().send_message


@pytest.fixture(scope="module")
async def seed_accounts(client):
    """2개 계좌 생성 (API 경유) — 테스트 전 1회."""
    accounts = {}
    for acct_data in [_ACCOUNT_A, _ACCOUNT_B]:
        resp = await client.post("/api/accounts", json=acct_data)
        # 이미 존재할 수 있음 (이전 테스트 실행 잔존)
        if resp.status_code == 409:
            resp = await client.get(f"/api/accounts/{acct_data['id']}")
        assert resp.status_code in (200, 201), (
            f"Failed to seed account {acct_data['id']}: {resp.status_code} {resp.text}"
        )
        accounts[acct_data["id"]] = resp.json()
    return accounts


# ── Scenario 1: 2계좌 동시 분석 ─────────────────────────────────────


async def test_two_account_concurrent_analysis(client, seed_accounts, session_factory):
    """계좌 A(가치투자/position) + 계좌 B(모멘텀/swing) 전략 팩토리 분기 검증."""
    from src.core.enums import StrategyType
    from src.strategy.registry import StrategyCommonDeps, StrategyFactory
    from tests.conftest import make_settings

    # 전략 팩토리로 2개 전략 인스턴스 생성
    deps = StrategyCommonDeps(
        orchestrator=AsyncMock(),
        recorder=AsyncMock(),
        broker=AsyncMock(),
        session_factory=session_factory,
        settings=make_settings(),
        cache=AsyncMock(),
    )

    strategy_a = StrategyFactory.create(
        StrategyType.POSITION,
        deps,
        account_id="acct-value",
        investment_prompt=_ACCOUNT_A["investment_prompt"],
    )
    strategy_b = StrategyFactory.create(
        StrategyType.SWING,
        deps,
        account_id="acct-momentum",
        investment_prompt=_ACCOUNT_B["investment_prompt"],
    )

    # 검증: 서로 다른 전략 타입 + 계좌별 속성
    assert type(strategy_a).__name__ == "PositionTradingStrategy"
    assert type(strategy_b).__name__ == "SwingTradingStrategy"
    assert strategy_a._account_id == "acct-value"
    assert strategy_b._account_id == "acct-momentum"
    assert strategy_a._investment_prompt == _ACCOUNT_A["investment_prompt"]
    assert strategy_b._investment_prompt == _ACCOUNT_B["investment_prompt"]


# ── Scenario 2: 분석 데이터 공유 (공통 1회 + 계좌별 N회) ────────────


async def test_market_data_shared_once(client, seed_accounts, session_factory):
    """SchedulerFactory: 공통 작업 1회 + 계좌별 작업 N회 등록 규칙 검증."""
    from src.core.enums import StrategyType
    from src.scheduler.engine import SchedulerEngine
    from src.scheduler.factory import AccountContext, SchedulerFactory

    from tests.conftest import make_settings

    settings = make_settings(USE_MOCK_BROKER=True)

    # 테스트용 SchedulerEngine (실제 APScheduler 없이 job 목록만 관리)
    engine = SchedulerEngine(session_factory=session_factory, settings=settings)

    # 공통 작업 등록
    SchedulerFactory._register_common_jobs(
        engine,
        provider=None,  # mock broker → market_data_collect 스킵
        watchlist_symbols=["005930", "035420"],
        generator=AsyncMock(),
        telegram_bot=AsyncMock(),
        settings=settings,
        session_factory=session_factory,
    )

    # 계좌별 작업 등록 — 2개 계좌
    ctx_value = AccountContext(
        account_id="acct-value",
        nickname="가치투자",
        account_no="50071111-01",
        broker=AsyncMock(),
        auth=None,
        strategy_type=StrategyType.POSITION,
        portfolio_service=AsyncMock(),
        position_manager=AsyncMock(),
        exit_checker=MagicMock(),
        exit_service=AsyncMock(),
        order_executor=AsyncMock(),
        monitor=AsyncMock(),
        investment_prompt=_ACCOUNT_A["investment_prompt"],
        account_label="가치투자 (1-01)",
    )
    ctx_momentum = AccountContext(
        account_id="acct-momentum",
        nickname="모멘텀",
        account_no="50072222-01",
        broker=AsyncMock(),
        auth=None,
        strategy_type=StrategyType.SWING,
        portfolio_service=AsyncMock(),
        position_manager=AsyncMock(),
        exit_checker=MagicMock(),
        exit_service=AsyncMock(),
        order_executor=AsyncMock(),
        monitor=AsyncMock(),
        investment_prompt=_ACCOUNT_B["investment_prompt"],
        account_label="모멘텀 (2-01)",
    )

    for ctx in [ctx_value, ctx_momentum]:
        SchedulerFactory._register_account_jobs(
            engine,
            ctx,
            orchestrator=AsyncMock(),
            watchlist_symbols=["005930", "035420"],
            generator=AsyncMock(),
            telegram_bot=AsyncMock(),
            settings=settings,
            session_factory=session_factory,
            decision_queue=MagicMock(),
        )

    job_names = set(engine._job_fns.keys())

    # 공통 작업: pre_open_prep, weekly/monthly/llm_cost_report (market_data_collect은 provider=None이라 스킵)
    common_jobs = {"pre_open_prep", "weekly_report", "monthly_report", "llm_cost_report"}
    for cj in common_jobs:
        assert cj in job_names, f"Common job '{cj}' missing"

    # 계좌별 작업 (결정/실행 분리)
    assert "swing_decision:acct-momentum" in job_names, "Swing decision for acct-momentum missing"
    assert "position_decision:acct-value" in job_names, "Position decision for acct-value missing"
    assert "execution_drain:acct-value" in job_names
    assert "execution_drain:acct-momentum" in job_names
    assert "stop_loss_check:acct-value" in job_names
    assert "stop_loss_check:acct-momentum" in job_names
    assert "daily_report:acct-value" in job_names
    assert "daily_report:acct-momentum" in job_names

    # swing_decision은 acct-value에 없어야 함 (position 전략)
    assert "swing_decision:acct-value" not in job_names
    assert "position_decision:acct-momentum" not in job_names

    # 공통 작업은 정확히 4개 (market_data_collect 제외, pre_open_prep 포함)
    common_count = sum(1 for n in job_names if ":" not in n)
    assert common_count == 4, f"Expected 4 common jobs, got {common_count}"

    # 계좌별 작업: auth=None이라 token_refresh 없음. 계좌당 decision+execution_drain+
    # stop_loss+daily = 4개 × 2계좌 = 8개.
    per_account_count = sum(1 for n in job_names if ":" in n)
    assert per_account_count == 8, f"Expected 8 per-account jobs, got {per_account_count}"


# ── Scenario 3: 투자 철학 차등화 ─────────────────────────────────────


async def test_investment_philosophy_differentiation(client, seed_accounts):
    """동일 데이터 + 다른 투자 철학 프롬프트 → LLM에 다른 system message 전달."""
    from src.agent.agents.market_analyst import MarketAnalyst
    from src.core.enums import MessageRole
    from src.core.models import LLMMessage

    base_system = "당신은 주식 분석가입니다."

    # 구체 에이전트 인스턴스로 _inject_investment_prompt 검증
    agent = MarketAnalyst(
        router=MagicMock(),
        recorder=MagicMock(),
        tool_registry=MagicMock(),
    )

    # 가치투자 프롬프트 주입
    messages_a = [
        LLMMessage(role=MessageRole.SYSTEM, content=base_system),
        LLMMessage(role=MessageRole.USER, content="삼성전자를 분석해주세요."),
    ]
    result_a = agent._inject_investment_prompt(
        messages_a,
        investment_prompt=_ACCOUNT_A["investment_prompt"],
    )

    # 모멘텀 프롬프트 주입
    messages_b = [
        LLMMessage(role=MessageRole.SYSTEM, content=base_system),
        LLMMessage(role=MessageRole.USER, content="삼성전자를 분석해주세요."),
    ]
    result_b = agent._inject_investment_prompt(
        messages_b,
        investment_prompt=_ACCOUNT_B["investment_prompt"],
    )

    # system 메시지 내용이 달라야 함
    sys_a = result_a[0].content
    sys_b = result_b[0].content

    assert "가치 투자" in sys_a
    assert "PER" in sys_a
    assert "모멘텀" in sys_b
    assert "52주 신고가" in sys_b
    assert sys_a != sys_b


# ── Scenario 4: 계좌 격리 ───────────────────────────────────────────


async def test_account_isolation(session_factory):
    """계좌 A 포지션이 계좌 B에서 미노출."""
    from src.strategy.position_manager import PositionManager

    pm = PositionManager(session_factory)

    # 계좌 A에 포지션 생성
    pos = await pm.create(
        symbol="005930",
        strategy_type="position",
        quantity=10,
        entry_price=Decimal("72000"),
        stop_loss_price=Decimal("68000"),
        account_id="acct-value",
    )

    try:
        # 계좌 A 조회 → 존재
        open_a = await pm.get_open(account_id="acct-value")
        assert any(p.id == pos.id for p in open_a), "Position should be visible in acct-value"

        # 계좌 B 조회 → 미노출
        open_b = await pm.get_open(account_id="acct-momentum")
        assert all(p.id != pos.id for p in open_b), "Position should NOT be visible in acct-momentum"

        # account_id 없이 조회 → 전체에서 보임
        open_all = await pm.get_open()
        assert any(p.id == pos.id for p in open_all), "Position should be visible in unfiltered query"
    finally:
        # cleanup
        from src.core.enums import ExitReason

        await pm.close(pos.id, exit_price=Decimal("72000"), exit_reason=ExitReason.MANUAL)


# ── Scenario 5: 텔레그램 계좌 표시 ──────────────────────────────────


async def test_telegram_account_labeling(client, seed_accounts):
    """승인 요청/체결/경고 메시지에 [닉네임 (뒤4자리)] 표시."""
    from src.core.enums import ApprovalStatus, MonitoringAlertType, OrderSide
    from src.notification.templates import MessageTemplates

    label_a = "가치투자 (1-01)"
    label_b = "모멘텀 (2-01)"

    # 승인 요청 메시지
    msg_approval = MessageTemplates.approval_request(
        account_label=label_a,
        symbol="005930",
        name="삼성전자",
        side=OrderSide.BUY,
        quantity=10,
        price=Decimal("72000"),
        position_value_krw=Decimal("720000"),
        portfolio_pct=Decimal("0.72"),
        stop_loss_price=Decimal("68000"),
        take_profit_price=Decimal("80000"),
        risk_reward_ratio=Decimal("2.0"),
        analysis_summary="가치 분석 결과",
        web_verify_summary="검증 완료",
        session_id=None,
    )
    assert f"[{label_a}]" in msg_approval

    # 체결 통보 메시지
    msg_exec = MessageTemplates.execution_notification(
        account_label=label_b,
        symbol="035420",
        name="NAVER",
        side=OrderSide.BUY,
        quantity=5,
        fill_price=Decimal("210000"),
        commission=Decimal("150"),
        approval_status=ApprovalStatus.AUTO_APPROVED,
    )
    assert f"[{label_b}]" in msg_exec

    # 경고 알림 메시지
    msg_alert = MessageTemplates.monitoring_alert(
        account_label=label_a,
        alert_type=MonitoringAlertType.STOP_LOSS_PROXIMITY,
        symbol="005930",
        message="손절가 근접",
        current_value=Decimal("69000"),
        threshold_value=Decimal("68000"),
    )
    assert f"[{label_a}]" in msg_alert

    # account_label 미지정 시 → [계좌] 헤더 없음
    msg_no_label = MessageTemplates.monitoring_alert(
        alert_type=MonitoringAlertType.STOP_LOSS_PROXIMITY,
        symbol="005930",
        message="손절가 근접",
        current_value=Decimal("69000"),
        threshold_value=Decimal("68000"),
    )
    assert "[" not in msg_no_label.split("\n")[0] or "경고" in msg_no_label.split("\n")[0]


# ── Scenario 6: backward compatibility ───────────────────────────────


async def test_backward_compatibility_default(session_factory):
    """DB 계좌 없고 KIS_APP_KEY 설정 시 → "default" 합성 계좌 생성."""
    from src.scheduler.factory import SchedulerFactory

    from tests.conftest import make_settings

    settings = make_settings(
        KIS_APP_KEY="test_key",
        KIS_APP_SECRET="test_secret",
        KIS_ACCOUNT_NO="9999888801",
        USE_MOCK_BROKER=True,
        SCHEDULER_ENABLED=True,
    )

    # _synthesize_default_account 검증
    default_acct = SchedulerFactory._synthesize_default_account(settings)
    assert default_acct.id == "default"
    assert default_acct.nickname == "default"
    assert default_acct.kis_account_no == "9999888801"
    assert default_acct.is_active is True

    # _make_account_label — default 계좌는 닉네임==id이므로 뒤4자리만
    label = SchedulerFactory._make_account_label(default_acct)
    assert label == "8801"

    # 기존 API account_id 없이 호출 가능 여부 (정상 응답)
    # 이 테스트는 별도 앱 시작 없이 함수 레벨로 검증
    from src.strategy.position_manager import PositionManager

    pm = PositionManager(session_factory)
    # account_id 없이 get_open → 전체 조회 (에러 없음)
    result = await pm.get_open()
    assert isinstance(result, list)


# ── Scenario 7: 계좌 비활성화 ───────────────────────────────────────


async def test_account_deactivation(client, session_factory):
    """is_active=False 계좌 → 스케줄러/분석에서 제외."""
    # 임시 계좌 생성
    temp_acct = {
        "id": "acct-temp-deactivate",
        "nickname": "비활성테스트",
        "kis_app_key": "fake_key_temp",
        "kis_app_secret": "fake_secret_temp",
        "kis_account_no": "99990000-01",
        "kis_is_paper": True,
        "strategy_type": "swing",
        "investment_prompt": "테스트용",
    }
    resp = await client.post("/api/accounts", json=temp_acct)
    if resp.status_code == 409:
        # 이미 존재 — 재활성화 (이전 테스트에서 비활성화됐을 수 있음)
        resp = await client.put(
            f"/api/accounts/{temp_acct['id']}",
            json={"kis_is_paper": True},  # dummy update to get 200
        )
        # is_active를 직접 DB에서 True로 되돌림
        from sqlalchemy import update as sa_update

        from src.db.models.account import Account

        async with session_factory() as session:
            await session.execute(
                sa_update(Account)
                .where(Account.id == temp_acct["id"])
                .values(is_active=True)
            )
            await session.commit()
    assert resp.status_code in (200, 201)

    # 활성 계좌 목록에 포함 확인
    resp = await client.get("/api/accounts", params={"is_active": True})
    assert resp.status_code == 200
    active_ids = {a["id"] for a in resp.json()["items"]}
    assert temp_acct["id"] in active_ids

    # soft delete
    resp = await client.delete(f"/api/accounts/{temp_acct['id']}")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    # 활성 계좌 목록에서 제외 확인
    resp = await client.get("/api/accounts", params={"is_active": True})
    assert resp.status_code == 200
    active_ids_after = {a["id"] for a in resp.json()["items"]}
    assert temp_acct["id"] not in active_ids_after

    # _load_active_accounts에서도 제외
    from src.scheduler.factory import SchedulerFactory

    active = await SchedulerFactory._load_active_accounts(session_factory)
    active_loaded_ids = {a.id for a in active}
    assert temp_acct["id"] not in active_loaded_ids


# ── Scenario 8: 투자 철학 실시간 변경 ───────────────────────────────


async def test_investment_prompt_realtime_change(client, seed_accounts, session_factory):
    """API로 프롬프트 변경 → DB 즉시 반영 → 다음 사이클에 적용."""
    account_id = "acct-value"
    new_prompt = "변경된 투자 철학: ESG 중심 투자. 환경/사회/지배구조 우수 기업만 선별합니다."

    # 1. 프롬프트 변경
    resp = await client.put(
        f"/api/accounts/{account_id}/prompt",
        json={"investment_prompt": new_prompt},
    )
    assert resp.status_code == 200
    assert resp.json()["investment_prompt"] == new_prompt

    # 2. API 조회로 반영 확인
    resp = await client.get(f"/api/accounts/{account_id}")
    assert resp.status_code == 200
    assert resp.json()["investment_prompt"] == new_prompt

    # 3. DB 직접 조회로 즉시 반영 검증
    from sqlalchemy import select

    from src.db.models.account import Account

    async with session_factory() as session:
        acct = (
            await session.execute(select(Account).where(Account.id == account_id))
        ).scalar_one()
        assert acct.investment_prompt == new_prompt

    # 4. 스케줄러가 다음 사이클에 새 프롬프트를 읽는지 검증
    from src.scheduler.factory import SchedulerFactory

    accounts = await SchedulerFactory._load_active_accounts(session_factory)
    target = next((a for a in accounts if a.id == account_id), None)
    assert target is not None
    assert target.investment_prompt == new_prompt

    # 5. 원래 프롬프트로 복원
    resp = await client.put(
        f"/api/accounts/{account_id}/prompt",
        json={"investment_prompt": _ACCOUNT_A["investment_prompt"]},
    )
    assert resp.status_code == 200
