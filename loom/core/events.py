"""Session stream event types.

``LoomSession.stream_turn()`` is an async generator that yields a sequence of
these typed events.  All platform consumers (CLI, TUI, Discord) branch on the
event type to drive their respective UIs.

Keeping event types in ``loom.core`` lets every platform import them without
depending on ``loom.platform.cli``.

Stream Event Consumer Map
-------------------------
Producer: ``LoomSession.stream_turn()``

Use this table as the authoritative contract between the harness layer and
platform consumers.  When adding a new event type, update this table **and**
add ``Producers:`` / ``Consumers:`` entries to the event's docstring.

+---------------------+-----+-----+---------+----------+
| Event               | CLI | TUI | Discord | Required |
+=====================+=====+=====+=========+==========+
| TextChunk           |  ✓  |  ✓  |    ✓    |   YES    |
| ToolBegin           |  ✓  |  ✓  |    ✓    |   YES    |
| ToolEnd             |  ✓  |  ✓  |    ✓    |   YES    |
| TurnDone            |  ✓  |  ✓  |    ✓    |   YES    |
| ThinkCollapsed      |  ✓  |  ✓  |    ✓    |   no     |
| TurnPaused          |  ✓  |  ✓  |    ✓    |   no     |
| TurnDropped         |  ✓  |  —  |    ✓    |   no     |
| CompressDone        |  —  |  —  |    ✓    |   no     |
| ActionStateChange   |  —  |  ✓  |  skip   |   no     |
| ActionRolledBack    |  —  |  ✓  |    ✓    |   no     |
| EnvelopeStarted     |  ✓  |  ✓  |    ✓    |   no     |
| EnvelopeUpdated     |  ✓  |  ✓  |    ✓    |   no     |
| EnvelopeCompleted   |  ✓  |  ✓  |    ✓    |   no     |
| GrantsSnapshot      |  ✓  |  ✓  |    —    |   no     |
+---------------------+-----+-----+---------+----------+

Legend:
  ✓   = handled (active UI update)
  —   = intentionally ignored (``else: pass`` is correct)
  skip = received but no-op by design (too granular for that platform)

``EventConsumer`` protocol
--------------------------
The ``EventConsumer`` Protocol at the bottom of this module describes the
minimum surface a conforming consumer must implement.  Platform dispatch loops
do not need to subclass it — it exists purely for static analysis (pyright /
graphify) to verify coverage.

Usage (opt-in type annotation)::

    class MyConsumer:
        async def on_text_chunk(self, event: TextChunk) -> None: ...
        async def on_tool_begin(self, event: ToolBegin) -> None: ...
        async def on_tool_end(self, event: ToolEnd) -> None: ...
        async def on_turn_done(self, event: TurnDone) -> None: ...

    def run(consumer: EventConsumer, ...) -> None:
        ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class TextChunk:
    """A fragment of streaming LLM text.

    Producers:
        LoomSession.stream_turn() — emitted for every text delta from the
        LLM API while the model is generating.

    Consumers:
        CLI     ✓  accumulated in text_buffer, printed character-by-character
        TUI     ✓  dispatched as TuiChunk → MessageList
        Discord ✓  accumulated in narration_buf, sent as a batch after turn
    """

    text: str


@dataclass
class ToolBegin:
    """The agent is about to call a tool.

    Producers:
        LoomSession.stream_turn() — emitted immediately before the tool
        handler is dispatched, after BlastRadiusMiddleware has authorized.

    Consumers:
        CLI     ✓  prints spinner / tool-name line
        TUI     ✓  dispatched as TuiToolBegin → ToolBlock
        Discord ✓  appends symbol+tool_name to tool_buf (legacy path only;
                    suppressed when EnvelopeStarted has fired)
    """

    name: str
    args: dict[str, Any]
    call_id: str


@dataclass
class ToolEnd:
    """A tool call finished.

    Producers:
        LoomSession.stream_turn() — emitted after the tool handler returns
        and the result has been observed by LifecycleMiddleware.

    Consumers:
        CLI     ✓  cancels spinner, prints ok/error status
        TUI     ✓  dispatched as TuiToolEnd → ToolBlock / ActivityLog
        Discord ✓  appends ✓/✗ marker to tool_buf (legacy path only;
                    suppressed when EnvelopeStarted has fired)
    """

    name: str
    success: bool
    output: str
    duration_ms: float
    call_id: str


@dataclass
class TurnPaused:
    """The agent turn has been paused — all current tool calls are done but the
    loop is suspended, waiting for human input before continuing.

    The consumer should prompt the user and then call either::

        session.resume()         — continue the turn as-is
        session.resume_with(msg) — inject a message and continue
        session.cancel()         — abandon the rest of this turn

    Producers:
        LoomSession.stream_turn() — emitted when hitl_mode is on, after each
        tool batch completes, before the next LLM call.

    Consumers:
        CLI     ✓  blocks on input(), calls session.resume() / cancel()
        TUI     ✓  shows PauseModal widget, awaits user decision
        Discord ✓  sends pause message, waits for reply with client.wait_for()
    """

    tool_count_so_far: int = 0


@dataclass
class CompressDone:
    """Episodic memory was compressed to semantic facts mid-session.

    Producers:
        LoomSession._smart_compact() — emitted after compression finishes.

    Consumers:
        CLI     —  intentionally not displayed (too noisy for inline chat)
        TUI     —  intentionally not displayed
        Discord ✓  sends a brief status message to the thread
    """

    fact_count: int


@dataclass
class TurnDone:
    """The complete agent turn (including all tool loops) is done.

    Producers:
        LoomSession.stream_turn() — emitted once per user message, after all
        tool calls and the final LLM response have completed.

    Consumers:
        CLI     ✓  prints token/timing footer line
        TUI     ✓  dispatched as TuiTurnDone → BudgetPanel + ObservabilityPanel
        Discord ✓  triggers turn summary (if summary_mode != "off") + footer
    """

    tool_count: int
    input_tokens: int
    output_tokens: int
    elapsed_ms: float
    stop_reason: str = "complete"  # "complete" | "cancelled"


@dataclass
class TurnDropped:
    """The agent turn was dropped mid-stream due to an unexpected stop.

    This happens when:
    - The LLM API returns ``stop_reason`` that is neither ``end_turn`` nor
      ``tool_use`` (e.g. ``max_tokens``, provider-specific error codes).
    - The streaming response object is ``None`` (connection dropped before
      any final message arrived).

    ``stop_reason`` — the raw stop_reason string from the provider, or
                      ``"stream_none"`` when response was None.
    ``retry_count``  — how many automatic retries have already been attempted.
    ``tool_count``   — number of tools called before the drop.

    Producers:
        LoomSession.stream_turn() — emitted instead of TurnDone when the
        stream terminates abnormally.

    Consumers:
        CLI     ✓  prints a warning message to the console
        TUI     —  not separately surfaced (worker catches the exception)
        Discord ✓  sends a ⚠️ status message with stop_reason details
    """

    stop_reason: str
    retry_count: int = 0
    tool_count: int = 0
    exhausted: bool = False


@dataclass
class ActionStateChange:
    """An action transitioned to a new lifecycle state (Issue #42).

    Producers:
        LifecycleMiddleware / LifecycleGateMiddleware — emitted on every
        ActionRecord state transition (DECLARED → AUTHORIZED → … → MEMORIALIZED).

    Consumers:
        CLI     —  intentionally not displayed (too granular for terminal chat)
        TUI     ✓  dispatched as TuiActionStateChange → ExecutionDashboard
        Discord skip  received but a no-op (``pass``) — too granular for chat
    """

    action_id: str
    tool_name: str
    call_id: str
    old_state: str
    new_state: str
    reason: str | None = None


@dataclass
class ActionRolledBack:
    """An action was rolled back after post-validation failure (Issue #42).

    Producers:
        LifecycleMiddleware — emitted when a tool's post-execution validator
        returns False and the rollback handler is invoked.

    Consumers:
        CLI     —  not yet surfaced (post-validation rarely fires in CLI)
        TUI     ✓  dispatched as TuiActionRolledBack → ExecutionDashboard
        Discord ✓  appends ↩ rollback line to tool_buf
    """

    action_id: str
    tool_name: str
    call_id: str
    rollback_success: bool
    message: str = ""


@dataclass
class ThinkCollapsed:
    """A <think>…</think> block closed during streaming.

    Replaces the old ``TextChunk("▸ thinking…\\n")`` placeholder so each
    platform can render reasoning content in its own style.

    ``summary`` — first ~120 chars of the reasoning block, single line.
    ``full``    — complete think content for the detail view.

    Producers:
        LoomSession.stream_turn() — emitted when a ``</think>`` tag is
        detected in the streamed output.

    Consumers:
        CLI     ✓  prints a dim "thinking…" indicator
        TUI     ✓  dispatched as TuiThinkCollapsed → MessageList
        Discord ✓  sends as a -# small message with 💭 prefix
    """

    summary: str
    full: str


# ---------------------------------------------------------------------------
# Issue #106: ExecutionEnvelope ViewModel & stream events
# ---------------------------------------------------------------------------

@dataclass
class ExecutionNodeView:
    """Single action node view for UI consumption.

    Maps 1:1 to an ``ActionRecord`` but carries only the fields the
    presentation layer needs — no mutable state, no references to
    middleware internals.
    """

    node_id: str           # ActionRecord.id
    call_id: str           # ToolCall.id
    action_id: str | None  # same as node_id (for API clarity)
    tool_name: str
    level: int             # parallel level (0 = all current dispatch)
    state: str             # ActionState.value
    trust_level: str       # SAFE / GUARDED / CRITICAL  (use TrustLevel.plain)
    capabilities: list[str] = field(default_factory=list)
    args_preview: str = ""
    duration_ms: float = 0.0
    error_snippet: str = ""
    depends_on: list[str] = field(default_factory=list)
    # ── Detail fields (Issue #108) ──────────────────────────────────
    full_args: dict[str, Any] = field(default_factory=dict)
    state_history: list[dict[str, Any]] = field(default_factory=list)
    auth_decision: str = ""      # "once" / "scope" / "auto" / "deny" / ""
    auth_expires: float = 0.0    # time.time() expiry; 0 = permanent/N/A
    auth_selector: str = ""      # scope selector (e.g. "/workspace/doc/")
    output_preview: str = ""     # first ~200 chars of tool output


@dataclass
class ExecutionEnvelopeView:
    """Aggregate view for one tool-use batch — the primary UI unit.

    Built by ``LoomSession._build_envelope_view()`` (projection layer)
    and yielded as part of ``EnvelopeStarted / Updated / Completed``
    stream events.  TUI and Discord both consume this same structure.
    """

    envelope_id: str       # human-readable, e.g. "e1", "e2"
    session_id: str
    turn_index: int
    status: str            # "running" / "completed" / "failed"
    node_count: int
    parallel_groups: int   # number of distinct levels
    elapsed_ms: float = 0.0
    levels: list[list[str]] = field(default_factory=list)
    nodes: list[ExecutionNodeView] = field(default_factory=list)


@dataclass
class EnvelopeStarted:
    """A new tool-use batch (envelope) has been created and dispatch begins.

    Producers:
        LoomSession.stream_turn() — emitted when a new parallel tool batch
        starts executing, before any individual tool completes.

    Consumers:
        CLI     ✓  renders envelope header in status area
        TUI     ✓  dispatched as TuiEnvelopeStarted → ExecutionDashboard
        Discord ✓  sends formatted envelope status to status_msg, sets
                    _envelope_active=True to suppress legacy ToolBegin display
    """

    envelope: ExecutionEnvelopeView


@dataclass
class EnvelopeUpdated:
    """A node inside the current envelope changed state (e.g. tool finished).

    Producers:
        LoomSession.stream_turn() — emitted after each individual tool
        completes within an active envelope.

    Consumers:
        CLI     ✓  re-renders envelope status (debounced)
        TUI     ✓  dispatched as TuiEnvelopeUpdated → ExecutionDashboard
        Discord ✓  edits status_msg with updated envelope (debounced 0.5s)
    """

    envelope: ExecutionEnvelopeView


@dataclass
class EnvelopeCompleted:
    """All nodes in the envelope have reached terminal states.

    Producers:
        LoomSession.stream_turn() — emitted when every node in the envelope
        is in a terminal state (memorialized / denied / aborted / reverted).

    Consumers:
        CLI     ✓  renders final envelope state, clears active indicator
        TUI     ✓  dispatched as TuiEnvelopeCompleted → ExecutionDashboard
        Discord ✓  freezes status_msg as permanent record, opens new placeholder
    """

    envelope: ExecutionEnvelopeView


@dataclass
class GrantSummary:
    """One active scope grant — lightweight UI projection (#112)."""

    grant_id: str          # unique identifier for tracking expiry transitions
    tool_name: str         # e.g. "write_file"
    selector: str          # e.g. "/workspace/doc/"
    source: str            # "lease" / "auto" / "manual_confirm"
    expires_at: float      # absolute time.time(); 0 = permanent


@dataclass
class GrantsSnapshot:
    """Current state of active scope grants for UI display (#108, #112).

    Producers:
        LoomSession.stream_turn() — emitted after each tool call that results
        in a new grant being created or an existing one expiring.

    Consumers:
        CLI     ✓  updates status bar grant count
        TUI     ✓  dispatched as TuiGrantsUpdate → BudgetPanel / StatusBar
        Discord —  grants are displayed inline via /scope command, not streamed
    """

    active_count: int
    next_expiry_secs: float = 0.0  # seconds until nearest expiry; 0 = none
    grants: list[GrantSummary] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Issue #146: EventConsumer Protocol
# ---------------------------------------------------------------------------

class EventConsumer(Protocol):
    """Protocol for objects that consume ``LoomSession.stream_turn()`` events.

    Conforming consumers must implement handlers for all **required** events
    (``TextChunk``, ``ToolBegin``, ``ToolEnd``, ``TurnDone``) and may freely
    ignore optional ones.

    This Protocol exists for **static analysis only** — platform dispatch
    loops do not need to subclass it.  It enables pyright / graphify to:

    1. Verify that a consumer implements the minimum required surface.
    2. Track the event-flow graph from producer (stream_turn) to consumers.

    Usage (opt-in type annotation)::

        def build_consumer(...) -> EventConsumer:
            ...

    Required events
    ---------------
    All platform consumers (CLI, TUI, Discord) MUST handle these four events.
    Failing to handle them will result in missed output or a silent turn with
    no user feedback:

    - ``TextChunk``  — streaming LLM text
    - ``ToolBegin``  — tool dispatch started
    - ``ToolEnd``    — tool dispatch finished
    - ``TurnDone``   — turn complete

    Optional events
    ---------------
    Consumers may ignore these safely (``else: pass`` is correct behaviour).
    See the Consumer Map in the module docstring for platform-specific coverage:

    ``ThinkCollapsed``, ``TurnPaused``, ``TurnDropped``, ``CompressDone``,
    ``ActionStateChange``, ``ActionRolledBack``,
    ``EnvelopeStarted``, ``EnvelopeUpdated``, ``EnvelopeCompleted``,
    ``GrantsSnapshot``
    """

    async def on_text_chunk(self, event: TextChunk) -> None:
        """Handle a streaming LLM text fragment."""
        ...

    async def on_tool_begin(self, event: ToolBegin) -> None:
        """Handle tool dispatch start."""
        ...

    async def on_tool_end(self, event: ToolEnd) -> None:
        """Handle tool dispatch completion."""
        ...

    async def on_turn_done(self, event: TurnDone) -> None:
        """Handle turn completion (all tools + final LLM response done)."""
        ...


__all__ = [
    "ActionRolledBack",
    "ActionStateChange",
    "CompressDone",
    "EnvelopeCompleted",
    "EnvelopeStarted",
    "EnvelopeUpdated",
    "EventConsumer",
    "ExecutionEnvelopeView",
    "ExecutionNodeView",
    "GrantSummary",
    "GrantsSnapshot",
    "TextChunk",
    "ThinkCollapsed",
    "ToolBegin",
    "ToolEnd",
    "TurnDone",
    "TurnDropped",
    "TurnPaused",
]
