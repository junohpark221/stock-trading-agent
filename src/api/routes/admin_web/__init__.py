"""Backoffice web UI routes (/admin/*).

Domain-split package. Each submodule exposes a prefix-less ``router``; this
module assembles them under the shared ``/admin`` prefix and is the single
import contract (``from src.api.routes.admin_web import router``).
"""

from fastapi import APIRouter

from . import (
    accounts,
    auth,
    backtest,
    dashboard,
    decision_queue_view,
    decisions,
    entry_snapshots,
    exec_monitor,
    memory,
    news,
    performance,
    scheduler,
    stock_master,
)

router = APIRouter(prefix="/admin", tags=["admin-web"])

# Include order preserves the original monolith's route declaration order so
# dynamic paths (e.g. /accounts/{account_id}) keep matching after the split.
for _mod in (
    auth,
    dashboard,
    accounts,
    performance,
    backtest,
    scheduler,
    stock_master,
    decisions,
    decision_queue_view,
    entry_snapshots,
    memory,
    news,
    exec_monitor,
):
    router.include_router(_mod.router)

__all__ = ["router"]
