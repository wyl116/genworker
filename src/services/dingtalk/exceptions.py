"""DingTalk service exceptions."""

from src.services._http_base import BaseAPIError


class DingTalkAPIError(BaseAPIError):
    """Raised when DingTalk API returns an application-level error."""

