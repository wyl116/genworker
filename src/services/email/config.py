"""Email client configuration."""
from dataclasses import dataclass


@dataclass(frozen=True)
class EmailAccountConfig:
    name: str = ""
    address: str = ""
    username: str = ""
    password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465
    use_ssl: bool = True


@dataclass(frozen=True)
class EmailConfig:
    worker_mailbox: EmailAccountConfig = EmailAccountConfig()
    owner_mailbox: EmailAccountConfig = EmailAccountConfig()

