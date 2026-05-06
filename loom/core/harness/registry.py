"""
Tool Registry — the single source of truth for what tools exist, their
trust level, and their JSON schema in both Anthropic and OpenAI formats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from .middleware import ToolCall, ToolResult, VerifierResult
from .permissions import ToolCapability, TrustLevel

if TYPE_CHECKING:
    from .scope import ScopeRequest


@dataclass
class ToolDefinition:
    """
    Describes one tool: its contract (name, schema, trust) and its executor.

    `executor` is an async function that receives a ToolCall and returns a
    ToolResult.  The harness calls it as the innermost step in the pipeline.

    `capabilities` is an additive set of ToolCapability flags that further
    qualify what the tool does, beyond its TrustLevel tier.  Defaults to NONE
    so existing tool definitions require no changes.

    Action Lifecycle fields (Issue #42)
    ------------------------------------
    `preconditions` — human-readable descriptions of required preconditions.
    `rollback_fn`   — async function to undo the tool's effect on failure.
    `post_validator` — async function to verify the tool's effect after execution.
    `impact_scope`  — impact scope classification for the tool.
    """
    name: str
    description: str
    trust_level: TrustLevel
    input_schema: dict[str, Any]
    executor: Callable[[ToolCall], Awaitable[ToolResult]]
    tags: list[str] = field(default_factory=list)
    capabilities: ToolCapability = field(default_factory=lambda: ToolCapability.NONE)

    # --- Action Lifecycle (Issue #42) ---
    preconditions: list[str] = field(default_factory=list)
    precondition_checks: list[Callable[[ToolCall], Awaitable[bool]]] = field(default_factory=list)
    """
    Callable gates evaluated before tool dispatch (Issue #50).

    Each check receives the ToolCall and returns True (pass) or False (fail).
    ALL checks must pass for the tool to proceed to EXECUTING.
    Failure → ABORTED with no tool call made.

    These complement ``preconditions`` (human-readable strings kept for
    documentation and audit trail).  Tools with no checks behave
    identically to the Phase 1 lifecycle.
    """
    rollback_fn: Callable[[ToolCall, ToolResult], Awaitable[ToolResult]] | None = None
    post_validator: Callable[
        [ToolCall, ToolResult], Awaitable["bool | VerifierResult"]
    ] | None = None
    """Semantic verification hook (Issue #196).

    Runs after a tool mechanically succeeds (``result.success=True``). When
    the returned ``VerifierResult.passed`` (or plain ``bool``) is False, the
    harness downgrades the tool result to
    ``success=False, failure_type="semantic_failure"`` and — if
    ``rollback_fn`` is defined — runs the rollback. The model sees a clear
    signal that the action didn't achieve its intent and can self-correct.

    Returning plain ``bool`` is supported for backward compatibility; prefer
    ``VerifierResult(passed=..., reason=...)`` to give the model a
    human-readable explanation of what went wrong.
    """
    impact_scope: str = "general"
    """
    Impact scope classification for lifecycle audit (Issue #42).

    Renamed from ``scope`` in Phase D (Issue #45) to avoid confusion with
    the scope-aware permission fields (``scope_resolver``, ``scope_descriptions``).
    Values: "filesystem", "shell", "network", "memory", "agent", "general".
    """

    inline_only: bool = False
    """
    If True, ``JITRetrievalMiddleware`` will not spill this tool's output
    to scratchpad regardless of size (Issue #197).

    Set this for tools whose purpose is already to return content the agent
    needs inline — spilling defeats the point. Examples: ``scratchpad_read``
    (paradoxical to re-spill), ``list_dir`` (always small), ``memorize`` /
    ``task_write`` (structured short responses).
    """

    spill_threshold_chars: int | None = None
    """
    Per-tool override for ``JITRetrievalMiddleware`` spill threshold (Issue #302).

    When set, the JIT spill is gated by this value instead of the pipeline-wide
    default (8000 chars). Use a higher value for tools whose output the agent
    nearly always wants inline — e.g. ``read_file`` returning a few hundred
    lines of source: spilling those forces an extra ``scratchpad_read`` round
    trip with no token savings worth the friction.

    ``None`` (default) means "use the global threshold". ``0`` disables spill
    entirely (equivalent to ``inline_only=True``).
    """

    # --- Scope-aware permission (Issue #45 Phase A) ---
    scope_descriptions: list[str] = field(default_factory=list)
    """
    Human-readable summaries of the tool's scope behavior.

    Used for audit log, confirm prompt, and documentation.
    Examples: "writes under requested workspace path",
              "executes shell commands within workspace sandbox".
    """

    scope_resolver: Callable[[ToolCall], ScopeRequest] | None = None
    """
    Dynamic resolver that converts tool call arguments into a ScopeRequest.

    When present, BlastRadiusMiddleware (Phase B) will use this to
    perform scope-aware authorization instead of tool-name authorization.
    When absent, the tool falls back to legacy tool-name authorization.
    """

    def to_anthropic_schema(self) -> dict[str, Any]:
        """Serialize to the format expected by the Anthropic messages API."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def to_openai_schema(self) -> dict[str, Any]:
        """Serialize to the OpenAI / router-canonical tool format."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema,
        }


_TOOL_NAME_PATTERN = __import__("re").compile(r"^[a-zA-Z0-9_-]+$")


class ToolRegistry:
    """
    Central registry for all tools available to the agent.

    Tools are registered once (usually at startup) and looked up by name
    during tool-use cycles.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        # Strict providers (DeepSeek, OpenAI) reject tool names that don't
        # match ^[a-zA-Z0-9_-]+$. Catch violations at boot rather than
        # letting them surface as opaque 400s mid-turn.
        if not _TOOL_NAME_PATTERN.match(tool.name):
            raise ValueError(
                f"Invalid tool name {tool.name!r}: must match ^[a-zA-Z0-9_-]+$ "
                f"(letters, digits, underscore, hyphen). Common offenders: "
                f"':' '.' '/' — use '__' as a namespace separator instead."
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def to_anthropic_schema(self) -> list[dict[str, Any]]:
        return [t.to_anthropic_schema() for t in self._tools.values()]

    def to_openai_schema(self) -> list[dict[str, Any]]:
        return [t.to_openai_schema() for t in self._tools.values()]
