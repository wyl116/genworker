"""
Response models for the API layer.

Used for non-streaming endpoints (health, errors).
Streaming endpoints use SSE format via event_adapter.
"""
from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """Standard error response for non-streaming endpoints."""

    model_config = {"frozen": True}

    error: str = Field(..., description="Error message")
    code: str = Field(default="INTERNAL_ERROR", description="Error code")
    detail: str = Field(default="", description="Detailed error information")


class HealthResponse(BaseModel):
    """Health check response."""

    model_config = {"frozen": True}

    status: str = Field(default="healthy", description="Service status")
    service: str = Field(default="", description="Service name")
    version: str = Field(default="", description="Service version")
