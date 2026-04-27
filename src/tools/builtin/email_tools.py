"""Builtin email MCP tools backed by EmailClient."""
from __future__ import annotations

import base64
from typing import Any

from src.services.email import EmailClient
from src.tools.runtime_scope import ExecutionScopeProvider

from ..mcp.tool import Tool
from ..mcp.types import MCPCategory, RiskLevel, ToolType


def create_email_search_tool(email_client: EmailClient) -> Tool:
    """Create the email search tool."""

    async def handler(
        query: str = "",
        account: str = "worker_mailbox",
        folder: str = "INBOX",
    ) -> dict[str, Any]:
        emails = await email_client.search(
            query=query,
            account=account,
            folder=folder,
        )
        return {"emails": emails}

    return Tool(
        name="email_search",
        description=(
            "Search emails from a mailbox folder. Returns email metadata "
            "including sender, subject, body preview, thread headers and attachments."
        ),
        handler=handler,
        parameters={
            "query": {
                "type": "string",
                "description": "Optional full-text query matched against subject and body.",
            },
            "account": {
                "type": "string",
                "description": "Mailbox account: worker_mailbox, owner_mailbox, or proxy_send.",
            },
            "folder": {
                "type": "string",
                "description": "Mailbox folder name, default INBOX.",
            },
        },
        tool_type=ToolType.READ,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        tags=frozenset({"email", "search", "mailbox"}),
    )


def create_worker_scoped_email_search_tool(
    platform_client_factory: Any,
    execution_scope_provider: ExecutionScopeProvider,
) -> Tool:
    """Create a worker-aware email search tool."""

    async def handler(
        query: str = "",
        account: str = "worker_mailbox",
        folder: str = "INBOX",
    ) -> dict[str, Any]:
        email_client = _require_worker_email_client(
            platform_client_factory,
            execution_scope_provider,
            tool_name="email_search",
        )
        emails = await email_client.search(
            query=query,
            account=account,
            folder=folder,
        )
        return {"emails": emails}

    return Tool(
        name="email_search",
        description=(
            "Search emails from the current worker mailbox folder. Returns email metadata "
            "including sender, subject, body preview, thread headers and attachments."
        ),
        handler=handler,
        parameters={
            "query": {
                "type": "string",
                "description": "Optional full-text query matched against subject and body.",
            },
            "account": {
                "type": "string",
                "description": "Mailbox account: worker_mailbox, owner_mailbox, or proxy_send.",
            },
            "folder": {
                "type": "string",
                "description": "Mailbox folder name, default INBOX.",
            },
        },
        tool_type=ToolType.READ,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        tags=frozenset({"email", "search", "mailbox"}),
    )


def create_email_send_tool(email_client: EmailClient) -> Tool:
    """Create the email send tool."""

    async def handler(
        to: str,
        subject: str,
        body: str,
        reply_to: str = "",
        send_mode: str = "worker_mailbox",
        cc: str = "",
        html_body: str = "",
    ) -> dict[str, Any]:
        recipients = _split_addresses(to)
        cc_list = _split_addresses(cc)
        message_id = await email_client.send(
            to=recipients,
            subject=subject,
            body=body,
            reply_to=reply_to or None,
            send_mode=send_mode,
            cc=cc_list,
            html_body=html_body,
        )
        return {"status": "sent", "message_id": message_id}

    return Tool(
        name="email_send",
        description=(
            "Send an email through the configured mailbox. Supports reply threading, "
            "CC recipients and optional HTML body."
        ),
        handler=handler,
        parameters={
            "to": {
                "type": "string",
                "description": "Comma-separated recipient email addresses.",
            },
            "subject": {
                "type": "string",
                "description": "Email subject line.",
            },
            "body": {
                "type": "string",
                "description": "Plain-text email body.",
            },
            "reply_to": {
                "type": "string",
                "description": "Optional Message-ID to thread this email as a reply.",
            },
            "send_mode": {
                "type": "string",
                "description": "Mailbox send mode: worker_mailbox, owner_mailbox, or proxy_send.",
            },
            "cc": {
                "type": "string",
                "description": "Optional comma-separated CC recipient addresses.",
            },
            "html_body": {
                "type": "string",
                "description": "Optional HTML alternative body.",
            },
        },
        required_params=("to", "subject", "body"),
        tool_type=ToolType.WRITE,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.MEDIUM,
        tags=frozenset({"email", "send", "mailbox"}),
    )


