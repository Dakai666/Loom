"""
ArtifactsPanel component — list of generated artifacts.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.scroll_view import ScrollView
from textual.widget import Widget
from textual.widgets import Static

from .artifact_card import Artifact, ArtifactCard, ArtifactState


class ArtifactsPanel(Widget):
    """
    Panel showing list of artifacts generated during the session.

    Layout:
    ┌─ ARTIFACTS ───────────────────────────────────────────────────┐
    │  file.py       modified   2m ago   [jump] [diff]            │
    │  tests/test.py created    2m ago   [jump] [view]            │
    │  config.toml   modified   5m ago   [jump] [diff]            │
    └───────────────────────────────────────────────────────────────┘
    """

    artifacts: reactive[list[Artifact]] = reactive([], layout=True)
    _selected_index: int = 0

    class ArtifactSelected(Message, bubble=True):
        """User selected an artifact."""

        def __init__(self, artifact: Artifact) -> None:
            super().__init__()
            self.artifact = artifact

    def compose(self) -> ComposeResult:
        yield Static("", id="artifacts-content")

    def add_artifact(
        self,
        path: str,
        state: ArtifactState,
        diff_lines: list[str] | None = None,
        preview: str = "",
    ) -> None:
        """Add a new artifact to the panel."""
        artifact = Artifact(
            path=path,
            state=state,
            timestamp=datetime.datetime.now(),
            diff_lines=diff_lines or [],
            preview=preview,
        )
        self.artifacts = [*self.artifacts, artifact]
        self._render()

    def clear(self) -> None:
        """Clear all artifacts."""
        self.artifacts = []
        self._selected_index = 0
        self._render()

    def _render(self) -> None:
        content = self.query_one("#artifacts-content", Static)
        if not self.artifacts:
            content.update("[dim](no artifacts yet)[/dim]")
            return

        lines = ["[bold]ARTIFACTS[/bold]", ""]

        for i, a in enumerate(self.artifacts):
            age = self._age_string(a.timestamp)
            state_color = {
                ArtifactState.CREATED: "green",
                ArtifactState.MODIFIED: "yellow",
                ArtifactState.DELETED: "red",
            }[a.state]
            filename = a.path.split("/")[-1].split("\\")[-1]
            marker = ">" if i == self._selected_index else " "

            lines.append(
                f"{marker} [yellow]{filename}[/yellow]  "
                f"[{state_color}]{a.state.value}[/{state_color}]  "
                f"[dim]{age}[/dim]"
            )

        content.update("\n".join(lines))

    def _age_string(self, dt: datetime.datetime) -> str:
        delta = datetime.datetime.now() - dt
        if delta.total_seconds() < 60:
            return f"{int(delta.total_seconds())}s ago"
        if delta.total_seconds() < 3600:
            return f"{int(delta.total_seconds() / 60)}m ago"
        return f"{int(delta.total_seconds() / 3600)}h ago"
