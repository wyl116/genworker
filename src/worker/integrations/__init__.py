"""
External information source integrations.

Provides:
- ContentParser: LLM-assisted content parsing to extract goal info
- GoalGenerator: convert parsed info to Goal objects
- SyncManager: bidirectional sync between local goals and external sources
- worker-scoped gateways and business integration glue

Outbound channel adapters now live under ``src.channels.outbound``. This
package root exposes integration-domain APIs only; compatibility exports for
historical outbound adapter imports remain in leaf modules.
"""

from .content_parser import ContentParser
from .domain_models import MonitorConfig, ParsedGoalInfo, SyncRecord
from .goal_generator import generate_goal_from_parsed, update_goal_from_external
from .sync_manager import SyncManager
from .worker_scoped_channel_gateway import WorkerScopedChannelGateway

__all__ = [
    "ContentParser",
    "MonitorConfig",
    "ParsedGoalInfo",
    "SyncManager",
    "SyncRecord",
    "WorkerScopedChannelGateway",
    "generate_goal_from_parsed",
    "update_goal_from_external",
]
