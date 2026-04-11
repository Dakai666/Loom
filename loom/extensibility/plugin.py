"""
Plugin System — unified extension interface for Loom.

A ``LoomPlugin`` can contribute tools, middleware, lenses, and notifiers in one
package.  Plugins live in ``~/.loom/plugins/`` and are auto-scanned on session
start.  The first time a new plugin file is seen, the user is asked to approve
it (GUARDED); approval is stored in RelationalMemory so future sessions load
it silently.

Simple single-tool extensions can still use ``@loom.tool`` — that decorator
registers into the module-level default AdapterRegistry, which is treated as
an anonymous built-in plugin by the session loader.

Usage (in ~/.loom/plugins/git_tools.py)
----------------------------------------
    import loom
    from loom.extensibility import LoomPlugin
    from loom.core.harness.registry import ToolDefinition
    from loom.core.harness.permissions import TrustLevel

    class GitPlugin(LoomPlugin):
        name = "git"
        version = "1.0"

        def tools(self):
            return [git_status_tool, git_diff_tool]

    loom.register_plugin(GitPlugin())

    # Or for simple cases, just use the decorator:
    @loom.tool(trust_level="safe", description="Show git log")
    async def git_log(call): ...
"""

from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loom.core.harness.middleware import Middleware
    from loom.core.harness.registry import ToolDefinition
    from loom.extensibility.lens import BaseLens
    from loom.notify.router import BaseNotifier


class LoomPlugin(ABC):
    """
    Abstract base for Loom plugins.

    Override only the methods you need — everything defaults to empty.
    Loom calls each method once during session startup and merges results
    into the live session without touching the core harness.

    Attributes
    ----------
    name:    Unique identifier (used in approval records and log messages).
    version: Semver string for display purposes.
    """

    name: str = ""
    version: str = "1.0"

    def tools(self) -> list["ToolDefinition"]:
        """Return ToolDefinition objects to install into the session registry."""
        return []

    def middleware(self) -> list["Middleware"]:
        """Return Middleware instances to prepend to the session pipeline."""
        return []

    def lenses(self) -> list["BaseLens"]:
        """Return BaseLens instances to register in the global LensRegistry."""
        return []

    def notifiers(self) -> list["BaseNotifier"]:
        """Return BaseNotifier instances to add to the NotificationRouter."""
        return []

    def on_session_start(self, session: object) -> None:
        """Called after all plugin contributions have been installed."""

    def on_session_stop(self, session: object) -> None:
        """Called just before session.stop() writes its final state."""


class PluginRegistry:
    """
    Holds all loaded LoomPlugin instances for a session.

    ``install_into(session)`` merges each plugin's contributions into the
    live session's registry and pipeline.  Plugins are applied in
    registration order; later plugins can shadow earlier tool names.
    """

    def __init__(self) -> None:
        self._plugins: list[LoomPlugin] = []

    def register(self, plugin: LoomPlugin) -> None:
        """Add a plugin.  Replaces any existing plugin with the same name."""
        self._plugins = [p for p in self._plugins if p.name != plugin.name]
        self._plugins.append(plugin)

    def all(self) -> list[LoomPlugin]:
        return list(self._plugins)

    def install_into(self, session: object) -> dict[str, int]:
        """
        Install all registered plugins into a live LoomSession.

        Returns a summary dict: {plugin_name: tools_installed, ...}
        """
        from loom.core.harness.middleware import MiddlewarePipeline

        summary: dict[str, int] = {}

        for plugin in self._plugins:
            label = plugin.name or "(anonymous)"
            count = 0

            # Tools → session registry
            for tool_def in plugin.tools():
                session.registry.register(tool_def)  # type: ignore[attr-defined]
                count += 1

            # Middleware → prepend to pipeline (before TraceMiddleware)
            for mw in plugin.middleware():
                if session._pipeline is not None:  # type: ignore[attr-defined]
                    session._pipeline._middlewares.insert(0, mw)  # type: ignore[attr-defined]

            # Lenses → global LensRegistry (no per-session state needed)
            # (LensRegistry is stateless; plugins just add more lenses)

            # Notifiers → session notification router (if present)
            for notifier in plugin.notifiers():
                router = getattr(session, "_notifier_router", None)
                if router is not None:
                    router.register(notifier)

            # Lifecycle hook
            try:
                plugin.on_session_start(session)
            except Exception:
                pass

            summary[label] = count

        return summary
