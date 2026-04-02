"""
Loom TUI — Textual-based terminal interface for Loom agent.

Architecture
------------
Elm-inspired: each component owns its state and responds to messages.

    LoomSession.stream_turn() yields events
           |
           v
    TuiApp.on_event() dispatches to components
           |
           v
    Component.update() -> Component.render()

Layout (dock):
    Header (top dock, height=3)
    ConversationPane (left, 60%)
        MessageList + ToolBlock
    WorkspacePanel (right, 40%)
        Artifacts / Knowledge Graph tabs
    InputArea (bottom dock, height=3)
    StatusBar (bottom dock, height=1)
    ObservabilityPanel (bottom dock, height=3, collapsed by default)

Event model (unchanged from ui.py):
    TextChunk   — partial LLM text
    ToolBegin   — tool call started
    ToolEnd     — tool call finished
    TurnDone    — turn complete
"""

from __future__ import annotations

from .app import LoomApp
from .components import (
    Header,
    MessageList,
    MessageItem,
    ToolBlock,
    StatusBar,
    InputArea,
    ObservabilityPanel,
    ArtifactState,
    Artifact,
    ArtifactsPanel,
    KnowledgeGraph,
    KnowledgeNode,
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
    ToggleVerbose,
    BudgetUpdate,
)

__all__ = [
    "LoomApp",
    "Header",
    "MessageList",
    "MessageItem",
    "ToolBlock",
    "StatusBar",
    "InputArea",
    "ObservabilityPanel",
    "ArtifactState",
    "Artifact",
    "ArtifactsPanel",
    "KnowledgeGraph",
    "KnowledgeNode",
    "WorkspacePanel",
    "WorkspaceTab",
    "StreamEvent",
    "TurnStart",
    "TextChunk",
    "ToolBegin",
    "ToolEnd",
    "TurnDone",
    "ClearScreen",
    "ToggleVerbose",
    "BudgetUpdate",
]
