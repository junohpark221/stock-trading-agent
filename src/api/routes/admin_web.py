"""Backoffice web UI routes."""

import json
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import login_handler, logout_handler, require_admin
from src.api.routes.accounts import _mask_account_no, _slugify
from src.api.routes.backtest import _execute_backtest
from src.api.templates import templates
from src.core.enums import BacktestMode, BacktestStatus, StrategyType
from src.core.models import BacktestConfig
from src.db.models.account import Account, AccountCrypto
from src.db.models.backtest import BacktestRun, BacktestTrade
from src.db.models.market_data import StockMaster
from src.db.models.scheduler import JobExecution
from src.db.models.strategy import PositionRecord
from src.db.session import get_db_session, get_session_factory
from src.report.data_fetcher import ReportDataFetcher
from src.report.metrics import PerformanceCalculator

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin-web"])


# ── Login / Logout ───────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """GET /admin/login — 로그인 폼 렌더링."""
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login")
async def login(request: Request):
    """POST /admin/login — 비밀번호 검증 후 쿠키 설정."""
    return await login_handler(request)


@router.get("/logout")
async def logout(request: Request):
    """GET /admin/logout — 쿠키 삭제 후 리다이렉트."""
    return await logout_handler(request)


# ── Dashboard ────────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/ — 대시보드: 시스템 상태 + 계좌 현황 + 오늘 매매."""
    # 1) DB 상태
    db_status = "disconnected"
    try:
        await session.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        logger.warning("dashboard_db_check_failed", exc_info=True)

    # 2) Redis 상태 (lazy import to avoid circular dependency with main)
    from src.main import get_redis

    redis_status = "disconnected"
    try:
        await get_redis().ping()
        redis_status = "connected"
    except Exception:
        logger.warning("dashboard_redis_check_failed", exc_info=True)

    # 3) 스케줄러 상태 (lazy import to avoid circular dependency with main)
    from src.main import get_scheduler

    try:
        scheduler_status = get_scheduler().get_status()
    except RuntimeError:
        scheduler_status = {"is_running": False, "is_paused": False, "jobs": [], "disabled": True}

    # 4) 계좌 목록 + 최신 스냅샷
    result = await session.execute(
        select(Account).where(Account.is_active.is_(True)).order_by(Account.created_at)
    )
    accounts_orm = list(result.scalars().all())

    fetcher = ReportDataFetcher(get_session_factory())
    account_cards = []
    for acct in accounts_orm:
        snapshot = await fetcher.get_latest_snapshot(account_id=acct.id)
        account_cards.append({
            "id": acct.id,
            "nickname": acct.nickname,
            "strategy_type": acct.strategy_type,
            "is_paper": acct.kis_is_paper,
            "total_value": snapshot.total_value if snapshot else None,
            "unrealized_pnl": snapshot.unrealized_pnl if snapshot else None,
            "realized_pnl_daily": snapshot.realized_pnl_daily if snapshot else None,
            "positions_count": snapshot.positions_count if snapshot else 0,
            "snapshot_date": snapshot.snapshot_date if snapshot else None,
        })

    # 5) 오늘 주문
    todays_orders = await fetcher.get_todays_orders(account_id=None)
    recent_orders = todays_orders[-20:][::-1]  # 최근 20건, 최신순

    order_summary = {
        "total": len(todays_orders),
        "buy": sum(1 for o in todays_orders if o.side == "buy"),
        "sell": sum(1 for o in todays_orders if o.side == "sell"),
        "filled": sum(1 for o in todays_orders if o.status == "filled"),
    }

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "db_status": db_status,
        "redis_status": redis_status,
        "scheduler": scheduler_status,
        "accounts": account_cards,
        "recent_orders": recent_orders,
        "order_summary": order_summary,
    })


# ── Account Management ───────────────────────────────────────────────────


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
        risk_overrides=risk_overrides,
        is_active=True,
    )
    session.add(account)
    await session.commit()

    logger.info("account_created_via_web", account_id=account_id)
    return RedirectResponse(url="/admin/accounts", status_code=303)


