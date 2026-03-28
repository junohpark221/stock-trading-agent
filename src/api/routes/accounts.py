"""Account CRUD API routes — 계좌 관리.

Endpoints:
    GET    /api/accounts                    — 계좌 목록 조회
    POST   /api/accounts                    — 계좌 생성 (KIS 인증 암호화 저장)
    GET    /api/accounts/{account_id}       — 계좌 상세 (KIS secret 미노출)
    PUT    /api/accounts/{account_id}       — 계좌 수정 (부분 업데이트)
    PUT    /api/accounts/{account_id}/prompt — 투자 철학 프롬프트 변경
    DELETE /api/accounts/{account_id}       — 계좌 비활성화 (soft delete)
"""

from __future__ import annotations

import re
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.core.enums import StrategyType
from src.db.models.account import Account, AccountCrypto
from src.db.session import get_db_session

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


# ── Request/Response Models ──────────────────────────────────────────────


class CreateAccountRequest(BaseModel):
    id: str | None = Field(
        None, max_length=50, description="계좌 ID (미지정 시 nickname 기반 slug)",
    )
    nickname: str = Field(..., min_length=1, max_length=50)
    kis_app_key: str = Field(..., min_length=1)
    kis_app_secret: str = Field(..., min_length=1)
    kis_account_no: str = Field(..., min_length=1, max_length=20)
    kis_account_prod: str = Field("01", max_length=5)
    kis_is_paper: bool = True
    kis_hts_id: str = Field("", max_length=50)
    strategy_type: StrategyType = StrategyType.SWING
    investment_prompt: str = ""
    risk_overrides: dict | None = None


class UpdateAccountRequest(BaseModel):
    nickname: str | None = Field(None, max_length=50)
    kis_account_prod: str | None = Field(None, max_length=5)
    kis_is_paper: bool | None = None
    kis_hts_id: str | None = Field(None, max_length=50)
    strategy_type: StrategyType | None = None
    risk_overrides: dict | None = None


class UpdatePromptRequest(BaseModel):
    investment_prompt: str


class AccountSummary(BaseModel):
    id: str
    nickname: str
    strategy_type: str
    is_active: bool
    account_no_masked: str


class AccountDetail(AccountSummary):
    investment_prompt: str
    risk_overrides: dict | None
    kis_is_paper: bool
    created_at: datetime


# ── Helpers ──────────────────────────────────────────────────────────────


def _mask_account_no(account_no: str) -> str:
    """계좌번호 마스킹: '12345678' → '****5678'."""
    if len(account_no) <= 4:
        return account_no
    return "*" * (len(account_no) - 4) + account_no[-4:]


