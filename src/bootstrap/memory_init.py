"""Memory system initializer."""
from typing import List

from src.common.logger import get_logger
from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus

from .base import Initializer
from .context import BootstrapContext

logger = get_logger()


class MemoryInitializer(Initializer):
    """
    Initializer for the memory subsystem.

    Handles:
    - OpenViking client initialization
    - Health check verification
    """

    def __init__(self) -> None:
        self._client = None
        self._runtime_status = ComponentRuntimeStatus(
            component="openviking",
            enabled=False,
            status=ComponentStatus.DISABLED,
            selected_backend="openviking",
            primary_backend="openviking",
        )

    @property
    def name(self) -> str:
        return "memory"

    @property
    def depends_on(self) -> List[str]:
        return ["logging"]

    @property
    def priority(self) -> int:
        return 50

    @property
    def required(self) -> bool:
        return False

    async def initialize(self, context: BootstrapContext) -> bool:
        """Initialize memory system."""
        settings = context.settings
        context.set_state("openviking_client", None)

        if not settings.openviking_enabled:
            self._runtime_status = ComponentRuntimeStatus(
                component="openviking",
                enabled=False,
                status=ComponentStatus.DISABLED,
                selected_backend="openviking",
                primary_backend="openviking",
            )
            logger.info(
                "[MemoryInit] component=openviking status=disabled backend=openviking"
            )
            context.register_runtime_component(
                "openviking",
                lambda: self._runtime_status,
                required=self.required,
            )
            return True

        if not settings.openviking_endpoint.strip():
            self._runtime_status = ComponentRuntimeStatus(
                component="openviking",
                enabled=True,
                status=ComponentStatus.FAILED,
                selected_backend="openviking",
                primary_backend="openviking",
                last_error="endpoint_empty",
            )
            logger.warning(
                "[MemoryInit] component=openviking status=failed backend=openviking reason=endpoint_empty"
            )
            context.record_error(self.name, "endpoint_empty")
            context.register_runtime_component(
                "openviking",
                lambda: self._runtime_status,
                required=self.required,
            )
            return True

        try:
            from src.memory.backends.openviking import OpenVikingClient

            client = OpenVikingClient(
                endpoint=settings.openviking_endpoint,
                timeout_seconds=settings.openviking_timeout_seconds,
            )
            self._client = client
            context.set_state("openviking_client", client)

            health = await client.health_check(raise_on_error=True)
            if health:
                self._runtime_status = ComponentRuntimeStatus(
                    component="openviking",
                    enabled=True,
                    status=ComponentStatus.READY,
                    selected_backend="openviking",
                    primary_backend="openviking",
                )
                context.memory_ready = True
                logger.info(
                    "[MemoryInit] component=openviking status=ready backend=openviking"
                )
            else:
                self._runtime_status = ComponentRuntimeStatus(
                    component="openviking",
                    enabled=True,
                    status=ComponentStatus.DEGRADED,
                    selected_backend="openviking",
                    primary_backend="openviking",
                    last_error="health_check_unhealthy",
                )
                logger.warning(
                    "[MemoryInit] component=openviking status=degraded backend=openviking reason=health_check_unhealthy"
                )
                context.record_error(self.name, "health_check_unhealthy")
            return True

        except ImportError as exc:
            self._runtime_status = ComponentRuntimeStatus(
                component="openviking",
                enabled=True,
                status=ComponentStatus.FAILED,
                selected_backend="openviking",
                primary_backend="openviking",
                last_error=str(exc).splitlines()[0][:200],
            )
            logger.warning(
                "[MemoryInit] component=openviking status=failed backend=openviking last_error=%s",
                self._runtime_status.last_error,
            )
            context.record_error(self.name, self._runtime_status.last_error)
            return True

        except Exception as exc:
            self._runtime_status = ComponentRuntimeStatus(
                component="openviking",
                enabled=True,
                status=ComponentStatus.FAILED,
                selected_backend="openviking",
                primary_backend="openviking",
                last_error=str(exc).splitlines()[0][:200],
            )
            logger.warning(
                "[MemoryInit] component=openviking status=failed backend=openviking last_error=%s",
                self._runtime_status.last_error,
            )
            context.record_error(self.name, self._runtime_status.last_error)
            return True

        finally:
            context.register_runtime_component(
                "openviking",
                lambda: self._runtime_status,
                required=self.required,
            )

    async def cleanup(self) -> None:
        """Cleanup memory resources."""
        if self._client is not None:
            await self._client.close()
            self._client = None
