"""
Logging system initializer.

Initializes the application logging system including file handlers,
formatters, and log filters.
"""
from typing import List

from .base import Initializer
from .context import BootstrapContext


class LoggingInitializer(Initializer):
    """
    Initializer for the logging subsystem.

    This should be one of the first initializers to run.
    """

    @property
    def name(self) -> str:
        return "logging"

    @property
    def depends_on(self) -> List[str]:
        return []

    @property
    def priority(self) -> int:
        return 1

    @property
    def required(self) -> bool:
        return True

    async def initialize(self, context: BootstrapContext) -> bool:
        """Initialize the logging system."""
        from src.common.logger import initialize_logging

        try:
            logger = initialize_logging()
            context.set_state("logger", logger)
            return True
        except Exception as e:
            print(f"Failed to initialize logging: {e}")
            return False

    async def cleanup(self) -> None:
        """Cleanup logging resources."""
        try:
            from src.common.logger import shutdown_logging
            shutdown_logging()
        except Exception as e:
            print(f"Error during logging cleanup: {e}")
