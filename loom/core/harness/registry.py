"""
Tool Registry — the single source of truth for what tools exist, their
trust level, and their JSON schema in both Anthropic and OpenAI formats.
"""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .middleware import ToolCall, ToolResult
from .permissions import TrustLevel


@dataclass
class ToolDefinition:
    """
    Describes one tool: its contract (name, schema, trust) and its executor.

    `executor` is an async function that receives a ToolCall and returns a
    ToolResult.  The harness calls it as the innermost step in the pipeline.
    """
    name: str
    description: str
    trust_level: TrustLevel
    input_schema: dict[str, Any]
    executor: Callable[[ToolCall], Awaitable[ToolResult]]
    tags: list[str] = field(default_factory=list)

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


class ToolRegistry:
    """
    Central registry for all tools available to the agent.

    Tools are registered once (usually at startup) and looked up by name
    during tool-use cycles.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def to_anthropic_schema(self) -> list[dict[str, Any]]:
        return [t.to_anthropic_schema() for t in self._tools.values()]

    def to_openai_schema(self) -> list[dict[str, Any]]:
        return [t.to_openai_schema() for t in self._tools.values()]
