"""Worker-scoped platform client factory."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.common.worker_channel_credentials import EmailCredential, SlackCredential

from .worker_channel_credential_loader import WorkerChannelCredentialLoader

if TYPE_CHECKING:
    from src.services.dingtalk import DingTalkClient
    from src.services.email import EmailClient
    from src.services.feishu import FeishuClient
    from src.services.slack import SlackClient
    from src.services.wecom import WeComClient

    PlatformClient = FeishuClient | WeComClient | DingTalkClient | EmailClient | SlackClient
else:
    PlatformClient = Any


class WorkerPlatformClientFactory:
    """Construct one isolated client per tenant/worker/channel triple."""

    def __init__(
        self,
        credential_loader: WorkerChannelCredentialLoader,
    ) -> None:
        self._credential_loader = credential_loader
        self._cache: dict[tuple[str, str, str], PlatformClient | None] = {}

    def get_client(
        self,
        tenant_id: str,
        worker_id: str,
        channel_type: str,
    ) -> PlatformClient | None:
        normalized_channel = str(channel_type or "").strip().lower()
        cache_key = (tenant_id, worker_id, normalized_channel)
        if cache_key in self._cache:
            return self._cache[cache_key]

        credentials = self._credential_loader.load(tenant_id, worker_id)
        client: PlatformClient | None
        if normalized_channel == "feishu" and credentials.feishu is not None:
            from src.services.feishu import FeishuAuth, FeishuClient, FeishuConfig

            config = FeishuConfig(
                app_id=credentials.feishu.app_id,
                app_secret=credentials.feishu.app_secret,
            )
            auth = FeishuAuth(config)
            client = FeishuClient(config, auth_provider=auth)
        elif normalized_channel == "wecom" and credentials.wecom is not None:
            from src.services.wecom import WeComAuth, WeComClient, WeComConfig

            config = WeComConfig(
                corpid=credentials.wecom.corpid,
                corpsecret=credentials.wecom.corpsecret,
                agent_id=credentials.wecom.agent_id,
            )
            auth = WeComAuth(config)
            client = WeComClient(config, auth_provider=auth)
        elif normalized_channel == "dingtalk" and credentials.dingtalk is not None:
            from src.services.dingtalk import (
                DingTalkAuth,
                DingTalkClient,
                DingTalkConfig,
            )

            config = DingTalkConfig(
                app_key=credentials.dingtalk.app_key,
                app_secret=credentials.dingtalk.app_secret,
                robot_code=credentials.dingtalk.robot_code,
            )
            auth = DingTalkAuth(config)
            client = DingTalkClient(config, auth_provider=auth)
        elif normalized_channel == "email" and credentials.email is not None:
            from src.services.email import EmailClient

            client = EmailClient(_build_email_config(credentials.email))
        elif normalized_channel == "slack" and credentials.slack is not None:
            from src.services.slack import SlackAuth, SlackClient

            config = _build_slack_config(credentials.slack)
            client = SlackClient(config, auth_provider=SlackAuth(config))
        else:
            client = None

        self._cache[cache_key] = client
        return client

    def invalidate(
        self,
        tenant_id: str | None = None,
        worker_id: str | None = None,
    ) -> None:
        if tenant_id is None and worker_id is None:
            self._cache.clear()
            return
        for key in tuple(self._cache):
            if tenant_id is not None and key[0] != tenant_id:
                continue
            if worker_id is not None and key[1] != worker_id:
                continue
            self._cache.pop(key, None)


def _build_email_config(credential: EmailCredential) -> EmailConfig:
    from src.services.email import EmailAccountConfig, EmailConfig

    return EmailConfig(
        worker_mailbox=EmailAccountConfig(
            address=credential.worker_address,
            username=credential.worker_username,
            password=credential.worker_password,
            imap_host=credential.worker_imap_host,
            imap_port=credential.worker_imap_port,
            smtp_host=credential.worker_smtp_host,
            smtp_port=credential.worker_smtp_port,
        ),
        owner_mailbox=EmailAccountConfig(
            address=credential.owner_address,
            username=credential.owner_username,
            password=credential.owner_password,
            imap_host=credential.owner_imap_host,
            imap_port=credential.owner_imap_port,
            smtp_host=credential.owner_smtp_host,
            smtp_port=credential.owner_smtp_port,
        ),
    )


def _build_slack_config(credential: SlackCredential) -> SlackConfig:
    from src.services.slack import SlackConfig

    return SlackConfig(
        bot_token=credential.bot_token,
        app_token=credential.app_token,
        signing_secret=credential.signing_secret,
        team_id=credential.team_id,
    )