@router.get("/accounts/{account_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def accounts_edit_form(
    request: Request,
    account_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/accounts/{account_id}/edit — 계좌 수정 폼."""
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    return templates.TemplateResponse("account_edit.html", {
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
    """POST /admin/accounts/{account_id}/edit — 계좌 수정 처리."""
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
            return templates.TemplateResponse("account_edit.html", {
                "request": request,
                "account": account,
                "strategy_types": [e.value for e in StrategyType],
                "error": "리스크 오버라이드가 올바른 JSON 형식이 아닙니다.",
            })
    elif not risk_raw:
        risk_overrides = None

    account.nickname = str(form.get("nickname", account.nickname)).strip()
    account.strategy_type = str(form.get("strategy_type", account.strategy_type))
    account.kis_is_paper = "kis_is_paper" in form
    account.kis_account_prod = str(form.get("kis_account_prod", account.kis_account_prod)).strip()
    account.kis_hts_id = str(form.get("kis_hts_id", account.kis_hts_id or "")).strip()
    account.investment_prompt = str(form.get("investment_prompt", account.investment_prompt or "")).strip()
    account.risk_overrides = risk_overrides

    await session.commit()

    logger.info("account_updated_via_web", account_id=account_id)
    return RedirectResponse(url="/admin/accounts", status_code=303)


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


# ── Account Detail ───────────────────────────────────────────────────────


@router.get("/accounts/{account_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def account_detail(
    request: Request,
    account_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/accounts/{account_id} — 계좌 상세: 정보 + 잔고 + 포지션."""
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    fetcher = ReportDataFetcher(get_session_factory())
    snapshot = await fetcher.get_latest_snapshot(account_id=account_id)
    positions = await fetcher.get_open_positions(account_id=account_id)

    return templates.TemplateResponse("account_detail.html", {
        "request": request,
        "account": account,
        "snapshot": snapshot,
        "positions": positions,
    })


# ── Trades History ───────────────────────────────────────────────────────


@router.get("/trades", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def trades_history(
    request: Request,
    account_id: str | None = Query(None),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    symbol: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/trades — 매매 이력: 청산 포지션 필터 + 페이지네이션."""
    # 기본 쿼리 (trades.py 패턴 재사용)
    stmt = (
        select(PositionRecord)
        .where(PositionRecord.status == "closed")
        .order_by(PositionRecord.exit_date.desc())
    )
    count_stmt = select(func.count(PositionRecord.id)).where(
        PositionRecord.status == "closed",
    )

    # 필터 적용
    if account_id:
        stmt = stmt.where(PositionRecord.account_id == account_id)
        count_stmt = count_stmt.where(PositionRecord.account_id == account_id)
    if symbol:
        stmt = stmt.where(PositionRecord.symbol == symbol)
        count_stmt = count_stmt.where(PositionRecord.symbol == symbol)
    if from_date:
        stmt = stmt.where(PositionRecord.exit_date >= from_date)
        count_stmt = count_stmt.where(PositionRecord.exit_date >= from_date)
    if to_date:
        stmt = stmt.where(PositionRecord.exit_date <= to_date)
        count_stmt = count_stmt.where(PositionRecord.exit_date <= to_date)

    # 카운트 + 페이지네이션
    total = (await session.execute(count_stmt)).scalar_one()
    total_pages = max(1, math.ceil(total / per_page))
    page = min(page, total_pages)

    offset = (page - 1) * per_page
    stmt = stmt.offset(offset).limit(per_page)
    trades = list((await session.execute(stmt)).scalars().all())

    # 계좌 목록 (필터 드롭다운용)
    acct_result = await session.execute(
        select(Account).where(Account.is_active.is_(True)).order_by(Account.created_at)
    )
    accounts = list(acct_result.scalars().all())

    context = {
        "request": request,
        "trades": trades,
        "accounts": accounts,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "account_id": account_id,
        "from_date": from_date,
        "to_date": to_date,
        "symbol": symbol,
    }

    # HTMX 요청이면 partial만 반환
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/trade_rows.html", context)
    return templates.TemplateResponse("trades.html", context)


# ── Performance Analysis ────────────────────────────────────────────────


@router.get("/performance", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def performance_analysis(
    request: Request,
    account_id: str | None = Query(None),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/performance — 성과 분석: 핵심 지표 + 전략별 + 월별."""
    # 날짜 기본값
    if to_date is None:
        to_date = date.today()
    if from_date is None:
        from_date = to_date - timedelta(days=30)

    # 계좌 목록 (필터 드롭다운용)
    acct_result = await session.execute(
        select(Account).where(Account.is_active.is_(True)).order_by(Account.created_at)
    )
    accounts = list(acct_result.scalars().all())

    # 데이터 조회
    fetcher = ReportDataFetcher(get_session_factory())
    closed_positions = await fetcher.get_closed_positions(
        start_date=from_date, end_date=to_date, account_id=account_id,
    )
    snapshots = await fetcher.get_portfolio_snapshots(
        start_date=from_date, end_date=to_date, account_id=account_id,
    )

    # 성과 계산
    metrics = PerformanceCalculator.calculate(
        closed_positions=closed_positions,
        snapshots=snapshots,
        period_start=from_date,
        period_end=to_date,
    )
    strategy_breakdown = PerformanceCalculator.breakdown_by_strategy(closed_positions)
    monthly_breakdown = PerformanceCalculator.breakdown_by_month(closed_positions)

    context = {
        "request": request,
        "metrics": metrics,
        "strategy_breakdown": strategy_breakdown,
        "monthly_breakdown": monthly_breakdown,
        "accounts": accounts,
        "account_id": account_id,
        "from_date": from_date,
        "to_date": to_date,
    }

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/performance_content.html", context)
    return templates.TemplateResponse("performance.html", context)


# ── Backtest ────────────────────────────────────────────────────────────


@router.get("/backtest", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def backtest_page(
    request: Request,
    strategy_type: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/backtest — 백테스트 실행 목록 + 새 실행 폼."""
    stmt = select(BacktestRun).order_by(BacktestRun.started_at.desc())
    count_stmt = select(func.count(BacktestRun.id))

    if strategy_type:
        stmt = stmt.where(BacktestRun.strategy_type == strategy_type)
        count_stmt = count_stmt.where(BacktestRun.strategy_type == strategy_type)
    if status:
        stmt = stmt.where(BacktestRun.status == status)
        count_stmt = count_stmt.where(BacktestRun.status == status)

    total = (await session.execute(count_stmt)).scalar_one()
    total_pages = max(1, math.ceil(total / per_page))
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    stmt = stmt.offset(offset).limit(per_page)
    runs = list((await session.execute(stmt)).scalars().all())

    context = {
        "request": request,
        "runs": runs,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "strategy_type": strategy_type,
        "status": status,
        "strategy_types": [e.value for e in StrategyType],
        "backtest_modes": [e.value for e in BacktestMode],
        "backtest_statuses": [e.value for e in BacktestStatus],
    }

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/backtest_runs.html", context)
    return templates.TemplateResponse("backtest.html", context)


@router.post("/backtest/run", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def backtest_run(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db_session),
):
    """POST /admin/backtest/run — 새 백테스트 실행."""
    form = await request.form()

    strategy_type_val = form.get("strategy_type", "position")
    mode_val = form.get("mode", "technical")
    start_date_val = date.fromisoformat(str(form["start_date"]))
    end_date_val = date.fromisoformat(str(form["end_date"]))
    initial_capital_val = Decimal(str(form.get("initial_capital", "10000000")))
    symbols_raw = str(form.get("symbols", "")).strip()
    symbols_list = [s.strip() for s in symbols_raw.split(",") if s.strip()] if symbols_raw else []

    run_id = uuid4()
    now = datetime.now(UTC)

    db_run = BacktestRun(
        run_id=run_id,
        strategy_type=strategy_type_val,
        mode=mode_val,
        start_date=start_date_val,
        end_date=end_date_val,
        initial_capital=initial_capital_val,
        slippage_bps=10,
        symbols=symbols_list or None,
        parameters=None,
        status=BacktestStatus.PENDING.value,
        total_trades=0,
        started_at=now,
    )
    session.add(db_run)
    await session.commit()

    config = BacktestConfig(
        strategy_type=StrategyType(strategy_type_val),
        start_date=start_date_val,
        end_date=end_date_val,
        initial_capital=initial_capital_val,
        symbols=symbols_list,
        mode=BacktestMode(mode_val),
    )
    background_tasks.add_task(_execute_backtest, run_id, config)

    return RedirectResponse(url="/admin/backtest", status_code=303)


@router.get("/backtest/{run_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def backtest_detail(
    request: Request,
    run_id: UUID,
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/backtest/{run_id} — 백테스트 상세 (HTMX partial)."""
    stmt = select(BacktestRun).where(BacktestRun.run_id == run_id)
    run = (await session.execute(stmt)).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    trades = []
    if run.status == BacktestStatus.COMPLETED.value:
        trade_stmt = (
            select(BacktestTrade)
            .where(BacktestTrade.run_id == run_id)
            .order_by(BacktestTrade.trade_date)
        )
        trades = list((await session.execute(trade_stmt)).scalars().all())

    return templates.TemplateResponse("partials/backtest_detail.html", {
        "request": request,
        "run": run,
        "trades": trades,
    })


# ── Scheduler ───────────────────────────────────────────────────────────


async def _build_scheduler_context(
    request: Request,
    session: AsyncSession,
    *,
    job_name: str | None = None,
    status: str | None = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """스케줄러 상태 + 작업 이력 컨텍스트 공통 빌더."""
    from src.main import get_scheduler

    try:
        scheduler_status = get_scheduler().get_status()
    except RuntimeError:
        scheduler_status = {"is_running": False, "is_paused": False, "jobs": [], "disabled": True}

    # 작업 이름 목록 (필터 드롭다운용)
    job_names = [j["name"] for j in scheduler_status.get("jobs", [])]

    # 작업 이력 쿼리
    stmt = select(JobExecution).order_by(JobExecution.started_at.desc())
    count_stmt = select(func.count(JobExecution.id))

    if job_name:
        stmt = stmt.where(JobExecution.job_name == job_name)
        count_stmt = count_stmt.where(JobExecution.job_name == job_name)
    if status:
        stmt = stmt.where(JobExecution.status == status)
        count_stmt = count_stmt.where(JobExecution.status == status)

    total = (await session.execute(count_stmt)).scalar_one()
    total_pages = max(1, math.ceil(total / per_page))
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    stmt = stmt.offset(offset).limit(per_page)
    history = list((await session.execute(stmt)).scalars().all())

    return {
        "request": request,
        "scheduler": scheduler_status,
        "job_names": job_names,
        "history": history,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "job_name": job_name,
        "status": status,
    }


@router.get("/scheduler", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def scheduler_page(
    request: Request,
    job_name: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/scheduler — 스케줄러 상태 + 작업 목록 + 이력."""
    context = await _build_scheduler_context(
        request, session, job_name=job_name, status=status, page=page, per_page=per_page,
    )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/scheduler_status.html", context)
    return templates.TemplateResponse("scheduler.html", context)


@router.post("/scheduler/pause", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def scheduler_pause(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """POST /admin/scheduler/pause — 스케줄러 일시정지."""
    from src.main import get_scheduler

    get_scheduler().pause_all()
    context = await _build_scheduler_context(request, session)
    return templates.TemplateResponse("partials/scheduler_status.html", context)


@router.post("/scheduler/resume", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def scheduler_resume(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """POST /admin/scheduler/resume — 스케줄러 재개."""
    from src.main import get_scheduler

    get_scheduler().resume_all()
    context = await _build_scheduler_context(request, session)
    return templates.TemplateResponse("partials/scheduler_status.html", context)


@router.post("/scheduler/run/{job_name:path}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def scheduler_run_job(
    request: Request,
    job_name: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db_session),
):
    """POST /admin/scheduler/run/{job_name} — 작업 즉시 실행."""
    from src.main import get_scheduler

    scheduler = get_scheduler()
    background_tasks.add_task(scheduler.run_job_now, job_name)
    context = await _build_scheduler_context(request, session)
    return templates.TemplateResponse("partials/scheduler_status.html", context)


# ── Stock Master ───────────────────────────────────────────────────────


async def _get_stock_master_stats(session: AsyncSession) -> dict:
    """stock_master 테이블 통계 조회."""
    total = (await session.execute(
        select(func.count(StockMaster.symbol))
    )).scalar_one()
    kospi = (await session.execute(
        select(func.count(StockMaster.symbol)).where(
            StockMaster.market_type == "kospi", StockMaster.is_active.is_(True),
        )
    )).scalar_one()
    kosdaq = (await session.execute(
        select(func.count(StockMaster.symbol)).where(
            StockMaster.market_type == "kosdaq", StockMaster.is_active.is_(True),
        )
    )).scalar_one()
    inactive = (await session.execute(
        select(func.count(StockMaster.symbol)).where(StockMaster.is_active.is_(False))
    )).scalar_one()
    last_updated = (await session.execute(
        select(func.max(StockMaster.updated_at))
    )).scalar_one()

    return {
        "total": total,
        "kospi": kospi,
        "kosdaq": kosdaq,
        "inactive": inactive,
        "last_updated": last_updated.strftime("%Y-%m-%d %H:%M") if last_updated else None,
    }


_SYNC_NS = "stock_master_sync"
_SYNC_KEY = "status"
_SYNC_TTL = 300  # 5분


async def _set_sync_status(cache, data: dict) -> None:
    """Redis에 동기화 상태 저장 (실패 시 로그만)."""
    try:
        await cache.set_json(_SYNC_NS, _SYNC_KEY, data, ttl=_SYNC_TTL)
    except Exception:
        logger.warning("stock_master_sync_status_write_failed", data=data)


async def _send_sync_failure_telegram(error_msg: str) -> None:
    """동기화 실패 시 텔레그램 알림 발송 (실패 시 로그만)."""
    try:
        from src.main import get_telegram_bot

        bot = get_telegram_bot()
        await bot.send_message(
            f"<b>종목 마스터 동기화 실패</b>\n에러: {error_msg}"
        )
    except Exception:
        logger.warning("stock_master_sync_telegram_failed", error=error_msg)


async def _sync_stock_master_background(session_factory) -> None:
    """BackgroundTasks에서 실행되는 stock_master 동기화."""
    from src.config import get_settings
    from src.data.cache import get_cache

    cache = get_cache()

    await _set_sync_status(cache, {
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
    })

    try:
        from src.broker.kis.client import KISClient
        from src.data.providers.kis_provider import KISDataProvider

        settings = get_settings()

        # stock_master는 공개 .mst.zip 다운로드라 인증 불필요 — 임시 KISClient 사용
        client = KISClient(settings=settings, cache=cache)
        await client.connect()
        try:
            provider = KISDataProvider(
                client=client,
                cache=cache,
                session_factory=session_factory,
                settings=settings,
            )
            count = await provider.sync_stock_master()
            logger.info("stock_master_sync_manual_done", upserted=count)
            await _set_sync_status(cache, {"status": "completed", "count": count})
            # 텔레그램 성공 알림
            try:
                from src.main import get_telegram_bot
                bot = get_telegram_bot()
                await bot.send_message(
                    f"<b>종목 마스터 동기화 완료</b>\n갱신: {count}건"
                )
            except Exception:
                logger.warning("stock_master_sync_success_telegram_failed", count=count)
        finally:
            await client.disconnect()

    except Exception as exc:
        error = str(exc)
        logger.exception("stock_master_sync_failed", error=error)
        await _set_sync_status(cache, {"status": "failed", "error": error})
        await _send_sync_failure_telegram(error)


@router.get("/stock-master", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def stock_master_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/stock-master — 종목 마스터 관리: 통계 + 최근 업데이트 10건."""
    stats = await _get_stock_master_stats(session)

    result = await session.execute(
        select(StockMaster).order_by(StockMaster.updated_at.desc()).limit(10)
    )
    stocks = list(result.scalars().all())

    return templates.TemplateResponse("stock_master.html", {
        "request": request,
        "stats": stats,
        "stocks": stocks,
    })


@router.post("/stock-master/sync", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def stock_master_sync(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """POST /admin/stock-master/sync — 종목 마스터 동기화 (비동기)."""
    from src.data.cache import get_cache

    cache = get_cache()

    # 이미 실행 중이면 중복 방지
    try:
        existing = await cache.get_json(_SYNC_NS, _SYNC_KEY)
        if existing and existing.get("status") == "running":
            return templates.TemplateResponse("partials/stock_master_sync_status.html", {
                "request": request,
                "sync_status": "running",
                "sync_count": 0,
                "sync_error": None,
            })
    except Exception:
        pass

    session_factory = get_session_factory()
    background_tasks.add_task(_sync_stock_master_background, session_factory)
    return templates.TemplateResponse("partials/stock_master_sync_status.html", {
        "request": request,
        "sync_status": "running",
        "sync_count": 0,
        "sync_error": None,
    })


@router.get("/stock-master/sync/status", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def stock_master_sync_status(request: Request):
    """GET /admin/stock-master/sync/status — 동기화 상태 폴링."""
    from src.data.cache import get_cache

    cache = get_cache()
    status = "idle"
    count = 0
    error = None

    try:
        data = await cache.get_json(_SYNC_NS, _SYNC_KEY)
        if data:
            status = data.get("status", "idle")
            count = data.get("count", 0)
            error = data.get("error")
    except Exception:
        pass

    return templates.TemplateResponse("partials/stock_master_sync_status.html", {
        "request": request,
        "sync_status": status,
        "sync_count": count,
        "sync_error": error,
    })
