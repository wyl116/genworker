"""
Bootstrap orchestrator for coordinating application startup and shutdown.

Manages initialization order of subsystems based on their dependencies
using topological sort (Kahn's algorithm).
"""
import asyncio
from typing import Dict, List, Optional

from src.common.logger import get_logger

from .base import Initializer
from .context import BootstrapContext

logger = get_logger()


class DependencyError(Exception):
    """Raised when there is an issue with initializer dependencies."""
    pass


class BootstrapOrchestrator:
    """
    Orchestrates application startup and shutdown.

    - Registers initializers
    - Resolves dependency order using topological sort
    - Executes initializers in the correct order
    - Handles cleanup in reverse order during shutdown
    """

    def __init__(self):
        self._initializers: Dict[str, Initializer] = {}
        self._initialized: List[Initializer] = []
        self._context: Optional[BootstrapContext] = None
        self._initial_state: Dict[str, object] = {}

    def set_initial_state(self, key: str, value: object) -> None:
        """Set initial context state before startup."""
        self._initial_state[key] = value

    def register(self, initializer: Initializer) -> "BootstrapOrchestrator":
        """Register an initializer. Returns self for chaining."""
        name = initializer.name
        if name in self._initializers:
            raise ValueError(f"Initializer '{name}' is already registered")
        self._initializers[name] = initializer
        logger.debug(
            f"Registered initializer: {name} "
            f"(depends_on: {initializer.depends_on})"
        )
        return self

    def _topological_sort(self) -> List[Initializer]:
        """Sort initializers by dependency order using Kahn's algorithm."""
        in_degree: Dict[str, int] = {name: 0 for name in self._initializers}
        graph: Dict[str, List[str]] = {name: [] for name in self._initializers}

        for name, initializer in self._initializers.items():
            for dep in initializer.depends_on:
                if dep not in self._initializers:
                    raise DependencyError(
                        f"Initializer '{name}' depends on '{dep}' "
                        f"which is not registered"
                    )
                graph[dep].append(name)
                in_degree[name] += 1

        queue: List[str] = [
            name for name, degree in in_degree.items() if degree == 0
        ]
        queue.sort(key=lambda n: self._initializers[n].priority)

        result: List[Initializer] = []
        while queue:
            current = queue.pop(0)
            result.append(self._initializers[current])

            for dependent in graph[current]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)
                    queue.sort(key=lambda n: self._initializers[n].priority)

        if len(result) != len(self._initializers):
            remaining = set(self._initializers.keys()) - {
                i.name for i in result
            }
            raise DependencyError(
                f"Circular dependency detected involving: {remaining}"
            )

        return result

    async def startup(
        self, settings: Optional[object] = None,
    ) -> BootstrapContext:
        """Execute all initializers in dependency order."""
        if settings is None:
            from src.common.settings import get_settings
            settings = get_settings()

        self._context = BootstrapContext(settings=settings)
        self._initialized = []

        # Apply initial state
        for key, value in self._initial_state.items():
            self._context.set_state(key, value)

        try:
            sorted_initializers = self._topological_sort()
        except DependencyError as e:
            logger.error(f"Dependency resolution failed: {e}")
            self._context.record_error("orchestrator", str(e))
            return self._context

        logger.info("=" * 60)
        logger.info("Starting bootstrap sequence...")
        logger.info(
            f"Initializers to run: {[i.name for i in sorted_initializers]}"
        )
        logger.info("=" * 60)

        for i, initializer in enumerate(sorted_initializers, 1):
            name = initializer.name
            logger.info(
                f"[{i}/{len(sorted_initializers)}] Initializing: {name}"
            )

            try:
                success = await initializer.initialize(self._context)

                if success:
                    logger.info(f"  {name} initialized successfully")
                    self._initialized.append(initializer)
                else:
                    error_msg = "Initialization returned False"
                    logger.warning(f"  {name} failed: {error_msg}")
                    self._context.record_error(name, error_msg)

                    if initializer.required:
                        logger.error(
                            f"Required initializer '{name}' failed, "
                            f"aborting startup"
                        )
                        break

            except Exception as e:
                error_msg = str(e)
                logger.error(f"  {name} failed with exception: {error_msg}")
                self._context.record_error(name, error_msg)

                if initializer.required:
                    logger.error(
                        f"Required initializer '{name}' failed, "
                        f"aborting startup"
                    )
                    break

        logger.info("=" * 60)
        if self._context.has_errors():
            logger.warning(
                f"Bootstrap completed with {len(self._context.errors)} error(s)"
            )
        else:
            logger.info("Bootstrap completed successfully")
        logger.info("=" * 60)

        return self._context

    async def shutdown(self) -> None:
        """Execute cleanup for all initialized subsystems in reverse order."""
        if not self._initialized:
            logger.info("No initializers to clean up")
            return

        logger.info("=" * 60)
        logger.info("Starting shutdown sequence...")
        logger.info("=" * 60)

        for i, initializer in enumerate(reversed(self._initialized), 1):
            name = initializer.name
            logger.info(
                f"[{i}/{len(self._initialized)}] Cleaning up: {name}"
            )
            try:
                await asyncio.wait_for(initializer.cleanup(), timeout=10)
                logger.info(f"  {name} cleaned up successfully")
            except asyncio.TimeoutError:
                logger.error(f"  {name} cleanup timed out")
            except Exception as e:
                logger.error(f"  {name} cleanup failed: {e}")

        logger.info("=" * 60)
        logger.info("Shutdown sequence completed")
        logger.info("=" * 60)

        self._initialized = []

    @property
    def context(self) -> Optional[BootstrapContext]:
        """Get the current bootstrap context."""
        return self._context
