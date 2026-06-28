"""Jinja2 template engine singleton for admin web UI."""

from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from src.core.time import to_kst

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


def _kst(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """UTC aware datetime을 KST로 변환해 표기. None은 '-'."""
    if value is None:
        return "-"
    return to_kst(value).strftime(fmt)


templates.env.filters["kst"] = _kst
