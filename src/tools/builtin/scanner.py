"""Discovery for decorator-registered pure builtin tools."""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from src.common.logger import get_logger

from .registry import get_registered_factories

logger = get_logger()

_EXCLUDED_MODULES = frozenset({
    "__init__",
    "agent_tool",
    "bash_sandbox",
    "bash_security",
    "email_tools",
    "task_store",
    "task_tools",
    "workspace_sandbox",
})


def scan_builtin_tools(package_path: str = "src.tools.builtin") -> tuple:
    """Import builtin modules to trigger decorator side effects."""
    package = importlib.import_module(package_path)
    package_dir = Path(package.__file__).parent

    for module_info in pkgutil.iter_modules([str(package_dir)]):
        name = module_info.name
        if name.startswith("_") or name in _EXCLUDED_MODULES:
            continue
        importlib.import_module(f"{package_path}.{name}")

    factories = get_registered_factories()
    logger.info("[BuiltinScanner] Loaded %s builtin factory specs", len(factories))
    return factories

