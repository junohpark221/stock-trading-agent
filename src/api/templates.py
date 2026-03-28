"""Jinja2 template engine singleton for admin web UI."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