def create_worker_scoped_email_send_tool(
    platform_client_factory: Any,
    execution_scope_provider: ExecutionScopeProvider,
) -> Tool:
    """Create a worker-aware email send tool."""

    async def handler(
        to: str,
        subject: str,
        body: str,
        reply_to: str = "",
        send_mode: str = "worker_mailbox",
        cc: str = "",
        html_body: str = "",
    ) -> dict[str, Any]:
        email_client = _require_worker_email_client(
            platform_client_factory,
            execution_scope_provider,
            tool_name="email_send",
        )
        recipients = _split_addresses(to)
        cc_list = _split_addresses(cc)
        message_id = await email_client.send(
            to=recipients,
            subject=subject,
            body=body,
            reply_to=reply_to or None,
            send_mode=send_mode,
            cc=cc_list,
            html_body=html_body,
        )
        return {"status": "sent", "message_id": message_id}

    return Tool(
        name="email_send",
        description=(
            "Send an email through the current worker mailbox. Supports reply threading, "
            "CC recipients and optional HTML body."
        ),
        handler=handler,
        parameters={
            "to": {
                "type": "string",
                "description": "Comma-separated recipient email addresses.",
            },
            "subject": {
                "type": "string",
                "description": "Email subject line.",
            },
            "body": {
                "type": "string",
                "description": "Plain-text email body.",
            },
            "reply_to": {
                "type": "string",
                "description": "Optional Message-ID to thread this email as a reply.",
            },
            "send_mode": {
                "type": "string",
                "description": "Mailbox send mode: worker_mailbox, owner_mailbox, or proxy_send.",
            },
            "cc": {
                "type": "string",
                "description": "Optional comma-separated CC recipient addresses.",
            },
            "html_body": {
                "type": "string",
                "description": "Optional HTML alternative body.",
            },
        },
        required_params=("to", "subject", "body"),
        tool_type=ToolType.WRITE,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.MEDIUM,
        tags=frozenset({"email", "send", "mailbox"}),
    )


def create_email_download_attachment_tool(email_client: EmailClient) -> Tool:
    """Create the attachment download tool."""

    async def handler(
        message_id: str,
        content_id: str,
        account: str = "worker_mailbox",
        folder: str = "INBOX",
    ) -> dict[str, Any]:
        payload = await email_client.download_attachment(
            message_id=message_id,
            content_id=content_id,
            account=account,
            folder=folder,
        )
        return {
            "message_id": message_id,
            "content_id": content_id,
            "content_base64": base64.b64encode(payload).decode("ascii"),
            "size": len(payload),
        }

    return Tool(
        name="email_download_attachment",
        description=(
            "Download one email attachment by message_id and content_id or filename. "
            "Returns base64-encoded bytes."
        ),
        handler=handler,
        parameters={
            "message_id": {
                "type": "string",
                "description": "The email Message-ID that owns the attachment.",
            },
            "content_id": {
                "type": "string",
                "description": "Attachment content ID or filename.",
            },
            "account": {
                "type": "string",
                "description": "Mailbox account: worker_mailbox, owner_mailbox, or proxy_send.",
            },
            "folder": {
                "type": "string",
                "description": "Mailbox folder name, default INBOX.",
            },
        },
        required_params=("message_id", "content_id"),
        tool_type=ToolType.READ,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        tags=frozenset({"email", "attachment", "download"}),
    )


def create_worker_scoped_email_download_attachment_tool(
    platform_client_factory: Any,
    execution_scope_provider: ExecutionScopeProvider,
) -> Tool:
    """Create a worker-aware attachment download tool."""

    async def handler(
        message_id: str,
        content_id: str,
        account: str = "worker_mailbox",
        folder: str = "INBOX",
    ) -> dict[str, Any]:
        email_client = _require_worker_email_client(
            platform_client_factory,
            execution_scope_provider,
            tool_name="email_download_attachment",
        )
        payload = await email_client.download_attachment(
            message_id=message_id,
            content_id=content_id,
            account=account,
            folder=folder,
        )
        return {
            "message_id": message_id,
            "content_id": content_id,
            "content_base64": base64.b64encode(payload).decode("ascii"),
            "size": len(payload),
        }

    return Tool(
        name="email_download_attachment",
        description=(
            "Download one email attachment by message_id and content_id or filename. "
            "Returns base64-encoded bytes."
        ),
        handler=handler,
        parameters={
            "message_id": {
                "type": "string",
                "description": "The email Message-ID that owns the attachment.",
            },
            "content_id": {
                "type": "string",
                "description": "Attachment content ID or filename.",
            },
            "account": {
                "type": "string",
                "description": "Mailbox account: worker_mailbox, owner_mailbox, or proxy_send.",
            },
            "folder": {
                "type": "string",
                "description": "Mailbox folder name, default INBOX.",
            },
        },
        required_params=("message_id", "content_id"),
        tool_type=ToolType.READ,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        tags=frozenset({"email", "attachment", "download"}),
    )


def _split_addresses(value: str) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in str(value or "").split(",")
        if item.strip()
    )


def _require_worker_email_client(
    platform_client_factory: Any,
    execution_scope_provider: ExecutionScopeProvider,
    *,
    tool_name: str,
) -> EmailClient:
    scope = execution_scope_provider.current()
    if scope is None:
        raise RuntimeError(f"{tool_name} requires execution scope")
    client = platform_client_factory.get_client(
        scope.tenant_id,
        scope.worker_id,
        "email",
    )
    if client is None:
        raise RuntimeError(
            f"{tool_name} requires email credentials for worker "
            f"{scope.tenant_id}/{scope.worker_id}"
        )
    if not isinstance(client, EmailClient):
        raise RuntimeError(
            f"{tool_name} resolved a non-email client for worker "
            f"{scope.tenant_id}/{scope.worker_id}"
        )
    return client
