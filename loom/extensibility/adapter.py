"""
AdapterRegistry — public API for registering external tools with Loom.

The ``@registry.tool()`` decorator converts an async function into a
``ToolDefinition`` and registers it in one step.

Usage
-----
    adapters = AdapterRegistry()

    @adapters.tool(trust_level="safe", tags=["http"])
    async def fetch_url(call: ToolCall) -> ToolResult:
        \"\"\"Fetch the contents of a URL.\"\"\"
        url = call.args.get("url", "")
        ...

    # Install all adapter tools into a session
    adapters.install_into(session.registry)

    # Build from a LensResult's platform_adapters
    adapters = AdapterRegistry.from_lens_result(lens_result)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry

if TYPE_CHECKING:
    from loom.extensibility.lens import LensResult


class AdapterRegistry:
    """
    Holds externally registered ToolDefinitions.

    Unlike the built-in ``ToolRegistry``, this registry is designed for
    third-party tools and lens-imported adapters. Use ``install_into()``
    to merge its tools into a live session's ToolRegistry.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    # ------------------------------------------------------------------
    # Manual registration
    # ------------------------------------------------------------------

    def register(self, tool_def: ToolDefinition) -> None:
        """Register a ToolDefinition directly."""
        self._tools[tool_def.name] = tool_def

    def get(self, name: str) -> ToolDefinition | None:
        """Return the tool with the given name, or None."""
        return self._tools.get(name)

    def all(self) -> list[ToolDefinition]:
        """Return all registered ToolDefinitions."""
        return list(self._tools.values())

    def install_into(self, registry: ToolRegistry) -> int:
        """
        Copy all registered tools into a ToolRegistry.
        Returns the number of tools installed.
        """
        for tool in self._tools.values():
            registry.register(tool)
        return len(self._tools)

    # ------------------------------------------------------------------
    # Decorator API
    # ------------------------------------------------------------------

    def tool(
        self,
        *,
        description: str | None = None,
        trust_level: str | TrustLevel = "safe",
        input_schema: dict | None = None,
        tags: list[str] | None = None,
    ) -> Callable[[Callable], ToolDefinition]:
        """
        Decorator factory that converts an async function into a
        ToolDefinition and registers it in this AdapterRegistry.

        Parameters
        ----------
        description:  Defaults to the function's docstring if not provided.
        trust_level:  "safe" | "guarded" | "critical" (or TrustLevel enum).
        input_schema: JSON Schema dict for the tool's arguments.
        tags:         List of tag strings for filtering.

        Returns
        -------
        The created ToolDefinition (replaces the original function in the
        namespace; call ``tool_def.executor(call)`` to test the function).
        """
        def decorator(fn: Callable) -> ToolDefinition:
            desc = description or (fn.__doc__ or "").strip() or fn.__name__

            tl: TrustLevel = (
                TrustLevel[trust_level.upper()]
                if isinstance(trust_level, str)
                else trust_level
            )

            tool_def = ToolDefinition(
                name=fn.__name__,
                description=desc,
                trust_level=tl,
                input_schema=input_schema or {"type": "object", "properties": {}},
                executor=fn,
                tags=list(tags or []),
            )
            self.register(tool_def)
            return tool_def

        return decorator

    # ------------------------------------------------------------------
    # Factory from LensResult
    # ------------------------------------------------------------------

    @classmethod
    def from_lens_result(
        cls,
        result: "LensResult",
        executor_factory: Callable[[dict], Callable] | None = None,
    ) -> "AdapterRegistry":
        """
        Build an AdapterRegistry from a LensResult's ``platform_adapters``.

        Each adapter dict is converted to a ToolDefinition.

        Parameters
        ----------
        result:            A LensResult returned by a Lens.
        executor_factory:  Optional callable that receives an adapter dict and
                           returns an async ``executor(call: ToolCall) -> ToolResult``.
                           If omitted, a placeholder executor is used that returns
                           an error result when called.
        """
        registry = cls()

        for adapter in result.platform_adapters:
            name = (adapter.get("name") or "unknown").strip()
            trust_raw = (adapter.get("trust_level") or "safe").lower()
            try:
                trust = TrustLevel[trust_raw.upper()]
            except KeyError:
                trust = TrustLevel.SAFE

            if executor_factory:
                executor = executor_factory(adapter)
            else:
                _name = name  # close over the current value

                async def _placeholder(call: ToolCall, _n: str = _name) -> ToolResult:
                    return ToolResult(
                        call_id=call.id,
                        tool_name=_n,
                        success=False,
                        error=f"No executor registered for adapter '{_n}'",
                    )

                executor = _placeholder

            tool_def = ToolDefinition(
                name=name,
                description=(adapter.get("description") or "").strip(),
                trust_level=trust,
                input_schema=adapter.get(
                    "input_schema", {"type": "object", "properties": {}}
                ),
                executor=executor,
                tags=list(adapter.get("tags", [])),
            )
            registry.register(tool_def)

        return registry
