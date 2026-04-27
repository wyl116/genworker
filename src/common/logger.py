"""
Logging utility module - unified log management.

Supports:
- Daily rotation
- Log level separation (INFO/DEBUG/ERROR)
- Batch writing and buffering
- Console and file dual output
- Thread safety
- Configurable log format
"""
import os
import sys
import logging
import logging.handlers
from pathlib import Path
from typing import Optional
from datetime import datetime


def _convert_log_format(format_str: str) -> str:
    """Convert Java-style log format to Python logging format."""
    if "{yyyy-MM-dd HH:mm:ss.SSS}" in format_str:
        format_str = format_str.replace(
            "{yyyy-MM-dd HH:mm:ss.SSS}", "%(asctime)s"
        )
    elif "{yyyy-MM-dd HH:mm:ss}" in format_str:
        format_str = format_str.replace("{yyyy-MM-dd HH:mm:ss}", "%(asctime)s")

    replacements = {
        "%level": "%(levelname)s",
        "%msg": "%(message)s",
        "%name": "%(name)s",
        "%thread": "%(threadName)s",
        "%logger": "%(name)s",
        "%file": "%(filename)s",
        "%path": "%(pathname)s",
        "%relpath": "%(relpath)s",
        "%line": "%(lineno)d",
        "%func": "%(funcName)s",
        "%n": "",
    }

    for old, new in replacements.items():
        format_str = format_str.replace(old, new)

    return format_str


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RelativePathFormatter(logging.Formatter):
    """Formatter that supports project-relative paths."""

    def format(self, record):
        if record.pathname and record.pathname.startswith(_PROJECT_ROOT):
            record.relpath = os.path.relpath(record.pathname, _PROJECT_ROOT)
        else:
            record.relpath = record.pathname
        return super().format(record)


class MillisecondFormatter(RelativePathFormatter):
    """Formatter with millisecond and relative path support."""

    def formatTime(self, record, datefmt=None):
        if datefmt:
            s = datetime.fromtimestamp(record.created).strftime(datefmt)
        else:
            s = datetime.fromtimestamp(record.created).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        s = f"{s}.{int(record.msecs):03d}"
        return s


