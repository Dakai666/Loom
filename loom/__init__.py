"""Loom — Harness-first, memory-native, self-directing agent framework."""

__version__ = "0.3.6.1"

from loom.extensibility.adapter import AdapterRegistry as _AdapterRegistry
from loom.extensibility.plugin import LoomPlugin, PluginRegistry as _PluginRegistry

# ---------------------------------------------------------------------------
# Module-level registries (populated by @loom.tool and loom.register_plugin)
# ---------------------------------------------------------------------------

# Default adapter registry — receives @loom.tool decorated functions.
_default_registry = _AdapterRegistry()

# Default plugin registry — receives loom.register_plugin() calls.
_default_plugin_registry = _PluginRegistry()


def tool(
    *,
    description: str | None = None,
    trust_level: str = "safe",
    input_schema: dict | None = None,
    tags: list[str] | None = None,
):
    """
    Decorator that registers an async tool function into Loom's default
    adapter registry.  Use this for simple single-tool extensions.

    For plugins that contribute tools + middleware + lenses together,
    use ``loom.register_plugin(MyPlugin())`` instead.

    Usage (in ~/.loom/plugins/my_tools.py or loom_tools.py)
    ---------------------------------------------------------
        import loom

        @loom.tool(trust_level="safe", description="Query internal API")
        async def query_internal_api(call):
            ...
    """
    return _default_registry.tool(
        description=description,
        trust_level=trust_level,
        input_schema=input_schema,
        tags=tags,
    )


def register_plugin(plugin: LoomPlugin) -> None:
    """
    Register a LoomPlugin into the module-level default plugin registry.

    The plugin's tools, middleware, lenses, and notifiers are installed
    into every new LoomSession that loads this plugin file.

    Usage (in ~/.loom/plugins/my_plugin.py)
    ----------------------------------------
        import loom
        from loom.extensibility import LoomPlugin

        class MyPlugin(LoomPlugin):
            name = "my_plugin"

            def tools(self):
                return [my_tool_def]

        loom.register_plugin(MyPlugin())
    """
    _default_plugin_registry.register(plugin)


def _get_default_registry() -> _AdapterRegistry:
    """Return the module-level default adapter registry (internal use)."""
    return _default_registry


def _get_default_plugin_registry() -> _PluginRegistry:
    """Return the module-level default plugin registry (internal use)."""
    return _default_plugin_registry
