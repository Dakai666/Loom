"""
SelfReflectionPlugin — LoomPlugin wrapper for relational self-reflection.

Belongs in the extensibility layer; the core analysis logic lives in
``loom.autonomy.self_reflection``.

Register via ``~/.loom/plugins/self_reflection.py``::

    from loom.extensibility.self_reflection_plugin import SelfReflectionPlugin
    import loom
    loom.register_plugin(SelfReflectionPlugin())
"""

from __future__ import annotations

import asyncio
import logging

from loom.extensibility.plugin import LoomPlugin

logger = logging.getLogger(__name__)


class SelfReflectionPlugin(LoomPlugin):
    """
    Plugin that hooks into session stop to reflect on behavioural patterns.

    Installs a ``reflect_self`` tool (SAFE trust) so the agent can also
    trigger reflection on demand via a slash command or autonomy trigger.
    """

    name = "self_reflection"
    version = "1.0"

    # ------------------------------------------------------------------
    # LoomPlugin.tools()
    # ------------------------------------------------------------------

    def tools(self):
        """Register the ``reflect_self`` tool into the session registry."""
        from loom.core.harness.registry import ToolDefinition
        from loom.core.harness.middleware import ToolResult
        from loom.core.harness.permissions import TrustLevel
        from loom.autonomy.self_reflection import run_self_reflection

        async def _reflect_self_executor(call) -> ToolResult:
            session = call.args.get("_session")  # injected by on_session_start
            if session is None:
                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name, success=False,
                    error="reflect_self: session context not available.",
                )

            episodic = getattr(session, "_episodic", None)
            relational = getattr(session, "_relational", None)
            if episodic is None or relational is None:
                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name, success=False,
                    error="reflect_self: memory stores not initialised.",
                )

            async def _llm(prompt: str) -> str:
                resp = await session.router.chat(
                    model=session.model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=512,
                )
                return resp.text or ""

            written = await run_self_reflection(
                episodic=episodic,
                relational=relational,
                llm_fn=_llm,
                session_id=session.session_id,
            )
            if not written:
                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name, success=True,
                    output="Self-reflection complete — no new patterns identified.",
                )
            lines = [f"Self-reflection: {len(written)} pattern(s) recorded:"]
            for e in written:
                lines.append(f"  [{e.predicate}] {e.object}")
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=True,
                output="\n".join(lines),
            )

        tool = ToolDefinition(
            name="reflect_self",
            description=(
                "Trigger a self-reflection cycle: analyse recent episodic memory and "
                "write behavioural observations (tends_to / should_avoid / discovered) "
                "as loom-self relational triples. No arguments required."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            executor=_reflect_self_executor,
            trust_level=TrustLevel.SAFE,
        )
        return [tool]

    def middleware(self) -> list:
        return []

    def lenses(self) -> list:
        return []

    def notifiers(self) -> list:
        return []

    # ------------------------------------------------------------------
    # LoomPlugin.on_session_start()
    # ------------------------------------------------------------------

    def on_session_start(self, session: object) -> None:
        """Bind session reference into the reflect_self executor closure."""
        self._session = session

        tool_def = session.registry.get("reflect_self")  # type: ignore[attr-defined]
        if tool_def is None:
            return

        _original_executor = tool_def.executor

        async def _bound_executor(call):
            call.args["_session"] = session
            return await _original_executor(call)

        tool_def.executor = _bound_executor  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # LoomPlugin.on_session_stop()
    # ------------------------------------------------------------------

    def on_session_stop(self, session: object) -> None:
        """Fire-and-forget post-session reflection."""
        from loom.autonomy.self_reflection import run_self_reflection

        episodic = getattr(session, "_episodic", None)
        relational = getattr(session, "_relational", None)
        if episodic is None or relational is None:
            return

        async def _llm(prompt: str) -> str:
            try:
                resp = await session.router.chat(  # type: ignore[attr-defined]
                    model=session.model,  # type: ignore[attr-defined]
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=512,
                )
                return resp.text or ""
            except Exception:
                return ""

        async def _run():
            try:
                await run_self_reflection(
                    episodic=episodic,
                    relational=relational,
                    llm_fn=_llm,
                    session_id=getattr(session, "session_id", None),
                )
            except Exception as exc:
                logger.warning("SelfReflectionPlugin.on_session_stop failed: %s", exc)

        try:
            asyncio.ensure_future(_run())
        except RuntimeError:
            pass  # No running event loop — skip silently
