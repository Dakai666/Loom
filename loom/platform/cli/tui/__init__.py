"""
Loom TUI — Textual-based terminal interface for Loom agent.

Layout:
    Header (top dock, height=3)
    ConversationPane (left, 75%)
        MessageList + ToolBlock
    WorkspacePanel (right, 25%)
        Artifacts / Activity / Budget tabs
    InputArea (bottom of conversation pane)
    StatusBar (bottom dock, height=1)
    ObservabilityPanel (bottom dock, hidden by default)
"""

from __future__ import annotations

from .app import LoomApp
from .components import (
    Header,
    MessageList,
    MessageBubble,
    ToolBlock,
    AgentState,
    StatusBar,
    InputArea,
    ObservabilityPanel,
    ArtifactState,
    Artifact,
    ArtifactsPanel,
    SwarmDashboard,
    ActivityEntry,
    ExecutionDashboard,
    BudgetPanel,
    WorkspacePanel,
    WorkspaceTab,
)
from .events import (
    StreamEvent,
    TurnStart,
    TextChunk,
    ToolBegin,
    ToolEnd,
    TurnDone,
    ClearScreen,
    BudgetUpdate,
    EnvelopeStarted,
    EnvelopeUpdated,
    EnvelopeCompleted,
    GrantsUpdate,
)

__all__ = [
    "LoomApp",
    "Header",
    "MessageList",
    "MessageBubble",
    "ToolBlock",
    "AgentState",
    "StatusBar",
    "InputArea",
    "ObservabilityPanel",
    "ArtifactState",
    "Artifact",
    "ArtifactsPanel",
    "SwarmDashboard",
    "ActivityEntry",
    "ExecutionDashboard",
    "BudgetPanel",
    "WorkspacePanel",
    "WorkspaceTab",
    "StreamEvent",
    "TurnStart",
    "TextChunk",
    "ToolBegin",
    "ToolEnd",
    "TurnDone",
    "ClearScreen",
    "BudgetUpdate",
    "EnvelopeStarted",
    "EnvelopeUpdated",
    "EnvelopeCompleted",
    "GrantsUpdate",
]
