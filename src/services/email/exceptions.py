"""Email client exceptions."""


class EmailClientError(Exception):
    """Base email client error."""


class EmailPermissionError(EmailClientError):
    """Raised when a proxy mailbox accesses restricted folders."""