def _slugify(text: str) -> str:
    """닉네임을 URL-safe slug로 변환."""
    slug = re.sub(r"[^a-z0-9가-힣-]", "-", text.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "account"


def _to_summary(account: Account) -> dict:
    return {
        "id": account.id,
        "nickname": account.nickname,
        "strategy_type": account.strategy_type,
        "is_active": account.is_active,
        "account_no_masked": _mask_account_no(account.kis_account_no),
    }


def _to_detail(account: Account) -> dict:
    return {
        **_to_summary(account),
        "investment_prompt": account.investment_prompt,
        "risk_overrides": account.risk_overrides,
        "kis_is_paper": account.kis_is_paper,
        "created_at": account.created_at.isoformat() if account.created_at else None,
    }


# ── GET /api/accounts ────────────────────────────────────────────────────


@router.get("")
async def list_accounts(
    is_active: bool | None = Query(None, description="활성 상태 필터"),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """계좌 목록 조회."""
    try:
        stmt = select(Account).order_by(Account.created_at)
        if is_active is not None:
            stmt = stmt.where(Account.is_active == is_active)

        rows = (await session.execute(stmt)).scalars().all()
        items = [_to_summary(row) for row in rows]
        return JSONResponse(content={"items": items, "total": len(items)})

    except Exception:
        logger.exception("list_accounts_failed")
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── POST /api/accounts ───────────────────────────────────────────────────


@router.post("", status_code=201)
async def create_account(
    req: CreateAccountRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """계좌 생성 — KIS 인증정보를 Fernet 암호화하여 저장."""
    settings = get_settings()

    if not settings.ACCOUNT_ENCRYPTION_KEY:
        raise HTTPException(
            status_code=422,
            detail="ACCOUNT_ENCRYPTION_KEY is not configured",
        )

    account_id = req.id or _slugify(req.nickname)

    # 중복 확인
    existing = (
        await session.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Account '{account_id}' already exists")

    try:
        enc_key = settings.ACCOUNT_ENCRYPTION_KEY
        account = Account(
            id=account_id,
            nickname=req.nickname,
            kis_app_key_enc=AccountCrypto.encrypt(req.kis_app_key, enc_key),
            kis_app_secret_enc=AccountCrypto.encrypt(req.kis_app_secret, enc_key),
            kis_account_no=req.kis_account_no,
            kis_account_prod=req.kis_account_prod,
            kis_is_paper=req.kis_is_paper,
            kis_hts_id=req.kis_hts_id,
            strategy_type=req.strategy_type.value,
            investment_prompt=req.investment_prompt,
            risk_overrides=req.risk_overrides,
            is_active=True,
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)

        logger.info("account_created", account_id=account_id)
        return JSONResponse(content=_to_detail(account), status_code=201)

    except HTTPException:
        raise
    except Exception:
        logger.exception("create_account_failed")
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── GET /api/accounts/{account_id} ───────────────────────────────────────


@router.get("/{account_id}")
async def get_account(
    account_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """계좌 상세 조회 — KIS secret 미노출."""
    try:
        account = (
            await session.execute(select(Account).where(Account.id == account_id))
        ).scalar_one_or_none()

        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")

        return JSONResponse(content=_to_detail(account))

    except HTTPException:
        raise
    except Exception:
        logger.exception("get_account_failed", account_id=account_id)
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── PUT /api/accounts/{account_id} ───────────────────────────────────────


@router.put("/{account_id}")
async def update_account(
    account_id: str,
    req: UpdateAccountRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """계좌 수정 — non-None 필드만 업데이트."""
    try:
        account = (
            await session.execute(select(Account).where(Account.id == account_id))
        ).scalar_one_or_none()

        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")

        updates = req.model_dump(exclude_none=True)
        if "strategy_type" in updates:
            updates["strategy_type"] = updates["strategy_type"].value

        for field, value in updates.items():
            setattr(account, field, value)

        await session.commit()
        await session.refresh(account)

        logger.info("account_updated", account_id=account_id, fields=list(updates.keys()))
        return JSONResponse(content=_to_detail(account))

    except HTTPException:
        raise
    except Exception:
        logger.exception("update_account_failed", account_id=account_id)
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── PUT /api/accounts/{account_id}/prompt ────────────────────────────────


@router.put("/{account_id}/prompt")
async def update_prompt(
    account_id: str,
    req: UpdatePromptRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """투자 철학 프롬프트 변경 — 배포 없이 즉시 반영."""
    try:
        account = (
            await session.execute(select(Account).where(Account.id == account_id))
        ).scalar_one_or_none()

        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")

        account.investment_prompt = req.investment_prompt
        await session.commit()

        logger.info("account_prompt_updated", account_id=account_id)
        return JSONResponse(content={
            "success": True,
            "account_id": account_id,
            "investment_prompt": req.investment_prompt,
        })

    except HTTPException:
        raise
    except Exception:
        logger.exception("update_prompt_failed", account_id=account_id)
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── DELETE /api/accounts/{account_id} ────────────────────────────────────


@router.delete("/{account_id}")
async def delete_account(
    account_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """계좌 비활성화 (soft delete) — is_active=False."""
    try:
        account = (
            await session.execute(select(Account).where(Account.id == account_id))
        ).scalar_one_or_none()

        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")

        account.is_active = False
        await session.commit()

        logger.info("account_deactivated", account_id=account_id)
        return JSONResponse(content={
            "success": True,
            "account_id": account_id,
            "is_active": False,
        })

    except HTTPException:
        raise
    except Exception:
        logger.exception("delete_account_failed", account_id=account_id)
        raise HTTPException(status_code=500, detail="Internal server error") from None
