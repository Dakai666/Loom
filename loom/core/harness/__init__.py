from .middleware import (
    Middleware, MiddlewarePipeline, ToolCall, ToolResult,
    LifecycleMiddleware, LifecycleGateMiddleware,
)
from .permissions import TrustLevel, PermissionContext
from .registry import ToolDefinition, ToolRegistry
from .validation import SchemaValidationMiddleware
from .lifecycle import (
    ActionState, ActionIntent, ActionRecord,
    ExecutionEnvelope, StateTransition,
    LifecycleContext, LIFECYCLE_CTX_KEY,
)
from .scope import (
    ScopeDescriptor, ScopeGrant, ScopeRequirement, ScopeRequest,
    ScopeDiff, DiffReason, PermissionVerdict,
    ScopeMatcher, PathMatcher, NetworkMatcher, ExecMatcher,
    AgentMatcher, MutationMatcher,
    covers, compute_diff, get_matcher,
)

__all__ = [
    "Middleware", "MiddlewarePipeline", "ToolCall", "ToolResult",
    "LifecycleMiddleware", "LifecycleGateMiddleware",
    "TrustLevel", "PermissionContext",
    "ToolDefinition", "ToolRegistry",
    "SchemaValidationMiddleware",
    "ActionState", "ActionIntent", "ActionRecord",
    "ExecutionEnvelope", "StateTransition",
    "LifecycleContext", "LIFECYCLE_CTX_KEY",
    # Scope-aware permission (Issue #45)
    "ScopeDescriptor", "ScopeGrant", "ScopeRequirement", "ScopeRequest",
    "ScopeDiff", "DiffReason", "PermissionVerdict",
    "ScopeMatcher", "PathMatcher", "NetworkMatcher", "ExecMatcher",
    "AgentMatcher", "MutationMatcher",
    "covers", "compute_diff", "get_matcher",
]
