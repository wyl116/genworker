"""API wiring initializer shell for bootstrap registration."""
from __future__ import annotations

from .base import Initializer
from .context import BootstrapContext
from src.runtime.api_wiring import initialize_api_wiring


class ApiWiringInitializer(Initializer):
    """Thin initializer shell delegating implementation to runtime.api_wiring."""

    @property
    def name(self) -> str:
        return "api_wiring"

    @property
    def depends_on(self) -> list[str]:
        return ["workers"]

    @property
    def priority(self) -> int:
        return 100

    async def initialize(self, context: BootstrapContext) -> bool:
        return await initialize_api_wiring(context)

    async def cleanup(self) -> None:
        pass
