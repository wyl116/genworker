"""
LiteLLM Router initializer.

Initializes the LiteLLM router for unified LLM access with failover support.
"""
from typing import List

from src.common.logger import get_logger

from .base import Initializer
from .context import BootstrapContext

logger = get_logger()


class LLMInitializer(Initializer):
    """
    Initializer for the LiteLLM Router subsystem.

    Handles:
    - LiteLLM router initialization from env-specific config file
    - Connection warmup for reduced first-request latency
    """

    @property
    def name(self) -> str:
        return "llm"

    @property
    def depends_on(self) -> List[str]:
        return ["logging"]

    @property
    def priority(self) -> int:
        return 40

    @property
    def required(self) -> bool:
        return False

    async def initialize(self, context: BootstrapContext) -> bool:
        """Initialize LiteLLM router."""
        if bool(getattr(context.settings, "community_smoke_profile", False)):
            logger.info("[LLMInit] community_smoke_profile enabled, skipping LiteLLM bootstrap")
            context.llm_ready = True
            return True
        try:
            from src.services.llm import (
                initialize_litellm_router,
                warmup_llm_connection,
            )
            from src.services.llm.config_source import MissingInjectedConfigError

            litellm_router = await initialize_litellm_router()

            if litellm_router:
                models = litellm_router.get_available_models()
                logger.info(
                    f"LiteLLM Router initialized with "
                    f"{len(models)} models: {models}"
                )
                context.set_state("litellm_router", litellm_router)
                context.llm_ready = True
                await self._warmup_connections()
                return True
            else:
                logger.warning(
                    "LiteLLM Router not initialized "
                    "(LiteLLM not installed or config missing)"
                )
                return True

        except ImportError as e:
            logger.warning(f"LiteLLM provider not available: {e}")
            return True

        except MissingInjectedConfigError:
            raise

        except Exception as e:
            logger.error(f"Failed to initialize LiteLLM Router: {e}")
            context.record_error(self.name, str(e))
            return False

    async def _warmup_connections(self) -> None:
        """Warmup LLM connections."""
        try:
            from src.services.llm import warmup_llm_connection

            warmup_results = await warmup_llm_connection()
            if warmup_results:
                for r in warmup_results:
                    if r.success:
                        logger.info(
                            f"LLM warmup: {r.model} ready "
                            f"({r.latency_ms:.0f}ms)"
                        )
                    else:
                        logger.warning(
                            f"LLM warmup: {r.model} failed - {r.error}"
                        )
        except Exception as e:
            logger.warning(f"LLM warmup failed: {e}")

    async def cleanup(self) -> None:
        """Cleanup LiteLLM resources."""
        try:
            from src.services.llm import cleanup_litellm_router
            await cleanup_litellm_router()
            logger.info("LiteLLM Router cleaned up")
        except ImportError:
            pass
        except Exception as e:
            logger.error(f"Failed to cleanup LiteLLM Router: {e}")
