"""Backoffice LLM 설정 뷰 — 에이전트별 모델 배분 현황 + YAML 기본값 재시드.

라우팅 해석 순서(Redis → DB → YAML) 때문에 YAML 커밋만으로는 반영되지 않는다.
이 화면의 reset 버튼이 SSH 없이 DB 재시드 + 캐시 클리어를 수행하는 경로다.
DB 현행값이 YAML 기본값과 다르면(어드민 PUT 오버라이드 등) 드리프트로 표시한다.
"""

from decimal import Decimal
from typing import Any

import structlog
import yaml
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.admin.llm_config import _YAML_PATH, reset_configs
from src.api.auth import require_admin
from src.api.routes.admin_web._common import redirect_with
from src.api.templates import templates
from src.db.models.llm import AgentModelConfigDB
from src.db.session import get_db_session

logger = structlog.get_logger(__name__)

router = APIRouter()

_FIELDS = ("routing_mode", "primary_model", "escalation_model", "confidence_threshold")


def _normalize(value: Any) -> Any:
    """드리프트 비교용 정규화 — threshold는 Decimal 수치 동등 비교."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float | int):
        return Decimal(str(value))
    return value


def _build_rows(
    db_rows: list[AgentModelConfigDB], yaml_agents: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """YAML 순서 기준으로 에이전트별 DB 현행값 ↔ YAML 기본값 비교 행 구성."""
    db_by_agent = {r.agent_type: r for r in db_rows}
    agent_order = list(yaml_agents) + [
        a for a in db_by_agent if a not in yaml_agents
    ]

    rows = []
    for agent_type in agent_order:
        db_row = db_by_agent.get(agent_type)
        yaml_cfg = yaml_agents.get(agent_type)
        fields = {}
        drift = False
        for key in _FIELDS:
            db_value = getattr(db_row, key, None) if db_row else None
            yaml_value = yaml_cfg.get(key) if yaml_cfg else None
            field_drift = (
                db_row is not None
                and yaml_cfg is not None
                and _normalize(db_value) != _normalize(yaml_value)
            )
            drift = drift or field_drift
            fields[key] = {"db": db_value, "yaml": yaml_value, "drift": field_drift}
        rows.append({
            "agent_type": agent_type,
            "db_row": db_row,
            "fields": fields,
            "drift": drift,
        })
    return rows


@router.get("/llm-config", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def llm_config_page(
    request: Request,
    msg: str | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/llm-config — 에이전트별 모델 배분 현황 (DB ↔ YAML 대조)."""
    result = await session.execute(select(AgentModelConfigDB))
    db_rows = list(result.scalars().all())

    with open(_YAML_PATH) as f:
        yaml_agents = yaml.safe_load(f).get("agents", {})

    rows = _build_rows(db_rows, yaml_agents)
    return templates.TemplateResponse("llm_config.html", {
        "request": request,
        "rows": rows,
        "has_drift": any(r["drift"] for r in rows),
        "msg": msg,
    })


@router.post("/llm-config/reset", dependencies=[Depends(require_admin)])
async def llm_config_reset(session: AsyncSession = Depends(get_db_session)):
    """POST /admin/llm-config/reset — YAML 기본값으로 DB 재시드 + Redis 캐시 클리어."""
    result = await reset_configs(session=session)
    count = result["reset_count"]
    logger.info("admin.llm_config_reset", reset_count=count)
    return redirect_with("/admin/llm-config", msg=f"YAML 기본값으로 {count}건 재시드 완료 (캐시 클리어 포함)")
