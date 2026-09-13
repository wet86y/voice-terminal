"""Session-scoped Shadow Agent execution-plane primitives."""

from .contracts import (
    ShadowContextFrame, ShadowContextSnapshot, ShadowContextUpdate, ShadowEvent,
    ShadowVisibleStatus, ToolEffect, parse_shadow_wake, request_shadow_action_definition,
)
from .component import ShadowAgentComponent, ShadowAgentSession
from .context import ShadowContextProjector
from .manager import ShadowRuntime, ShadowStatusObserver
from .provider import DeepSeekResponsesShadowProvider, ShadowProvider, ShadowProviderResponse
from .tools import ShadowToolExecutor
from .tools import ShadowToolDomain

__all__ = [
    "DeepSeekResponsesShadowProvider",
    "ShadowAgentComponent",
    "ShadowAgentSession",
    "ShadowProvider",
    "ShadowProviderResponse",
    "ShadowContextFrame",
    "ShadowContextSnapshot",
    "ShadowContextUpdate",
    "ShadowContextProjector",
    "ShadowEvent",
    "ShadowRuntime",
    "ShadowStatusObserver",
    "ShadowVisibleStatus",
    "ShadowToolExecutor",
    "ShadowToolDomain",
    "ToolEffect",
    "parse_shadow_wake",
    "request_shadow_action_definition",
]
