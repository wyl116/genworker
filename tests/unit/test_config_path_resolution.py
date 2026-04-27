# edition: baseline
import sys
from types import SimpleNamespace

from src.bootstrap import create_orchestrator
from src.common.paths import default_workspace_root, project_root, resolve_workspace_root
from pathlib import Path

from src.common.config.mcp_config import MCPConfigLoader
from src.tools.mcp.server import MCPServer
from src.tools.scanner import MCPScanner
import start


PROJECT_ROOT = Path("/Users/weiyilan/PycharmProjects/genworker")


def test_mcp_config_loader_uses_project_root_when_cwd_changes(monkeypatch):
    monkeypatch.chdir(PROJECT_ROOT / "src")

    loader = MCPConfigLoader()
    config = loader.load_for_environment()

    assert config is not None
    assert loader._base_path == PROJECT_ROOT


def test_mcp_scanner_default_config_path_is_cwd_independent(monkeypatch):
    monkeypatch.chdir(PROJECT_ROOT / "src")

    scanner = MCPScanner(MCPServer(name="test", version="1.0.0"))
    config = scanner._load_config()

    assert config is not None
    assert Path(scanner._config_path) == PROJECT_ROOT / "configs" / "mcp_servers.json"


def test_default_workspace_root_is_project_rooted():
    assert project_root() == PROJECT_ROOT
    assert default_workspace_root() == PROJECT_ROOT / "workspace"
    assert resolve_workspace_root() == PROJECT_ROOT / "workspace"
    assert resolve_workspace_root("workspace-alt") == PROJECT_ROOT / "workspace-alt"


def test_orchestrator_sets_project_rooted_workspace_by_default():
    orchestrator = create_orchestrator()

    assert orchestrator._initial_state["workspace_root"] == PROJECT_ROOT / "workspace"


def test_start_main_changes_cwd_to_project_root(monkeypatch):
    calls: dict[str, object] = {}

    def _fake_chdir(path: str) -> None:
        calls["chdir"] = path

    def _fake_run(*args, **kwargs) -> None:
        calls["uvicorn"] = {"args": args, "kwargs": kwargs}

    monkeypatch.setattr(start.os, "chdir", _fake_chdir)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=_fake_run))

    start.main()

    assert calls["chdir"] == str(PROJECT_ROOT)
    assert calls["uvicorn"]["kwargs"]["host"] == "0.0.0.0"
