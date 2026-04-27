"""
Bootstrap initializer protocol and base classes.

Defines the Initializer protocol that all subsystem initializers
must implement, providing a consistent interface for startup and cleanup.
"""
from abc import ABC, abstractmethod
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from .context import BootstrapContext


class Initializer(ABC):
    """
    Base class for subsystem initializers.

    Each initializer is responsible for:
    - Initializing a specific subsystem during startup
    - Cleaning up resources during shutdown
    - Declaring dependencies on other initializers
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this initializer."""
        pass

    @property
    def depends_on(self) -> List[str]:
        """List of initializer names that must complete before this one."""
        return []

    @property
    def priority(self) -> int:
        """Priority for ordering initializers with same dependencies (lower = earlier)."""
        return 100

    @property
    def required(self) -> bool:
        """Whether this initializer is required for the application to run."""
        return False

    @abstractmethod
    async def initialize(self, context: "BootstrapContext") -> bool:
        """
        Initialize the subsystem.

        Args:
            context: Shared bootstrap context for storing state and config.

        Returns:
            True if initialization succeeded, False otherwise.
        """
        pass

    @abstractmethod
    async def cleanup(self) -> None:
        """Clean up resources during shutdown. Must be idempotent."""
        pass
