"""
Request models for the API layer.

All models use Pydantic BaseModel with frozen config for immutability.
"""
from typing import Optional

from pydantic import BaseModel, Field


class WorkerTaskRequest(BaseModel):
    """
    Request body for POST /api/v1/worker/task/stream.

    Required fields:
    - task: The user's task description.
    - tenant_id: Tenant identifier for multi-tenant isolation.

    Optional fields:
    - worker_id: Specific worker to route to (auto-matched if omitted).
    - thread_id: For multi-turn conversation context.
    - channel_type/channel_id/display_name/topic: Service-mode ingress hints.
    - metadata: Arbitrary key-value pairs for extensibility.
    """

    model_config = {"frozen": True}

    task: str = Field(..., min_length=1, description="User task description")
    tenant_id: str = Field(..., min_length=1, description="Tenant identifier")
    worker_id: Optional[str] = Field(
        default=None, description="Optional worker ID for direct routing"
    )
    thread_id: Optional[str] = Field(
        default=None, description="Thread ID for multi-turn context"
    )
    channel_type: Optional[str] = Field(
        default=None, description="Optional inbound channel type",
    )
    channel_id: Optional[str] = Field(
        default=None, description="Optional inbound channel identifier",
    )
    display_name: Optional[str] = Field(
        default=None, description="Optional display name from inbound channel",
    )
    topic: Optional[str] = Field(
        default=None, description="Optional service-topic hint",
    )
    metadata: dict[str, str] = Field(
        default_factory=dict, description="Optional metadata key-value pairs"
    )


class WebhookIngestRequest(BaseModel):
    """Payload for generic webhook ingress."""

    model_config = {"frozen": True}

    event_type: str = Field(..., min_length=1, description="External event type")
    data: dict[str, object] = Field(default_factory=dict, description="Webhook payload")
    dedupe_key: Optional[str] = Field(default=None, description="Optional stable dedupe key")
    cognition_route: Optional[str] = Field(default=None, description="Optional route override")
    priority: int = Field(default=20, description="Priority hint")
