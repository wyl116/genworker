"""
Bootstrap context for sharing state during application startup.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus


@dataclass
class BootstrapContext:
    """
    Shared context for the bootstrap process.

    Passed to all initializers to allow:
    - Accessing application settings
    - Checking which subsystems are ready
    - Recording initialization errors
    - Sharing state between initializers
    """
    settings: Any = None

    # Subsystem readiness flags
    mcp_ready: bool = False
    llm_ready: bool = False
    tools_ready: bool = False
    memory_ready: bool = False

    # Error tracking
    errors: Dict[str, str] = field(default_factory=dict)

    # General state for inter-initializer communication
    state: Dict[str, Any] = field(default_factory=dict)
    _runtime_components: Dict[str, Callable[[], ComponentRuntimeStatus]] = field(
        default_factory=dict
    )
    _runtime_component_requirements: Dict[str, bool] = field(default_factory=dict)

    def record_error(self, initializer_name: str, error: str) -> None:
        """Record an initialization error."""
        self.errors[initializer_name] = error

    def has_errors(self) -> bool:
        """Check if any initialization errors occurred."""
        return len(self.errors) > 0

    def get_error(self, initializer_name: str) -> Optional[str]:
        """Get error message for a specific initializer, if any."""
        return self.errors.get(initializer_name)

    def set_state(self, key: str, value: Any) -> None:
        """Set a state value for inter-initializer communication."""
        self.state[key] = value

    def get_state(self, key: str, default: Any = None) -> Any:
        """Get a state value, with optional default."""
        return self.state.get(key, default)

    def register_runtime_component(
        self,
        name: str,
        provider: Callable[[], ComponentRuntimeStatus],
        *,
        required: bool = False,
    ) -> None:
        """Register a read-only runtime status provider for routes."""
        self._runtime_components[name] = provider
        self._runtime_component_requirements[name] = required

    def snapshot_runtime_components(self) -> dict[str, ComponentRuntimeStatus]:
        """Collect a best-effort snapshot of all registered components."""
        snapshot: dict[str, ComponentRuntimeStatus] = {}
        for name, provider in self._runtime_components.items():
            try:
                snapshot[name] = provider()
            except Exception as exc:
                snapshot[name] = ComponentRuntimeStatus(
                    component=name,
                    enabled=True,
                    status=ComponentStatus.FAILED,
                    selected_backend="unknown",
                    last_error=str(exc).splitlines()[0][:200],
                )
        return snapshot

    def runtime_component_requirements(self) -> dict[str, bool]:
        """Return a copy of registered component requirement flags."""
        return dict(self._runtime_component_requirements)
