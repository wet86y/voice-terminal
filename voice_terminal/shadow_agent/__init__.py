"""Isolated, non-production shadow task agent primitives.

This package intentionally has no dependency on the live voice scheduler.  A
future integration may adapt its command and event interfaces at the runtime
boundary without making the shadow harness part of today's speech path.
"""

from .contracts import ShadowTaskCommand, ShadowTaskEvent, TaskReceipt, task_action_definition
from .component import ShadowAgentComponent, ShadowAgentSession
from .manager import ShadowTaskManager
from .provider import DeepSeekResponsesShadowProvider, ShadowProvider, ShadowProviderResponse
from .tools import ShadowToolExecutor
from .tools import ShadowToolDomain

__all__ = [
    "DeepSeekResponsesShadowProvider",
    "ShadowAgentComponent",
    "ShadowAgentSession",
    "ShadowProvider",
    "ShadowProviderResponse",
    "ShadowTaskCommand",
    "ShadowTaskEvent",
    "ShadowTaskManager",
    "ShadowToolExecutor",
    "ShadowToolDomain",
    "TaskReceipt",
    "task_action_definition",
]
