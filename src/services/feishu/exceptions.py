"""Feishu service exceptions."""

from src.services._http_base import BaseAPIError


class FeishuAPIError(BaseAPIError):
    """Raised when Feishu API returns an application-level error."""

