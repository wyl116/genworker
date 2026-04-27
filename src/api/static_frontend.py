"""Static frontend mounting helpers."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles

from src.common.logger import get_logger

logger = get_logger()
_WARNED_MISSING_DIST = False


class FrontendStaticFiles(StaticFiles):
    """StaticFiles variant that preserves utf-8 HTML content type."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        if isinstance(response, FileResponse) and str(response.path).endswith("index.html"):
            response.headers["content-type"] = "text/html; charset=utf-8"
        return response


def mount_frontend(app: FastAPI) -> None:
    """Mount frontend/dist at / when present."""
    global _WARNED_MISSING_DIST

    project_root = Path(__file__).resolve().parents[2]
    dist_dir = project_root / "frontend" / "dist"
    if not dist_dir.is_dir():
        if not _WARNED_MISSING_DIST:
            logger.warning("frontend/dist not found, static frontend disabled")
            _WARNED_MISSING_DIST = True
        return

    app.mount(
        "/",
        FrontendStaticFiles(directory=str(dist_dir), html=True),
        name="frontend",
    )
