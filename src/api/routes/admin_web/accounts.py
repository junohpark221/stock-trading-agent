"""Backoffice account management routes: list/create/edit/toggle/detail/manual-order/sync."""

import json
from decimal import Decimal
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import require_admin
from src.api.portfolio_live import fetch_portfolio_view
from src.api.routes.accounts import _mask_account_no, _slugify
from src.api.templates import templates
from src.core.enums import StrategyType
from src.core.time import to_kst, today_kst
from src.db.models.account import Account, AccountCrypto
from src.db.models.execution import Order
from src.db.models.strategy import PositionRecord
from src.db.session import get_db_session, get_session_factory
from src.report.data_fetcher import ReportDataFetcher

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/accounts", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def accounts_list(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/accounts — 계좌 목록 (활성 우선 정렬)."""
    result = await session.execute(
        select(Account).order_by(Account.is_active.desc(), Account.created_at),
    )
    accounts_orm = list(result.scalars().all())

    accounts = []
    for acct in accounts_orm:
        acct.account_no_masked = _mask_account_no(acct.kis_account_no)
        accounts.append(acct)

    return templates.TemplateResponse("accounts.html", {
        "request": request,
        "accounts": accounts,
        "strategy_types": [e.value for e in StrategyType],
        "error": None,
    })


