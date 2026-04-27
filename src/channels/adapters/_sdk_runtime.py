"""Helpers for optional IM SDK integrations."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, is_dataclass
from datetime import timedelta
from datetime import datetime, timezone
import importlib
import inspect
import math
import random
import time
from concurrent.futures import Future
from types import ModuleType
from typing import Any, Iterable

from src.common.settings import get_settings


def optional_import(module_name: str) -> ModuleType | None:
    """Best-effort import for optional third-party SDKs."""
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


def is_task_running(task: asyncio.Task[Any] | None) -> bool:
    """Return whether an asyncio task is still alive."""
    return task is not None and not task.done()


async def cancel_task(task: asyncio.Task[Any] | None) -> None:
    """Cancel a task and suppress the expected cancellation error."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def call_maybe_async(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Call sync or async SDK hooks through one interface."""
    if not callable(func):
        return None
    result = func(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def build_variant_candidates(*variants: tuple[tuple[Any, ...], dict[str, Any]]) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Keep call-variant declarations readable at call sites."""
    return list(variants)


def call_with_variants(
    func: Any,
    variants: Iterable[tuple[tuple[Any, ...], dict[str, Any]]],
) -> Any:
    """Call a function using the first signature-compatible variant."""
    if not callable(func):
        raise TypeError(f"{func!r} is not callable")

    signature: inspect.Signature | None
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        signature = None

    last_error: Exception | None = None
    variant_list = list(variants)
    for args, kwargs in variant_list:
        if signature is not None:
            try:
                signature.bind_partial(*args, **kwargs)
            except TypeError:
                continue
        try:
            return func(*args, **kwargs)
        except TypeError as exc:
            last_error = exc
            if signature is not None:
                continue
    if last_error is not None:
        raise last_error
    raise TypeError(f"No compatible call variant found for {func!r}")


def object_to_dict(value: Any) -> Any:
    """Recursively convert SDK model objects into plain python structures."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): object_to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [object_to_dict(item) for item in value]
    if hasattr(value, "_asdict"):
        return object_to_dict(value._asdict())
    if is_dataclass(value):
        return object_to_dict(vars(value))
    if hasattr(value, "__dict__"):
        return {
            str(key): object_to_dict(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def submit_coroutine(
    loop: asyncio.AbstractEventLoop | None,
    coroutine: Any,
) -> Future[Any] | asyncio.Task[Any] | None:
    """Schedule a coroutine from the adapter loop or another thread."""
    if loop is None:
        return None
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop is loop:
        return loop.create_task(coroutine)
    return asyncio.run_coroutine_threadsafe(coroutine, loop)


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ReconnectPolicy:
    """Reconnect/backoff/circuit-breaker policy."""

    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    max_retry_exponent: int = 10
    jitter_ratio: float = 0.2
    circuit_failure_threshold: int = 3
    circuit_recovery_timeout_seconds: float = 60.0


class ReconnectController:
    """Stateful reconnect controller with exponential backoff and circuit breaker."""

    def __init__(self, policy: ReconnectPolicy) -> None:
        self._policy = policy
        self._breaker_state = "closed"
        self._consecutive_failures = 0
        self._reconnect_attempts = 0
        self._last_error = ""
        self._current_backoff_seconds = 0.0
        self._next_retry_at = ""
        self._circuit_open_until = ""
        self._circuit_open_until_monotonic = 0.0

    @property
    def breaker_state(self) -> str:
        return self._breaker_state

    def reset(self) -> None:
        self._breaker_state = "closed"
        self._consecutive_failures = 0
        self._reconnect_attempts = 0
        self._last_error = ""
        self._current_backoff_seconds = 0.0
        self._next_retry_at = ""
        self._circuit_open_until = ""
        self._circuit_open_until_monotonic = 0.0

    def before_attempt(self) -> float:
        """Return how many seconds to wait before the next connection attempt."""
        if self._breaker_state != "open":
            return 0.0
        remaining = self._circuit_open_until_monotonic - time.monotonic()
        if remaining > 0:
            return remaining
        self._breaker_state = "half_open"
        self._next_retry_at = ""
        self._current_backoff_seconds = 0.0
        return 0.0

    def register_success(self) -> None:
        self._breaker_state = "closed"
        self._consecutive_failures = 0
        self._last_error = ""
        self._current_backoff_seconds = 0.0
        self._next_retry_at = ""
        self._circuit_open_until = ""
        self._circuit_open_until_monotonic = 0.0

    def register_failure(self, exc: Exception) -> float:
        self._reconnect_attempts += 1
        self._consecutive_failures += 1
        self._last_error = f"{type(exc).__name__}: {exc}"

        if self._breaker_state == "half_open" or (
            self._consecutive_failures >= self._policy.circuit_failure_threshold
        ):
            delay = _apply_jitter(
                max(self._policy.circuit_recovery_timeout_seconds, 0.0),
                self._policy.jitter_ratio,
            )
            self._breaker_state = "open"
            self._current_backoff_seconds = delay
            self._circuit_open_until_monotonic = time.monotonic() + delay
            self._circuit_open_until = _iso_after(delay)
            self._next_retry_at = self._circuit_open_until
            return delay

        exponent = min(
            max(self._consecutive_failures - 1, 0),
            max(self._policy.max_retry_exponent - 1, 0),
        )
        delay = _compute_backoff(
            exponent,
            self._policy.backoff_base_seconds,
            self._policy.backoff_max_seconds,
        )
        delay = _apply_jitter(delay, self._policy.jitter_ratio)
        self._breaker_state = "closed"
        self._current_backoff_seconds = delay
        self._next_retry_at = _iso_after(delay)
        return delay

    def snapshot(self) -> dict[str, Any]:
        return {
            "breaker_state": self._breaker_state,
            "consecutive_failures": self._consecutive_failures,
            "reconnect_attempts": self._reconnect_attempts,
            "current_backoff_seconds": self._current_backoff_seconds,
            "next_retry_at": self._next_retry_at,
            "circuit_open_until": self._circuit_open_until,
            "last_error": self._last_error,
        }


def build_reconnect_controller(features: dict[str, Any]) -> ReconnectController:
    """Build reconnect controller from settings plus optional binding overrides."""
    settings = get_settings()
    base_delay = _as_positive_float(
        features.get("reconnect_backoff_base_seconds", features.get("reconnect_base_delay")),
        default=1.0,
    )
    max_delay = _as_positive_float(
        features.get("reconnect_backoff_max_seconds", features.get("reconnect_max_delay")),
        default=max(5.0, float(settings.im_channel_reconnect_interval)),
    )
    max_retry_exponent = _as_positive_int(
        features.get("reconnect_max_retries"),
        default=max(1, int(settings.im_channel_reconnect_max_retries)),
    )
    jitter_ratio = _as_ratio(
        features.get("reconnect_jitter_ratio"),
        default=max(0.0, float(settings.im_channel_reconnect_jitter_ratio)),
    )
    failure_threshold = _as_positive_int(
        features.get("circuit_failure_threshold"),
        default=max(1, int(settings.circuit_failure_threshold)),
    )
    recovery_timeout = _as_positive_float(
        features.get("circuit_recovery_timeout_seconds", features.get("circuit_recovery_timeout")),
        default=max(1.0, float(settings.circuit_recovery_timeout)),
    )
    return ReconnectController(ReconnectPolicy(
        backoff_base_seconds=base_delay,
        backoff_max_seconds=max(max_delay, base_delay),
        max_retry_exponent=max_retry_exponent,
        jitter_ratio=jitter_ratio,
        circuit_failure_threshold=failure_threshold,
        circuit_recovery_timeout_seconds=recovery_timeout,
    ))


def _compute_backoff(attempt: int, base: float, max_delay: float) -> float:
    delay = base * (2 ** max(attempt, 0))
    return min(delay, max_delay)


def _apply_jitter(delay: float, jitter_ratio: float) -> float:
    if delay <= 0 or jitter_ratio <= 0:
        return delay
    factor = random.uniform(max(0.0, 1.0 - jitter_ratio), 1.0 + jitter_ratio)
    return max(delay * factor, 0.0)


def _iso_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(seconds, 0.0))).isoformat()


def _as_positive_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed <= 0:
        return default
    return parsed


def _as_positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _as_ratio(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed < 0:
        return default
    return min(parsed, 1.0)
