# edition: baseline
from __future__ import annotations

import pytest

from src.api.im_config_validators import (
    IMConfigValidationError,
    mask_credentials,
    validate_im_config_payload,
)


def test_validate_im_config_payload_normalizes_chat_ids() -> None:
    channels, credentials = validate_im_config_payload(
        channels=[{
            "type": "slack",
            "connection_mode": "socket_mode",
            "chat_ids": ["C1", " ", "C1", "C2"],
            "reply_mode": "streaming",
            "features": {},
        }],
        credentials={
            "slack": {
                "bot_token": "xoxb-demo",
                "app_token": "xapp-demo",
            }
        },
    )
    assert channels[0]["chat_ids"] == ["C1", "C2"]
    assert credentials["slack"]["bot_token"] == "xoxb-demo"


def test_validate_im_config_payload_rejects_invalid_slack_token() -> None:
    with pytest.raises(IMConfigValidationError) as exc_info:
        validate_im_config_payload(
            channels=[{
                "type": "slack",
                "connection_mode": "socket_mode",
                "chat_ids": ["C1"],
                "reply_mode": "streaming",
                "features": {},
            }],
            credentials={"slack": {"bot_token": "bad-token"}},
        )
    assert exc_info.value.details[0]["loc"] == ["credentials", "slack", "bot_token"]


def test_mask_credentials_masks_secret_fields() -> None:
    masked = mask_credentials(
        {"feishu": {"app_id": "cli_demo", "app_secret": "secret-value"}}
    )
    assert masked["feishu"]["app_id"] == "cli_demo"
    assert masked["feishu"]["app_secret"] == "secr****"
