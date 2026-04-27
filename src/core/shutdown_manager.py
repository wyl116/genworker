"""
Unified resource shutdown manager.

Coordinates graceful shutdown of all resources, ensuring
proper cleanup in priority order.
"""
import asyncio
import logging
import signal
import os
import threading
from typing import List, Callable, Optional, Set
from enum import Enum
from src.common.logger import get_logger

logger = get_logger()


class ShutdownPriority(Enum):
    """Shutdown priority levels."""
    HIGH = 1      # Service deregistration, stop accepting requests
    MEDIUM = 2    # Close connections, cleanup resources
    LOW = 3       # Flush logs, clear caches


class ShutdownTask:
    """A shutdown task with priority and timeout."""

    def __init__(
        self,
        name: str,
        handler: Callable,
        priority: ShutdownPriority = ShutdownPriority.MEDIUM,
        timeout: int = 10,
        is_async: bool = True,
    ):
        self.name = name
        self.handler = handler
        self.priority = priority
        self.timeout = timeout
        self.is_async = is_async


class ShutdownManager:
    """
    Unified shutdown manager.

    - Shuts down resources in priority order
    - Timeout protection per task
    - Supports both sync and async tasks
    - Signal handling for graceful shutdown
    """

    def __init__(
        self, total_timeout: int = 30, force_exit_timeout: int = 5,
    ):
        self.total_timeout = total_timeout
        self.force_exit_timeout = force_exit_timeout
        self.tasks: List[ShutdownTask] = []
        self._is_shutting_down = False
        self._shutdown_complete = False
        self._signal_received = False

    @property
    def is_shutting_down(self) -> bool:
        """Check if shutdown is in progress."""
        return self._is_shutting_down

    @property
    def is_shutdown_complete(self) -> bool:
        """Check if shutdown has completed."""
        return self._shutdown_complete

    def register(
        self,
        name: str,
        handler: Callable,
        priority: ShutdownPriority = ShutdownPriority.MEDIUM,
        timeout: int = 10,
        is_async: bool = True,
    ):
        """Register a shutdown task."""
        task = ShutdownTask(name, handler, priority, timeout, is_async)
        self.tasks.append(task)
        logger.debug(
            f"Registered shutdown task: {name} "
            f"(priority: {priority.name}, timeout: {timeout}s)"
        )

    def setup_signal_handlers(self):
        """Set up signal handlers. Call only from main thread."""
        if threading.current_thread() is not threading.main_thread():
            logger.warning("Signal handlers can only be set in main thread")
            return

        def signal_handler(signum, frame):
            sig_name = signal.Signals(signum).name
            if self._signal_received:
                print(f"\nReceived second {sig_name}, forcing exit...")
                os._exit(1)
            self._signal_received = True
            print(f"\nReceived {sig_name}, shutting down gracefully...")
            if not self._is_shutting_down:
                def force_exit():
                    if not self._shutdown_complete:
                        print(f"\nShutdown timeout, forcing exit...")
                        os._exit(1)
                timer = threading.Timer(
                    self.total_timeout + self.force_exit_timeout,
                    force_exit,
                )
                timer.daemon = True
                timer.start()
            self._is_shutting_down = True

        try:
            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)
        except Exception as e:
            logger.warning(f"Failed to set signal handlers: {e}")

    async def shutdown(self) -> bool:
        """
        Execute graceful shutdown.

        Returns:
            True if all tasks completed successfully.
        """
        if self._is_shutting_down:
            logger.warning("Shutdown already in progress, skipping")
            return False

        self._is_shutting_down = True
        logger.info("=" * 60)
        logger.info("Starting graceful shutdown...")
        logger.info(f"Tasks to shutdown: {len(self.tasks)}")
        logger.info("=" * 60)

        sorted_tasks = sorted(self.tasks, key=lambda t: t.priority.value)
        success_count = 0
        failed_count = 0

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("No running event loop")
            self._shutdown_complete = True
            return False

        start_time = loop.time()

        for i, task in enumerate(sorted_tasks, 1):
            elapsed = loop.time() - start_time
            if elapsed >= self.total_timeout:
                remaining = len(sorted_tasks) - i + 1
                logger.warning(
                    f"Total timeout reached, skipping {remaining} tasks"
                )
                break

            logger.info(
                f"[{i}/{len(sorted_tasks)}] Shutting down: {task.name}"
            )

            try:
                if task.is_async:
                    await asyncio.wait_for(
                        task.handler(), timeout=task.timeout,
                    )
                else:
                    await asyncio.wait_for(
                        loop.run_in_executor(None, task.handler),
                        timeout=task.timeout,
                    )
                logger.info(f"  {task.name} shutdown OK")
                success_count += 1
            except asyncio.TimeoutError:
                logger.error(f"  {task.name} timed out ({task.timeout}s)")
                failed_count += 1
            except asyncio.CancelledError:
                logger.warning(f"  {task.name} cancelled")
                failed_count += 1
            except Exception as e:
                logger.error(f"  {task.name} failed: {e}")
                failed_count += 1

        total_elapsed = loop.time() - start_time
        logger.info("=" * 60)
        logger.info(
            f"Shutdown complete | elapsed={total_elapsed:.2f}s | "
            f"success={success_count} | failed={failed_count}"
        )
        logger.info("=" * 60)

        self._is_shutting_down = False
        self._shutdown_complete = True
        return failed_count == 0


_shutdown_manager: Optional[ShutdownManager] = None


def get_shutdown_manager() -> ShutdownManager:
    """Get the global shutdown manager singleton."""
    global _shutdown_manager
    if _shutdown_manager is None:
        _shutdown_manager = ShutdownManager()
    return _shutdown_manager


def reset_shutdown_manager():
    """Reset the shutdown manager (for testing)."""
    global _shutdown_manager
    _shutdown_manager = None