@router.post("/accounts/create", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def accounts_create(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """POST /admin/accounts/create — 새 계좌 등록."""
    from src.config import get_settings

    form = await request.form()
    settings = get_settings()

    # 에러 시 폼 다시 렌더링하기 위한 헬퍼
    async def _render_error(error: str):
        result = await session.execute(
            select(Account).order_by(Account.is_active.desc(), Account.created_at),
        )
        accts = list(result.scalars().all())
        for a in accts:
            a.account_no_masked = _mask_account_no(a.kis_account_no)
        return templates.TemplateResponse("accounts.html", {
            "request": request,
            "accounts": accts,
            "strategy_types": [e.value for e in StrategyType],
            "error": error,
        })

    if not settings.ACCOUNT_ENCRYPTION_KEY:
        return await _render_error("ACCOUNT_ENCRYPTION_KEY가 설정되지 않았습니다.")

    nickname = str(form.get("nickname", "")).strip()
    if not nickname:
        return await _render_error("닉네임은 필수입니다.")

    account_id = str(form.get("id", "")).strip() or _slugify(nickname)

    # 중복 확인
    existing = (
        await session.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if existing is not None:
        return await _render_error(f"계좌 ID '{account_id}'가 이미 존재합니다.")

    kis_app_key = str(form.get("kis_app_key", "")).strip()
    kis_app_secret = str(form.get("kis_app_secret", "")).strip()
    kis_account_no = str(form.get("kis_account_no", "")).strip()

    if not kis_app_key or not kis_app_secret or not kis_account_no:
        return await _render_error("KIS App Key, App Secret, 계좌번호는 필수입니다.")

    # risk_overrides JSON 파싱
    risk_overrides = None
    risk_raw = str(form.get("risk_overrides", "")).strip()
    if risk_raw:
        try:
            risk_overrides = json.loads(risk_raw)
        except (json.JSONDecodeError, ValueError):
            return await _render_error("리스크 오버라이드가 올바른 JSON 형식이 아닙니다.")

    enc_key = settings.ACCOUNT_ENCRYPTION_KEY
    account = Account(
        id=account_id,
        nickname=nickname,
        kis_app_key_enc=AccountCrypto.encrypt(kis_app_key, enc_key),
        kis_app_secret_enc=AccountCrypto.encrypt(kis_app_secret, enc_key),
        kis_account_no=kis_account_no,
        kis_account_prod=str(form.get("kis_account_prod", "01")).strip() or "01",
        kis_is_paper="kis_is_paper" in form,
        kis_hts_id=str(form.get("kis_hts_id", "")).strip(),
        strategy_type=str(form.get("strategy_type", "position")),
        investment_prompt=str(form.get("investment_prompt", "")).strip(),
        risk_tolerance=str(form.get("risk_tolerance", "moderate")).strip(),
        risk_overrides=risk_overrides,
        is_active=True,
    )
    session.add(account)
    await session.commit()

    logger.info("account_created_via_web", account_id=account_id)
    return RedirectResponse(url="/admin/accounts", status_code=303)


@router.get("/accounts/{account_id}/edit-form", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def accounts_edit_form(
    request: Request,
    account_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/accounts/{account_id}/edit-form — 인라인 편집 폼 partial (HTMX)."""
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    return templates.TemplateResponse("partials/account_edit_form.html", {
        "request": request,
        "account": account,
        "strategy_types": [e.value for e in StrategyType],
        "error": None,
    })


@router.post("/accounts/{account_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def accounts_edit(
    request: Request,
    account_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """POST /admin/accounts/{account_id}/edit — 계좌 수정 처리.

    성공 시 갱신된 계좌정보 partial을 반환해 #account-info 인라인 교체.
    """
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    form = await request.form()

    # risk_overrides JSON 파싱
    risk_overrides = account.risk_overrides
    risk_raw = str(form.get("risk_overrides", "")).strip()
    if risk_raw:
        try:
            risk_overrides = json.loads(risk_raw)
        except (json.JSONDecodeError, ValueError):
            return templates.TemplateResponse("partials/account_edit_form.html", {
                "request": request,
                "account": account,
                "strategy_types": [e.value for e in StrategyType],
                "error": "리스크 오버라이드가 올바른 JSON 형식이 아닙니다.",
            })
    elif not risk_raw:
        risk_overrides = None

    account.nickname = str(form.get("nickname", account.nickname)).strip()
    account.strategy_type = str(form.get("strategy_type", account.strategy_type))
    account.risk_tolerance = str(form.get("risk_tolerance", getattr(account, "risk_tolerance", "moderate"))).strip()
    account.kis_is_paper = "kis_is_paper" in form
    account.kis_account_prod = str(form.get("kis_account_prod", account.kis_account_prod)).strip()
    account.kis_hts_id = str(form.get("kis_hts_id", account.kis_hts_id or "")).strip()
    account.investment_prompt = str(form.get("investment_prompt", account.investment_prompt or "")).strip()
    account.risk_overrides = risk_overrides

    # KIS 인증정보 업데이트 (비어있으면 기존값 유지)
    from src.config import get_settings
    settings = get_settings()

    kis_app_key = str(form.get("kis_app_key", "")).strip()
    kis_app_secret = str(form.get("kis_app_secret", "")).strip()
    kis_account_no = str(form.get("kis_account_no", "")).strip()

    enc_key = settings.ACCOUNT_ENCRYPTION_KEY
    if kis_app_key:
        account.kis_app_key_enc = AccountCrypto.encrypt(kis_app_key, enc_key)
    if kis_app_secret:
        account.kis_app_secret_enc = AccountCrypto.encrypt(kis_app_secret, enc_key)
    if kis_account_no:
        account.kis_account_no = kis_account_no

    await session.commit()

    logger.info("account_updated_via_web", account_id=account_id)

    # B-10: 실행 중 스케줄러에 변경을 즉시 반영(재시작 불필요).
    # risk_overrides·전략·프롬프트 등 컨텍스트 파생값을 재빌드+잡 재등록한다.
    # KIS 인증정보 변경은 registry 브로커를 재생성하지 않으므로 재시작 필요.
    creds_changed = bool(kis_app_key or kis_app_secret or kis_account_no)
    reload_message: str | None = None
    try:
        from src.main import get_scheduler_runtime

        reloaded = await get_scheduler_runtime().reload_account(account_id)
        if not reloaded:
            reload_message = "변경은 저장됐으나 실행 중 스케줄러 반영에 실패했습니다. 다음 재시작 시 반영됩니다."
        elif creds_changed:
            reload_message = "리스크·전략 설정은 즉시 반영됐습니다. KIS 인증정보 변경은 재시작 후 적용됩니다."
    except RuntimeError:
        # 스케줄러 미가동(SCHEDULER_ENABLED=False / lifespan 미시작)
        reload_message = "변경 저장됨. 스케줄러 미가동 — 다음 시작 시 반영됩니다."

    return templates.TemplateResponse("partials/account_info.html", {
        "request": request,
        "account": account,
        "reload_message": reload_message,
    })


@router.post("/accounts/{account_id}/toggle", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def accounts_toggle(
    request: Request,
    account_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """POST /admin/accounts/{account_id}/toggle — 활성/비활성 토글."""
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    account.is_active = not account.is_active
    await session.commit()

    logger.info(
        "account_toggled_via_web",
        account_id=account_id,
        is_active=account.is_active,
    )

    # HTMX: 단일 행 교체
    account.account_no_masked = _mask_account_no(account.kis_account_no)
    return templates.TemplateResponse("partials/account_row.html", {
        "request": request,
        "acct": account,
    })


@router.get("/accounts/{account_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def account_detail(
    request: Request,
    account_id: str,
    order_success: str | None = Query(None),
    order_error: str | None = Query(None),
    sync_msg: str | None = Query(None),
    sync_error: str | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/accounts/{account_id} — 계좌 상세: 정보 + 잔고 + 포지션 + 수동 주문."""
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    view = await fetch_portfolio_view(account_id)
    fetcher = ReportDataFetcher(get_session_factory())
    positions = await fetcher.get_open_positions(account_id=account_id)
    pending_orders = await fetcher.get_pending_orders(account_id=account_id)

    return templates.TemplateResponse("account_detail.html", {
        "request": request,
        "account": account,
        "snapshot": view,
        "positions": positions,
        "pending_orders": pending_orders,
        "manual_order_success": order_success,
        "manual_order_error": order_error,
        "sync_msg": sync_msg,
        "sync_error": sync_error,
    })


@router.post("/accounts/{account_id}/orders", dependencies=[Depends(require_admin)])
async def account_manual_order(
    request: Request,
    account_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """POST /admin/accounts/{account_id}/orders — 백오피스 수동 주문 실행.

    폼 필드: symbol, quantity, price(선택), side("buy"|"sell").
    executor에 manual=True로 전달하여 웹검증/승인을 생략한다.
    결과 메시지를 쿼리스트링으로 붙여 계좌 상세로 redirect한다.
    """
    from urllib.parse import quote

    from src.api.routes.orders import (
        _build_executor,
        _confirm_fill,
        _resolve_account_label,
    )
    from src.config import get_settings
    from src.core.enums import DecisionAction, ExitReason, OrderStatus, OrderType
    from src.core.models import ExitSignal, TradeDecision

    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    form = await request.form()
    redirect_base = f"/admin/accounts/{account_id}"

    def _redirect_error(msg: str) -> RedirectResponse:
        return RedirectResponse(
            f"{redirect_base}?order_error={quote(msg)}", status_code=303,
        )

    symbol = str(form.get("symbol", "")).strip()
    side_raw = str(form.get("side", "")).strip().lower()
    qty_raw = str(form.get("quantity", "")).strip()
    price_raw = str(form.get("price", "")).strip()
    position_id_raw = str(form.get("position_id", "")).strip()
    # B-08: 주문유형 — 시장가(market) / 지정가(limit, 기본). 매수 경로에만 적용.
    order_type_raw = str(form.get("order_type", "")).strip().lower()
    order_type = OrderType.MARKET if order_type_raw == "market" else OrderType.LIMIT

    if side_raw not in ("buy", "sell"):
        return _redirect_error("side는 buy 또는 sell이어야 합니다")
    if side_raw == "buy" and not symbol:
        return _redirect_error("종목코드는 필수입니다")
    try:
        quantity = int(qty_raw)
    except ValueError:
        return _redirect_error(f"수량이 올바르지 않습니다: {qty_raw}")
    if quantity <= 0:
        return _redirect_error("수량은 1 이상이어야 합니다")

    price: Decimal | None = None
    if price_raw:
        try:
            price = Decimal(price_raw)
        except Exception:
            return _redirect_error(f"가격이 올바르지 않습니다: {price_raw}")
        if price <= 0:
            return _redirect_error("가격은 0보다 커야 합니다")

    # B-01: 매도는 진입 경로(execute_entry) 오용을 막고, 선택한 기존 포지션을
    # 정식 청산 경로(execute_exit)로 청산한다. position_id 필수.
    position: PositionRecord | None = None
    if side_raw == "sell":
        if not position_id_raw:
            return _redirect_error("매도는 대상 포지션을 선택해야 합니다")
        try:
            position_id = int(position_id_raw)
        except ValueError:
            return _redirect_error(f"포지션 ID가 올바르지 않습니다: {position_id_raw}")
        position = await session.get(PositionRecord, position_id)
        if (
            position is None
            or position.status != "open"
            or position.account_id != account_id
        ):
            return _redirect_error("유효한 open 포지션이 아닙니다")
        symbol = position.symbol  # 포지션 기준으로 종목 확정
        if quantity > position.quantity:
            return _redirect_error(
                f"매도 수량({quantity:,})이 보유 수량({position.quantity:,})을 초과합니다"
            )

    broker = None
    owned = False
    confirmed = None
    try:
        executor, broker, owned, finalizer = await _build_executor(account_id)

        if price is None:
            try:
                price_info = await broker.get_price(symbol)
                price = price_info.current_price
            except Exception as exc:
                logger.warning(
                    "manual_order_quote_failed",
                    account_id=account_id, symbol=symbol, error=str(exc),
                )
                return _redirect_error(f"현재가 조회 실패: {exc}")
            if price is None or price <= 0:
                return _redirect_error("현재가가 유효하지 않습니다")

        account_label = await _resolve_account_label(account_id)

        if side_raw == "sell":
            assert position is not None
            avg_cost = position.avg_cost or price
            pnl_pct = (
                (price - avg_cost) / avg_cost * Decimal("100")
                if avg_cost > 0
                else Decimal("0")
            )
            exit_signal = ExitSignal(
                symbol=symbol,
                reason=ExitReason.MANUAL,
                urgency="immediate",
                current_price=price,
                unrealized_pnl_pct=pnl_pct,
                recommended_action=DecisionAction.SELL,
                reasoning=f"Backoffice manual exit by admin ({account_id})",
            )
            result = await executor.execute_exit(
                exit_signal=exit_signal,
                position=position,
                session_id=uuid4(),
                account_id=account_id,
                account_label=account_label,
                broker=broker,
                exit_quantity=quantity,
                manual=True,
            )
        else:
            trade_decision = TradeDecision(
                symbol=symbol,
                action=DecisionAction.BUY,
                confidence=Decimal("1.0"),
                order_type=order_type,
                quantity=quantity,
                price=price,
                reasoning=f"Backoffice manual order by admin ({account_id})",
            )
            result = await executor.execute_entry(
                trade_decision=trade_decision,
                session_id=uuid4(),
                strategy_type="manual",
                account_id=account_id,
                account_label=account_label,
                manual=True,
            )

        # B-08: 접수분(pending)은 동기로 짧게 체결을 확인
        if result.pending and result.broker_order_id:
            confirmed = await _confirm_fill(
                broker=broker, finalizer=finalizer,
                order_id=result.order_id, broker_order_id=result.broker_order_id,
                timeout_sec=get_settings().MANUAL_ORDER_CONFIRM_TIMEOUT_SEC,
            )

    except HTTPException as exc:
        return _redirect_error(str(exc.detail))
    except Exception as exc:
        logger.exception(
            "manual_order_failed", account_id=account_id, symbol=symbol,
        )
        return _redirect_error(f"주문 실행 오류: {exc}")
    finally:
        if owned and broker:
            try:
                await broker.disconnect()
            except Exception:
                logger.exception("manual_order_broker_disconnect_failed")

    # B-08: 접수분(pending=True, success=True)은 동기 체결 확인 결과로 분기.
    # pending이 success를 동반하므로 success보다 먼저 검사한다.
    if result.pending:
        if confirmed is not None:
            if confirmed.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                fill_px = confirmed.filled_price or price
                fill_qty = confirmed.filled_quantity or quantity
                msg = f"{side_raw.upper()} {symbol} {fill_qty:,}주 @ {fill_px:,}원 체결"
                return RedirectResponse(
                    f"{redirect_base}?order_success={quote(msg)}", status_code=303,
                )
            # 접수 후 거부/취소 확인
            return _redirect_error(
                f"주문 {confirmed.status.value} (주문번호 {result.broker_order_id or '-'})"
            )
        # 타임아웃 — 체결 미확정
        msg = (
            f"{side_raw.upper()} {symbol} {quantity:,}주 접수 완료 — 체결 대기 "
            f"(주문번호 {result.broker_order_id or '-'})"
        )
        return RedirectResponse(
            f"{redirect_base}?order_success={quote(msg)}", status_code=303,
        )
    if result.success:
        # 즉시 체결(mock/일부 실브로커 — pending 아님)
        msg = (
            f"{side_raw.upper()} {symbol} {quantity:,}주 @ "
            f"{result.fill_price or price:,}원 체결"
        )
        return RedirectResponse(
            f"{redirect_base}?order_success={quote(msg)}", status_code=303,
        )
    return _redirect_error(result.error or "주문 실패")


@router.post("/accounts/{account_id}/sync-positions", dependencies=[Depends(require_admin)])
async def account_sync_positions(account_id: str):
    """POST /admin/accounts/{account_id}/sync-positions — 브로커-DB 포지션 즉시 동기화."""
    from src.execution.reconciler import PositionReconciler
    from src.main import get_broker_registry
    from src.strategy.position_manager import PositionManager

    try:
        # B-03: position_manager 주입 → 풀 3-way(Case 2 신규 생성·Case 3 수량보정)
        # 보장. 미주입 시 Case 1(DB-only 청산)만 동작해 스케줄러와 동작이 달라진다.
        reconciler = PositionReconciler(
            broker_registry=get_broker_registry(),
            session_factory=get_session_factory(),
            position_manager=PositionManager(get_session_factory()),
        )
        result = await reconciler.reconcile_for_account(account_id)
        msg = (
            f"포지션 동기화 완료 — "
            f"정리 {result.closed_count}건, "
            f"신규 {result.created_count}건, "
            f"수량보정 {result.qty_updated_count}건"
        )
        logger.info("admin.sync_positions", account_id=account_id, **vars(result))
        return RedirectResponse(
            f"/admin/accounts/{account_id}?sync_msg={msg}", status_code=303
        )
    except Exception as exc:
        logger.exception("admin.sync_positions.failed", account_id=account_id)
        return RedirectResponse(
            f"/admin/accounts/{account_id}?sync_error={exc}", status_code=303
        )


@router.post("/accounts/{account_id}/sync-orders", dependencies=[Depends(require_admin)])
async def account_sync_orders(account_id: str):
    """POST /admin/accounts/{account_id}/sync-orders — 미체결 주문 즉시 정리.

    - 이전 날 접수 또는 broker_order_id 없는 pending/submitted → cancelled
    - 오늘 접수 + broker_order_id 있음 → KIS 실 상태로 갱신
    """
    from src.config import get_settings
    from src.execution.fill_finalizer import FillFinalizer
    from src.execution.reconciler import OrderReconciler
    from src.main import get_broker_registry, get_telegram_bot
    from src.strategy.position_manager import PositionManager

    try:
        today = today_kst()
        cancelled_count = 0

        # 1) 취소측 (B-05):
        #    - broker_order_id 없음 또는 전일 접수 → cancelled (DB만 갱신).
        #      전일 국내주문은 KIS가 자동 만료하고, broker_order_id가 없으면 취소
        #      TR 호출이 불가하므로 DB 정리만 한다.
        #    - 당일 + broker_order_id 보유 → 실제 브로커 취소(cancel_order) 시도 후
        #      성공시에만 cancelled. 실패/예외 시에는 건드리지 않고 아래 §2
        #      reconciler가 실제 상태(체결 등)를 확정하게 둔다(살아있는 주문 오취소 방지).
        try:
            broker = get_broker_registry().get(account_id)
        except Exception:
            broker = None
            logger.warning("admin.sync_orders.no_broker", account_id=account_id)

        async with get_session_factory()() as session:
            rows = await session.execute(
                select(Order).where(
                    Order.account_id == account_id,
                    Order.status.in_(["pending", "submitted"]),
                )
            )
            orders: list[Order] = list(rows.scalars().all())
            for order in orders:
                order_date = (
                    to_kst(order.created_at).date() if order.created_at else None
                )
                is_prior = bool(order_date and order_date < today)
                if not order.broker_order_id or is_prior:
                    order.status = "cancelled"
                    cancelled_count += 1
                    continue
                # 당일 + broker_order_id 보유 → 실 브로커 취소 시도
                if broker is None:
                    continue
                try:
                    ok = await broker.cancel_order(order.broker_order_id)
                except Exception:
                    logger.exception(
                        "admin.sync_orders.cancel_failed",
                        account_id=account_id,
                        order_id=order.id,
                        broker_order_id=order.broker_order_id,
                    )
                    continue
                if ok:
                    order.status = "cancelled"
                    cancelled_count += 1
            await session.commit()

        # 2) 체결측 (B-02): 인라인 상태 문자열 갱신 제거. 스케줄러와 동일한 정식
        #    경로(OrderReconciler→FillFinalizer)로 오늘 접수 SUBMITTED 주문의 체결을
        #    확정한다 — 체결기록·포지션·position_id를 정상 생성하고, SUBMITTED 가드
        #    영구 스킵으로 인한 정합성 손상을 제거한다. (run은 전 계좌 대상이나
        #    멱등하며 스케줄러 job_reconcile_open_orders와 동일하다.)
        finalizer = FillFinalizer(
            session_factory=get_session_factory(),
            position_manager=PositionManager(get_session_factory()),
            telegram_bot=get_telegram_bot(),
            settings=get_settings(),
        )
        reconciler = OrderReconciler(
            broker_registry=get_broker_registry(),
            fill_finalizer=finalizer,
        )
        processed = await reconciler.run(target_date=today)

        msg = (
            f"미체결 정리 완료 — 취소처리 {cancelled_count}건, "
            f"체결확정 점검 {processed}건"
        )
        logger.info(
            "admin.sync_orders",
            account_id=account_id,
            cancelled=cancelled_count,
            processed=processed,
        )
        return RedirectResponse(
            f"/admin/accounts/{account_id}?sync_msg={msg}", status_code=303
        )
    except Exception as exc:
        logger.exception("admin.sync_orders.failed", account_id=account_id)
        return RedirectResponse(
            f"/admin/accounts/{account_id}?sync_error={exc}", status_code=303
        )
