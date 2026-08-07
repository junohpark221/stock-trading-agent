"""Orders API routes — 주문 조회/실행 + 승인 관리.

Endpoints:
    GET  /api/orders                           — 주문 목록 조회 (필터/페이지네이션)
    GET  /api/orders/{order_id}                — 주문 상세 (체결/승인 포함)
    POST /api/orders/execute                   — 수동 주문 실행 (full pipeline)
    GET  /api/orders/approvals/pending         — 대기 중 승인 요청 목록
    POST /api/orders/approvals/{request_id}/respond — API 승인/거부/수정 응답
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.enums import (
    ApprovalStatus,
    DecisionAction,
    OrderSide,
    OrderStatus,
)
from src.core.models import (
    ApprovalResponse,
    ExecuteOrderRequest,
    ExecutionRecord,
    ExecutionResult,
    OrderRecord,
    TradeDecision,
)
from src.db.models.execution import ApprovalRequestDB, Execution, Order
from src.db.session import get_db_session
from src.api.auth import require_admin

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/orders", tags=["orders"])


# ── Executor Factory ─────────────────────────────────────────────────


async def _build_account_credentials(account_id: str):
    """accounts 테이블에서 Fernet 복호화한 AccountCredentials를 조립(멀티 계좌)."""
    from src.broker.credentials import AccountCredentials
    from src.config import get_settings
    from src.db.models.account import Account, AccountCrypto
    from src.db.session import get_session_factory

    settings = get_settings()
    session_factory = get_session_factory()
    async with session_factory() as session:
        account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")

    enc_key = settings.ACCOUNT_ENCRYPTION_KEY
    if not enc_key:
        raise HTTPException(status_code=500, detail="ACCOUNT_ENCRYPTION_KEY not configured")

    return AccountCredentials(
        account_id=account.id,
        app_key=AccountCrypto.decrypt(account.kis_app_key_enc, enc_key),
        app_secret=AccountCrypto.decrypt(account.kis_app_secret_enc, enc_key),
        account_no=account.kis_account_no,
        account_prod=account.kis_account_prod,
        is_paper=account.kis_is_paper,
        hts_id=account.kis_hts_id,
    )


async def _build_account_broker(account_id: str):
    """계좌별 브로커 인스턴스 생성 후 connect. USE_MOCK_BROKER면 InMemoryBroker.

    account_id="default"는 레거시 env var 기반. 그 외는 accounts 테이블에서
    Fernet으로 복호화한 AccountCredentials로 KISClient.from_credentials() 호출.
    요청 단위 폴백 빌더 — 호출자가 disconnect() 책임을 진다(B-09: 공유 레지스트리
    재사용이 불가능한 경우에만 사용). 평상시 경로는 `_acquire_broker` 참고.
    """
    from src.config import get_settings
    from src.data.cache import get_cache

    settings = get_settings()
    cache = get_cache()

    if settings.USE_MOCK_BROKER:
        from src.broker.mock.client import InMemoryBroker
        broker = InMemoryBroker()
        await broker.connect()
        return broker

    from src.broker.kis.client import KISClient

    # "default" 레거시 계좌 — env var 직접 사용
    if account_id == "default":
        broker = KISClient(settings=settings, cache=cache)
        await broker.connect()
        return broker

    # 멀티 계좌 — accounts 테이블에서 복호화 후 credentials 경로
    credentials = await _build_account_credentials(account_id)
    broker = KISClient.from_credentials(credentials, cache)
    await broker.connect()
    return broker


async def _acquire_broker(account_id: str) -> tuple[object, bool]:
    """수동 주문/시세 조회용 브로커 확보. `(broker, owned)`를 반환한다. (B-09)

    앱은 web+scheduler가 한 프로세스라 startup에 연결된 공유 `BrokerRegistry`
    싱글톤을 재사용한다(요청마다 connect/disconnect로 인한 토큰 재발급·레이트리밋
    부담 제거).

    - 레지스트리에 등록된 계좌  → 재사용. `owned=False`(레지스트리 소유 →
      **disconnect 금지**, 앱 shutdown의 `disconnect_all()`이 정리).
    - 미등록 멀티 계좌          → DB 자격증명으로 lazy `register()` 후 재사용
      (`owned=False`, 이후 요청에서도 재사용).
    - 레지스트리 미초기화 / "default" 레거시 / mock → 요청 단위 생성(폴백,
      `owned=True` → 호출자가 disconnect).
    """
    from src.config import get_settings

    registry = None
    try:
        from src.main import get_broker_registry
        registry = get_broker_registry()
    except RuntimeError:
        registry = None  # 스케줄러 비활성 등 — 레지스트리 미초기화

    if registry is not None:
        try:
            return registry.get(account_id), False
        except KeyError:
            settings = get_settings()
            if account_id != "default" and not settings.USE_MOCK_BROKER:
                credentials = await _build_account_credentials(account_id)
                broker = await registry.register(credentials)
                return broker, False

    # 폴백: 요청 단위 생성 (호출자 disconnect 책임)
    broker = await _build_account_broker(account_id)
    return broker, True


async def _confirm_fill(
    *,
    broker,
    finalizer,
    order_id: int,
    broker_order_id: str,
    timeout_sec: int,
):
    """접수된 단일 주문의 체결을 동기적으로 짧게 확인한다. (B-08)

    `timeout_sec` 동안 ~1초 간격으로 `get_order_status`를 폴링하고, 종료상태가
    확인되면 reconciler와 동일한 공유 `FillFinalizer.finalize_from_order_result`
    (멱등)로 DB를 확정한 뒤 그 종료 `OrderResult`를 반환한다. 미확정이면 None.
    """
    from src.db.session import get_session_factory

    terminal = (
        OrderStatus.FILLED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
    )
    attempts = max(1, timeout_sec)

    for attempt in range(attempts):
        try:
            order_result = await broker.get_order_status(broker_order_id)
        except Exception:
            logger.warning(
                "manual_order_confirm_status_failed",
                order_id=order_id, broker_order_id=broker_order_id,
            )
            return None

        if order_result.status in terminal:
            session_factory = get_session_factory()
            async with session_factory() as session:
                order = await session.get(Order, order_id)
            if order is not None:
                await finalizer.finalize_from_order_result(
                    order=order, order_result=order_result,
                )
            return order_result

        if attempt < attempts - 1:
            await asyncio.sleep(1)

    return None


def _apply_confirmation(result, order_result):
    """동기 체결 확인(OrderResult)을 ExecutionResult에 반영해 새 결과를 반환. (B-08)"""
    status = order_result.status
    if status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
        return result.model_copy(update={
            "success": True,
            "pending": False,
            "fill_price": order_result.filled_price or result.fill_price,
            "commission": order_result.commission or result.commission,
        })
    # REJECTED / CANCELLED — 접수 후 거부/취소 확인
    return result.model_copy(update={
        "success": False,
        "pending": False,
        "error": f"주문 {status.value}",
    })


async def _resolve_account_label(account_id: str) -> str:
    """계좌 상세에서 표시할 닉네임/계좌번호 조합. 실패 시 account_id."""
    if account_id == "default":
        return "default"
    from src.db.models.account import Account
    from src.db.session import get_session_factory

    session_factory = get_session_factory()
    async with session_factory() as session:
        account = await session.get(Account, account_id)
    if account is None:
        return account_id
    acct_no = account.kis_account_no or ""
    last4 = acct_no[-4:] if len(acct_no) >= 4 else acct_no
    if account.nickname and account.nickname != account.id:
        return f"{account.nickname} ({last4})" if last4 else account.nickname
    return last4 or account.id


async def _build_executor(account_id: str = "default"):
    """OrderExecutor + 전체 의존성 트리를 조립.

    strategy.py의 _build_strategy() 패턴과 동일.
    `(executor, broker, owned, finalizer)` 튜플을 반환한다.
    - owned=True면 호출자가 broker.disconnect() 책임을 진다(요청 단위 폴백 브로커).
      owned=False면 공유 레지스트리 소유이므로 disconnect 금지(B-09).
    - finalizer는 B-08 동기 체결 확인(`_confirm_fill`)에 재사용한다.
    """
    from src.agent.decision_recorder import DecisionRecorder
    from src.config import get_settings
    from src.data.cache import get_cache
    from src.db.session import get_session_factory
    from src.execution.executor import OrderExecutor
    from src.execution.fill_finalizer import FillFinalizer
    from src.execution.web_verify import WebSearchVerifier
    from src.llm.cost_tracker import CostTracker
    from src.llm.router import LLMRouter
    from src.main import get_approval_manager, get_telegram_bot
    from src.strategy.portfolio_state import PortfolioStateService
    from src.strategy.position_manager import PositionManager
    from src.strategy.risk_manager import AlgoRiskManager

    settings = get_settings()
    session_factory = get_session_factory()
    cache = get_cache()

    # 계좌별 브로커 — 공유 레지스트리 재사용(가능 시), 아니면 요청 단위 폴백 (B-09)
    broker, owned = await _acquire_broker(account_id)

    # LLM
    cost_tracker = CostTracker(session_factory=session_factory, settings=settings)
    llm_router = LLMRouter(
        session_factory=session_factory,
        settings=settings,
        cost_tracker=cost_tracker,
        cache=cache,
    )
    recorder = DecisionRecorder(session_factory)

    # Notification/Approval — 앱 전역 싱글톤 사용. ApprovalManager를 여기서
    # 새로 만들면 TelegramBot._callback_handler가 덮어써져 스케줄러에서 보낸
    # 기존 승인 요청의 버튼 클릭이 "not_pending_or_already_timed_out"으로
    # 무시된다.
    telegram_bot = get_telegram_bot()
    approval_manager = get_approval_manager()

    # Execution components
    web_verifier = WebSearchVerifier(
        llm_router=llm_router, recorder=recorder, settings=settings,
    )

    # Strategy components
    portfolio_service = PortfolioStateService(
        broker=broker, session_factory=session_factory, cache=cache,
    )
    risk_manager = AlgoRiskManager(
        portfolio_service=portfolio_service,
        session_factory=session_factory,
        settings=settings,
    )
    position_manager = PositionManager(session_factory)

    executor = OrderExecutor(
        broker=broker,
        web_verifier=web_verifier,
        approval_manager=approval_manager,
        risk_manager=risk_manager,
        position_manager=position_manager,
        portfolio_service=portfolio_service,
        recorder=recorder,
        telegram_bot=telegram_bot,
        session_factory=session_factory,
        settings=settings,
    )

    # B-08: reconciler와 동일한 체결 확정 경로를 동기 확인에 재사용
    finalizer = FillFinalizer(
        session_factory=session_factory,
        position_manager=position_manager,
        telegram_bot=telegram_bot,
        settings=settings,
    )
    return executor, broker, owned, finalizer


# ── GET /api/orders ──────────────────────────────────────────────────


@router.get("")
async def list_orders(
    symbol: str | None = Query(None, description="종목코드 필터"),
    status: str | None = Query(None, description="주문 상태 필터"),
    from_date: date | None = Query(None, description="시작일 (YYYY-MM-DD)"),
    to_date: date | None = Query(None, description="종료일 (YYYY-MM-DD)"),
    account_id: str = Query("default", description="계좌 ID"),
    limit: int = Query(50, ge=1, le=500, description="조회 건수"),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """주문 목록 조회 — 필터링 + 페이지네이션."""
    try:
        stmt = select(Order)
        count_stmt = select(func.count()).select_from(Order)

        stmt = stmt.where(Order.account_id == account_id)
        count_stmt = count_stmt.where(Order.account_id == account_id)

        if symbol:
            stmt = stmt.where(Order.symbol == symbol)
            count_stmt = count_stmt.where(Order.symbol == symbol)
        if status:
            stmt = stmt.where(Order.status == status)
            count_stmt = count_stmt.where(Order.status == status)
        if from_date:
            stmt = stmt.where(Order.created_at >= datetime.combine(from_date, datetime.min.time(), tzinfo=UTC))
            count_stmt = count_stmt.where(Order.created_at >= datetime.combine(from_date, datetime.min.time(), tzinfo=UTC))
        if to_date:
            stmt = stmt.where(Order.created_at <= datetime.combine(to_date, datetime.max.time(), tzinfo=UTC))
            count_stmt = count_stmt.where(Order.created_at <= datetime.combine(to_date, datetime.max.time(), tzinfo=UTC))

        total = (await session.execute(count_stmt)).scalar_one()
        stmt = stmt.order_by(Order.created_at.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()

        items = [OrderRecord.model_validate(row) for row in rows]
        return JSONResponse(
            content={"items": [i.model_dump(mode="json") for i in items], "total": total}
        )

    except Exception:
        logger.exception("list_orders_failed")
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── GET /api/orders/{order_id} ───────────────────────────────────────


@router.get("/{order_id}")
async def get_order_detail(
    order_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """주문 상세 — 체결 기록 + 승인 요청 포함."""
    try:
        # 주문 조회
        order_row = (
            await session.execute(select(Order).where(Order.id == order_id))
        ).scalar_one_or_none()

        if order_row is None:
            raise HTTPException(status_code=404, detail="Order not found")

        # 체결 기록
        exec_rows = (
            await session.execute(
                select(Execution).where(Execution.order_id == order_id)
            )
        ).scalars().all()

        # 승인 요청
        approval_row = (
            await session.execute(
                select(ApprovalRequestDB).where(ApprovalRequestDB.order_id == order_id)
            )
        ).scalar_one_or_none()

        order_data = OrderRecord.model_validate(order_row).model_dump(mode="json")
        executions_data = [
            ExecutionRecord.model_validate(r).model_dump(mode="json") for r in exec_rows
        ]
        approval_data = None
        if approval_row is not None:
            approval_data = {
                "id": approval_row.id,
                "request_id": str(approval_row.request_id),
                "order_id": approval_row.order_id,
                "status": approval_row.status,
                "requested_at": approval_row.requested_at.isoformat() if approval_row.requested_at else None,
                "responded_at": approval_row.responded_at.isoformat() if approval_row.responded_at else None,
                "modified_quantity": approval_row.modified_quantity,
                "response_reason": approval_row.response_reason or "",
                "expires_at": approval_row.expires_at.isoformat() if approval_row.expires_at else None,
            }

        return JSONResponse(content={
            "order": order_data,
            "executions": executions_data,
            "approval": approval_data,
        })

    except HTTPException:
        raise
    except Exception:
        logger.exception("get_order_detail_failed", order_id=order_id)
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── POST /api/orders/execute ─────────────────────────────────────────


@router.post("/execute", dependencies=[Depends(require_admin)])
async def execute_order(
    req: ExecuteOrderRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """수동 주문 실행 — Web 검증 → 승인 → 브로커 주문 → 포지션 관리.

    매수(side=buy): OrderExecutor.execute_entry()의 전체 파이프라인.
    매도(side=sell): 기존 open 포지션을 해석해 execute_exit()로 청산한다(F-28).
      position_id를 주면 그 포지션을, 없으면 symbol+account의 가장 오래된 open
      포지션을 대상으로 한다. 대상이 없으면 422로 거부한다(진입 경로 오용 금지).
    manual=True면 웹검증/승인을 생략하고 즉시 브로커로 접수한다.
    price가 None이면 브로커 현재가로 지정가 주문을 낸다.
    """
    from src.config import get_settings
    from src.execution.manual_sell import (
        ManualSellRejection,
        build_manual_exit_signal,
        resolve_manual_sell,
    )

    broker = None
    owned = False
    try:
        executor, broker, owned, finalizer = await _build_executor(req.account_id)

        # F-28: 매도는 반드시 기존 포지션 청산 경로(execute_exit). 진입 경로로 내면
        # phantom 포지션이 생기거나(인라인 확정) 포지션이 안 닫힌다(비동기 확정).
        # 시세 조회 전에 먼저 해석해 불가한 매도를 외부 호출 없이 차단한다.
        resolution = None
        if req.side == OrderSide.SELL:
            resolution = await resolve_manual_sell(
                session=session,
                account_id=req.account_id,
                symbol=req.symbol,
                quantity=req.quantity,
                position_id=req.position_id,
            )
            if isinstance(resolution, ManualSellRejection):
                raise HTTPException(status_code=422, detail=resolution.message)

        # 가격 자동 보정 — 빈 값이면 현재가 조회
        price = req.price
        if price is None or price <= 0:
            try:
                price_info = await broker.get_price(req.symbol)
                price = price_info.current_price
            except Exception as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"현재가 조회 실패: {exc}",
                ) from exc
            if price is None or price <= 0:
                raise HTTPException(
                    status_code=422,
                    detail=f"현재가가 유효하지 않습니다: {price}",
                )

        session_id = uuid4()
        account_label = await _resolve_account_label(req.account_id)

        if resolution is not None:
            exit_signal = build_manual_exit_signal(
                position=resolution.position,
                price=price,
                reasoning=f"Manual exit via API ({req.account_id})",
            )
            result = await executor.execute_exit(
                exit_signal=exit_signal,
                position=resolution.position,
                session_id=session_id,
                account_id=req.account_id,
                account_label=account_label,
                broker=broker,
                exit_quantity=resolution.quantity,
                order_type_override=req.order_type,
                manual=req.manual,
            )
        else:
            trade_decision = TradeDecision(
                symbol=req.symbol,
                action=DecisionAction.BUY,
                confidence=Decimal("1.0"),
                order_type=req.order_type,
                quantity=req.quantity,
                price=price,
                stop_loss_price=req.stop_loss_price,
                take_profit_price=req.take_profit_price,
                reasoning=(
                    "Manual order via API" if req.manual
                    else "Manual order via API (with approval)"
                ),
            )
            result = await executor.execute_entry(
                trade_decision=trade_decision,
                session_id=session_id,
                strategy_type=req.strategy_type.value,
                account_id=req.account_id,
                account_label=account_label,
                manual=req.manual,
            )

        # B-08: 접수분(pending)은 동기로 짧게 체결을 확인해 응답에 반영
        if result.pending and result.broker_order_id:
            confirmed = await _confirm_fill(
                broker=broker, finalizer=finalizer,
                order_id=result.order_id, broker_order_id=result.broker_order_id,
                timeout_sec=get_settings().MANUAL_ORDER_CONFIRM_TIMEOUT_SEC,
            )
            if confirmed is not None:
                result = _apply_confirmation(result, confirmed)

        return JSONResponse(
            content=result.model_dump(mode="json"),
            status_code=200 if result.success else 422,
        )

    except HTTPException:
        raise
    except Exception:
        logger.exception("execute_order_failed", symbol=req.symbol)
        raise HTTPException(status_code=500, detail="Internal server error") from None
    finally:
        if owned and broker:
            await broker.disconnect()


@router.get("/quote/{account_id}/{symbol}", dependencies=[Depends(require_admin)])
async def get_quote(account_id: str, symbol: str) -> JSONResponse:
    """현재가 조회 — 수동 주문 폼/커맨드 보조용.

    공유 BrokerRegistry를 재사용하고(B-09), 폴백으로 생성한 경우에만 disconnect한다.
    """
    broker = None
    owned = False
    try:
        broker, owned = await _acquire_broker(account_id)
        info = await broker.get_price(symbol)
        return JSONResponse(content={
            "symbol": symbol,
            "account_id": account_id,
            "current_price": str(info.current_price),
            "timestamp": info.timestamp.isoformat() if getattr(info, "timestamp", None) else None,
        })
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("get_quote_failed", symbol=symbol, account_id=account_id)
        raise HTTPException(status_code=422, detail=f"현재가 조회 실패: {exc}") from exc
    finally:
        if owned and broker:
            await broker.disconnect()


# ── GET /api/orders/approvals/pending ────────────────────────────────


@router.get("/approvals/pending")
async def list_pending_approvals(
    account_id: str = Query("default", description="계좌 ID"),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """대기 중 승인 요청 목록 — Order 정보와 함께 반환."""
    try:
        stmt = (
            select(ApprovalRequestDB)
            .where(ApprovalRequestDB.status == "pending")
            .where(ApprovalRequestDB.account_id == account_id)
            .order_by(ApprovalRequestDB.requested_at.desc())
        )
        rows = (await session.execute(stmt)).scalars().all()

        items = []
        for row in rows:
            # Order 정보 보강
            order_row = (
                await session.execute(select(Order).where(Order.id == row.order_id))
            ).scalar_one_or_none()

            item = {
                "id": row.id,
                "request_id": str(row.request_id),
                "order_id": row.order_id,
                "status": row.status,
                "requested_at": row.requested_at.isoformat() if row.requested_at else None,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                "symbol": order_row.symbol if order_row else None,
                "side": order_row.side if order_row else None,
                "quantity": order_row.quantity if order_row else None,
                "price": str(order_row.price) if order_row and order_row.price else None,
            }
            items.append(item)

        return JSONResponse(content={"items": items, "total": len(items)})

    except Exception:
        logger.exception("list_pending_approvals_failed")
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── POST /api/orders/approvals/{request_id}/respond ──────────────────


@router.post("/approvals/{request_id}/respond")
async def respond_to_approval(
    request_id: UUID,
    body: ApprovalResponse,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """API 기반 승인/거부/수정 응답 (텔레그램 대안).

    asyncio.Event 메커니즘을 우회하고 DB를 직접 업데이트한다.
    """
    try:
        # 승인 요청 조회
        approval_row = (
            await session.execute(
                select(ApprovalRequestDB).where(
                    ApprovalRequestDB.request_id == request_id
                )
            )
        ).scalar_one_or_none()

        if approval_row is None:
            raise HTTPException(status_code=404, detail="Approval request not found")

        if approval_row.status != "pending":
            raise HTTPException(
                status_code=400,
                detail=f"Approval already responded: {approval_row.status}",
            )

        # modify 시 수량 필수
        if body.action == "modify" and body.modified_quantity is None:
            raise HTTPException(
                status_code=400,
                detail="modified_quantity is required for modify action",
            )

        # 상태 매핑
        if body.action == "approve":
            new_status = ApprovalStatus.APPROVED
        elif body.action == "reject":
            new_status = ApprovalStatus.REJECTED
        else:  # modify
            new_status = ApprovalStatus.APPROVED

        now = datetime.now(UTC)

        # ApprovalRequestDB 업데이트
        approval_update: dict = {
            "status": new_status.value,
            "responded_at": now,
            "response_reason": f"api_{body.action}",
        }
        if body.modified_quantity is not None:
            approval_update["modified_quantity"] = body.modified_quantity

        await session.execute(
            update(ApprovalRequestDB)
            .where(ApprovalRequestDB.request_id == request_id)
            .values(**approval_update)
        )

        # Order 업데이트
        order_update: dict = {"approval_status": new_status.value}
        if body.modified_quantity is not None:
            order_update["modified_quantity"] = body.modified_quantity

        await session.execute(
            update(Order)
            .where(Order.id == approval_row.order_id)
            .values(**order_update)
        )

        await session.commit()

        logger.info(
            "approval_responded_via_api",
            request_id=str(request_id),
            action=body.action,
            status=new_status.value,
        )

        return JSONResponse(content={
            "success": True,
            "request_id": str(request_id),
            "status": new_status.value,
            "order_id": approval_row.order_id,
        })

    except HTTPException:
        raise
    except Exception:
        logger.exception("respond_to_approval_failed", request_id=str(request_id))
        raise HTTPException(status_code=500, detail="Internal server error") from None
