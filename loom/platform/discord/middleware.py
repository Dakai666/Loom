import logging
import discord
from typing import TYPE_CHECKING, Any

from loom.core.harness.middleware import Middleware, ToolCall, ToolHandler, ToolResult

if TYPE_CHECKING:
    from loom.core.session import LoomSession

_log = logging.getLogger(__name__)

class TaskWriteDiscordReminderMiddleware(Middleware):
    """
    Middleware that intercepts `task_write` and, upon success, posts an embed
    to the current Discord thread showing the progress of the tasks.
    Configured via `loom.toml`: `[task_write] discord_reminder = true`.
    """

    def __init__(self, client: discord.Client, thread_id: int, session: "LoomSession") -> None:
        self._client = client
        self._thread_id = thread_id
        self._session = session

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        # 1. Execute the tool first
        result = await next(call)

        # 2. Only proceed if it was a successful task_write call
        if call.tool_name != "task_write" or not result.success:
            return result

        # 3. Check configuration (fallback to False)
        # _loom_config is loaded in LoomSession.__init__
        config = self._session._loom_config.get("task_write", {})
        if not config.get("discord_reminder", False):
            return result

        # 4. Extract tasks from call arguments
        todos = call.args.get("todos", [])
        if not todos:
            return result

        # 5. Fetch the Discord channel
        channel = self._client.get_channel(self._thread_id)
        if not channel:
            return result

        # 6. Format the checklist
        completed = []
        in_progress = []
        pending = []

        for t in todos:
            status = t.get("status", "pending")
            # Format: "✅ **id**: content" or just "✅ content" if id is not available
            content_str = t.get("content", "").strip()
            id_str = t.get("id", "").strip()
            
            # As discussed with the user, display id and content, e.g. "**id**: content"
            if id_str and content_str:
                display_text = f"**{id_str}**: {content_str}"
            elif content_str:
                display_text = content_str
            else:
                display_text = id_str
                
            if status == "completed":
                completed.append(f"✅ {display_text}")
            elif status == "in_progress":
                in_progress.append(f"▶️ {display_text}")
            else:
                pending.append(f"⬜ {display_text}")

        # Construct embed description
        desc_lines = []
        if completed:
            desc_lines.extend(completed)
        if in_progress:
            desc_lines.extend(in_progress)
        if pending:
            desc_lines.extend(pending)

        desc = "\\n".join(desc_lines)
        if len(desc) > 4096:
            desc = desc[:4093] + "..."

        title = "🔄 任務進度"
        if self._session._provisional_title:
            title = f"🔄 任務進度 — {self._session._provisional_title}"

        embed = discord.Embed(
            title=title,
            description=desc,
            color=0x3498db
        )
        embed.set_footer(text="📝 觸發：task_write 更新 · 剛剛")

        try:
            await channel.send(embed=embed)
        except Exception as exc:
            _log.warning("Failed to send task_write reminder embed to Discord thread %s: %s", self._thread_id, exc)

        return result
