"""WeCom service exceptions."""

from src.services._http_base import BaseAPIError


class WeComAPIError(BaseAPIError):
    """Raised when WeCom API returns an application-level error."""