class LoggerManager:
    """Logger manager - singleton pattern."""

    _instance = None
    _initialized = False

    DEFAULT_CONSOLE_FORMAT = (
        "{yyyy-MM-dd HH:mm:ss} - [%level] - %relpath:%line - %msg%n"
    )
    DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not LoggerManager._initialized:
            self._loggers = {}
            self._configured = False
            self._pending_config = {}
            LoggerManager._initialized = True

    @staticmethod
    def _create_formatter(
        format_str: str,
        date_format: str = None,
    ) -> logging.Formatter:
        """Create a formatter from format string."""
        if date_format is None:
            date_format = LoggerManager.DEFAULT_DATE_FORMAT

        converted_format = _convert_log_format(format_str)

        if "{yyyy-MM-dd HH:mm:ss.SSS}" in format_str or ".SSS" in format_str:
            return MillisecondFormatter(fmt=converted_format, datefmt=date_format)
        else:
            return RelativePathFormatter(
                fmt=converted_format, datefmt=date_format
            )

    @staticmethod
    def _get_format_from_env(env_key: str, default: str = None) -> str:
        """Read log format config from environment variable."""
        if default is None:
            default = LoggerManager.DEFAULT_CONSOLE_FORMAT
        return os.environ.get(env_key, default)

    def setup_logger(
        self,
        name: str = "genworker",
        log_dir: str = "logs",
        log_level: str = "INFO",
        console_output: bool = True,
        file_output: bool = True,
        separate_levels: bool = True,
        rotation: str = "daily",
        backup_count: int = 30,
        buffer_capacity: int = 1000,
        flush_interval: float = 5.0,
        log_format: str = "{yyyy-MM-dd HH:mm:ss} - [%level] - %file - %msg%n",
        log_date_format: str = "%Y-%m-%d %H:%M:%S",
        console_format: str = "{yyyy-MM-dd HH:mm:ss} - [%level] - %file - %msg%n",
    ) -> logging.Logger:
        """Set up a logger with the given configuration."""
        self._configured = True
        self._pending_config = {
            'log_dir': log_dir,
            'log_level': log_level,
            'console_output': console_output,
            'file_output': file_output,
            'separate_levels': separate_levels,
            'rotation': rotation,
            'backup_count': backup_count,
            'buffer_capacity': buffer_capacity,
            'flush_interval': flush_interval,
            'log_format': log_format,
            'log_date_format': log_date_format,
            'console_format': console_format,
        }

        if name in self._loggers:
            return self._loggers[name]

        logger = logging.getLogger(name)
        logger.setLevel(getattr(logging, log_level.upper()))
        logger.propagate = False
        logger.handlers.clear()

        if file_output:
            log_path = Path(log_dir)
            log_path.mkdir(parents=True, exist_ok=True)

        detailed_formatter = self._create_formatter(log_format, log_date_format)
        console_formatter = self._create_formatter(console_format, log_date_format)

        if console_output:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(getattr(logging, log_level.upper()))
            console_handler.setFormatter(console_formatter)
            logger.addHandler(console_handler)

        if file_output:
            if separate_levels:
                self._add_level_handlers(
                    logger, log_dir, name, rotation,
                    backup_count, buffer_capacity,
                    flush_interval, detailed_formatter,
                )
            else:
                handler = self._create_file_handler(
                    log_dir, f"{name}.log", rotation,
                    backup_count, buffer_capacity, flush_interval,
                    detailed_formatter,
                )
                handler.setLevel(getattr(logging, log_level.upper()))
                logger.addHandler(handler)

        self._loggers[name] = logger
        return logger

    def _add_level_handlers(
        self,
        logger: logging.Logger,
        log_dir: str,
        name: str,
        rotation: str,
        backup_count: int,
        buffer_capacity: int,
        flush_interval: float,
        formatter: logging.Formatter,
    ):
        """Add level-separated handlers."""
        info_handler = self._create_file_handler(
            log_dir, f"{name}_info.log", rotation,
            backup_count, buffer_capacity, flush_interval, formatter,
        )
        info_handler.setLevel(logging.INFO)
        info_handler.addFilter(lambda record: record.levelno < logging.ERROR)
        logger.addHandler(info_handler)

        debug_handler = self._create_file_handler(
            log_dir, f"{name}_debug.log", rotation,
            backup_count, buffer_capacity, flush_interval, formatter,
        )
        debug_handler.setLevel(logging.DEBUG)
        debug_handler.addFilter(lambda record: record.levelno == logging.DEBUG)
        logger.addHandler(debug_handler)

        error_handler = self._create_file_handler(
            log_dir, f"{name}_error.log", rotation,
            backup_count, buffer_capacity, flush_interval, formatter,
        )
        error_handler.setLevel(logging.ERROR)
        logger.addHandler(error_handler)

    def _create_file_handler(
        self,
        log_dir: str,
        filename: str,
        rotation: str,
        backup_count: int,
        buffer_capacity: int,
        flush_interval: float,
        formatter: Optional[logging.Formatter] = None,
    ) -> logging.Handler:
        """Create a file handler with optional buffering."""
        log_file = os.path.join(log_dir, filename)

        if rotation == "daily":
            handler = logging.handlers.TimedRotatingFileHandler(
                filename=log_file,
                when='midnight',
                interval=1,
                backupCount=backup_count,
                encoding='utf-8',
                delay=False,
                utc=False,
            )
            handler.suffix = "%Y-%m-%d"
        else:
            handler = logging.handlers.RotatingFileHandler(
                filename=log_file,
                maxBytes=10 * 1024 * 1024,
                backupCount=backup_count,
                encoding='utf-8',
                delay=False,
            )

        if formatter:
            handler.setFormatter(formatter)

        if buffer_capacity > 0:
            buffered_handler = logging.handlers.MemoryHandler(
                capacity=buffer_capacity,
                flushLevel=logging.ERROR,
                target=handler,
                flushOnClose=True,
            )
            import threading
            import atexit

            def periodic_flush():
                import time
                while True:
                    time.sleep(flush_interval)
                    try:
                        buffered_handler.flush()
                    except Exception:
                        pass

            flush_thread = threading.Thread(target=periodic_flush, daemon=True)
            flush_thread.start()
            atexit.register(buffered_handler.flush)
            return buffered_handler
        else:
            return handler

    def get_logger(self, name: str = "genworker") -> logging.Logger:
        """Get a logger by name."""
        if name in self._loggers:
            return self._loggers[name]

        if self._configured and self._pending_config:
            return self._create_logger_with_config(name, self._pending_config)

        return self._create_temp_console_logger(name)

    def _create_temp_console_logger(self, name: str) -> logging.Logger:
        """Create a temporary console-only logger."""
        if name in self._loggers:
            return self._loggers[name]

        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.handlers.clear()

        console_format = self._get_format_from_env("LOG_CONSOLE_FORMAT")
        log_date_format = self._get_format_from_env(
            "LOG_DATE_FORMAT", self.DEFAULT_DATE_FORMAT
        )
        console_formatter = self._create_formatter(console_format, log_date_format)

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)

        return logger

    def _create_logger_with_config(
        self, name: str, config: dict
    ) -> logging.Logger:
        """Create logger using stored config."""
        return self.setup_logger(name=name, **config)

    def shutdown(self):
        """Shutdown all loggers and flush buffers."""
        for logger in self._loggers.values():
            for handler in logger.handlers:
                handler.flush()
                handler.close()
        logging.shutdown()


