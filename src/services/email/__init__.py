"""Email service."""

from .client import EmailClient
from .config import EmailAccountConfig, EmailConfig
from .exceptions import EmailClientError, EmailPermissionError

__all__ = [
    "EmailAccountConfig",
    "EmailClient",
    "EmailClientError",
    "EmailConfig",
    "EmailPermissionError",
]
