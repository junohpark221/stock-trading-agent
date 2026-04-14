"""Orders API routes — 주문 조회/실행 + 승인 관리.

Endpoints:
    GET  /api/orders                           — 주문 목록 조회 (필터/페이지네이션)
    GET  /api/orders/{order_id}                — 주문 상세 (체결/승인 포함)
    POST /api/orders/execute                   — 수동 주문 실행 (full pipeline)
    GET  /api/orders/approvals/pending         — 대기 중 승인 요청 목록
    POST /api/orders/approvals/{request_id}/respond — API 승인/거부/수정 응답
"""

from __future__ import annotations

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


async def _build_account_broker(account_id: str):
    """계좌별 브로커 인스턴스 생성 후 connect. USE_MOCK_BROKER면 InMemoryBroker.

    account_id="default"는 레거시 env var 기반. 그 외는 accounts 테이블에서
    Fernet으로 복호화한 AccountCredentials로 KISClient.from_credentials() 호출.
    호출자가 disconnect() 책임을 진다.
    """
    from src.broker.credentials import AccountCredentials
    from src.config import get_settings
    from src.data.cache import get_cache
    from src.db.models.account import Account, AccountCrypto
    from src.db.session import get_session_factory

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
    session_factory = get_session_factory()
    async with session_factory() as session:
        account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")

    enc_key = settings.ACCOUNT_ENCRYPTION_KEY
    if not enc_key:
        raise HTTPException(status_code=500, detail="ACCOUNT_ENCRYPTION_KEY not configured")

    credentials = AccountCredentials(
        account_id=account.id,
        app_key=AccountCrypto.decrypt(account.kis_app_key_enc, enc_key),
        app_secret=AccountCrypto.decrypt(account.kis_app_secret_enc, enc_key),
        account_no=account.kis_account_no,
        account_prod=account.kis_account_prod,
        is_paper=account.kis_is_paper,
        hts_id=account.kis_hts_id,
    )
    broker = KISClient.from_credentials(credentials, cache)
    await broker.connect()
    return broker


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
    (OrderExecutor, broker) 튜플을 반환하여 호출자가 disconnect 가능.
    """
    from src.agent.decision_recorder import DecisionRecorder
    from src.config import get_settings
    from src.data.cache import get_cache
    from src.db.session import get_session_factory
    from src.execution.approval import ApprovalManager
    from src.execution.executor import OrderExecutor
    from src.execution.web_verify import WebSearchVerifier
    from src.llm.cost_tracker import CostTracker
    from src.llm.router import LLMRouter
    from src.main import get_telegram_bot
    from src.strategy.portfolio_state import PortfolioStateService
    from src.strategy.position_manager import PositionManager
    from src.strategy.risk_manager import AlgoRiskManager

    settings = get_settings()
    session_factory = get_session_factory()
    cache = get_cache()

    # 계좌별 브로커 — 요청 단위 생성/해제
    broker = await _build_account_broker(account_id)

    # LLM
    cost_tracker = CostTracker(session_factory=session_factory, settings=settings)
    llm_router = LLMRouter(
        session_factory=session_factory,
        settings=settings,
        cost_tracker=cost_tracker,
        cache=cache,
    )
    recorder = DecisionRecorder(session_factory)

    # Notification — 앱 전역 싱글톤 사용 (lifespan에서 start/stop 관리)
    telegram_bot = get_telegram_bot()

    # Execution components
    web_verifier = WebSearchVerifier(
        llm_router=llm_router, recorder=recorder, settings=settings,
    )
    approval_manager = ApprovalManager(
        telegram_bot=telegram_bot,
        recorder=recorder,
        session_factory=session_factory,
        cache=cache,
        settings=settings,
    )
    await approval_manager.initialize()

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
    return executor, broker


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
async def execute_order(req: ExecuteOrderRequest) -> JSONResponse:
    """수동 주문 실행 — Web 검증 → 승인 → 브로커 주문 → 포지션 관리.

    OrderExecutor.execute_entry()의 전체 파이프라인을 실행한다.
    manual=True면 웹검증/승인을 생략하고 즉시 브로커로 접수한다.
    price가 None이면 브로커 현재가로 지정가 주문을 낸다.
    """
    broker = None
    try:
        executor, broker = await _build_executor(req.account_id)

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

        # TradeDecision 구성
        action = DecisionAction.BUY if req.side == OrderSide.BUY else DecisionAction.SELL
        trade_decision = TradeDecision(
            symbol=req.symbol,
            action=action,
            confidence=Decimal("1.0"),
            order_type=req.order_type,
            quantity=req.quantity,
            price=price,
            stop_loss_price=req.stop_loss_price,
            take_profit_price=req.take_profit_price,
            reasoning="Manual order via API" if req.manual else "Manual order via API (with approval)",
        )

        session_id = uuid4()
        account_label = await _resolve_account_label(req.account_id)
        result = await executor.execute_entry(
            trade_decision=trade_decision,
            session_id=session_id,
            strategy_type=req.strategy_type.value,
            account_id=req.account_id,
            account_label=account_label,
            manual=req.manual,
        )

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
        if broker:
            await broker.disconnect()


@router.get("/quote/{account_id}/{symbol}", dependencies=[Depends(require_admin)])
async def get_quote(account_id: str, symbol: str) -> JSONResponse:
    """현재가 조회 — 수동 주문 폼/커맨드 보조용.

    계좌별 브로커로 broker.get_price(symbol)를 호출하고, 사용 후 disconnect한다.
    """
    broker = None
    try:
        broker = await _build_account_broker(account_id)
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
        if broker:
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