_logger_manager: Optional[LoggerManager] = None


def get_logger_manager() -> LoggerManager:
    """Get the logger manager singleton."""
    global _logger_manager
    if _logger_manager is None:
        _logger_manager = LoggerManager()
    return _logger_manager


def setup_logging(
    name: str = "genworker",
    log_dir: str = "logs",
    log_level: str = "INFO",
    console_output: bool = True,
    file_output: bool = True,
    separate_levels: bool = True,
    rotation: str = "daily",
    backup_count: int = 30,
    buffer_capacity: int = 1000,
    flush_interval: float = 5.0,
    log_format: str = "{yyyy-MM-dd HH:mm:ss} - [%level] - %relpath:%line - %msg%n",
    log_date_format: str = "%Y-%m-%d %H:%M:%S",
    console_format: str = "{yyyy-MM-dd HH:mm:ss} - [%level] - %relpath:%line - %msg%n",
) -> logging.Logger:
    """Initialize the logging system (convenience function)."""
    manager = get_logger_manager()
    return manager.setup_logger(
        name=name,
        log_dir=log_dir,
        log_level=log_level,
        console_output=console_output,
        file_output=file_output,
        separate_levels=separate_levels,
        rotation=rotation,
        backup_count=backup_count,
        buffer_capacity=buffer_capacity,
        flush_interval=flush_interval,
        log_format=log_format,
        log_date_format=log_date_format,
        console_format=console_format,
    )


def get_logger(name: str = "genworker") -> logging.Logger:
    """Get a logger (convenience function)."""
    manager = get_logger_manager()
    return manager.get_logger(name)


def shutdown_logging():
    """Shutdown the logging system (convenience function)."""
    manager = get_logger_manager()
    manager.shutdown()


def initialize_logging() -> logging.Logger:
    """
    Initialize logging from application settings.

    Called by the LoggingInitializer during bootstrap.
    """
    from src.common.settings import get_settings
    settings = get_settings()

    return setup_logging(
        name=settings.service_name,
        log_dir=settings.log_dir,
        log_level=settings.log_level,
        console_output=settings.log_console_output,
        file_output=settings.log_file_output,
        separate_levels=settings.log_separate_levels,
        rotation=settings.log_rotation,
        backup_count=settings.log_backup_count,
        buffer_capacity=settings.log_buffer_capacity,
        flush_interval=settings.log_flush_interval,
        log_format=settings.log_format,
        log_date_format=settings.log_date_format,
        console_format=settings.log_console_format,
    )


__all__ = [
    'LoggerManager',
    'get_logger_manager',
    'setup_logging',
    'get_logger',
    'shutdown_logging',
    'initialize_logging',
]
