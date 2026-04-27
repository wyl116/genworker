# edition: baseline
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.static_frontend import mount_frontend


def test_mount_frontend_serves_index(tmp_path: Path, monkeypatch) -> None:
    frontend_dist = tmp_path / "frontend" / "dist"
    frontend_dist.mkdir(parents=True)
    (frontend_dist / "index.html").write_text("<html><body>hello</body></html>", encoding="utf-8")
    monkeypatch.setattr(
        "src.api.static_frontend.Path.resolve",
        lambda self: Path(str(tmp_path / "src" / "api" / "static_frontend.py")),
    )

    app = FastAPI()
    mount_frontend(app)
    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
