"""R2-4: programmatic omission is external, operator entrypoints explicit."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from loom.autonomy.chime import ChimeRequest
from loom.platform.discord.bot import LoomDiscordBot
from loom.core.harness.permissions import PermissionContext
from loom.core.harness.registry import ToolRegistry
from tests.test_activity_skills_708 import dawn_request


@pytest.mark.parametrize("trust", ["omitted", None, "unknown"])
def test_default_and_unknown_programmatic_chimes_never_install_authority(trust):
    kwargs = {} if trust == "omitted" else {"input_trust": trust}
    req = ChimeRequest("untrusted", "outside", datetime.now(timezone.utc),
        allowed_tools=("run_bash",), scope_grants=({"resource": "exec", "action": "execute", "selector": "python3 cat.py"},),
        **kwargs)
    if trust == "omitted":
        assert req.input_trust == "external"
    session = SimpleNamespace(perm=PermissionContext("r2"), registry=ToolRegistry())
    tools, source = LoomDiscordBot._apply_chime_permissions(None, req, session)
    assert not tools and not session.perm.grants and session.perm.external_inputs
    LoomDiscordBot._revoke_chime_permissions(None, session, tools, source)


async def test_operator_phase_is_explicitly_local(tmp_path, monkeypatch):
    req = await dawn_request(tmp_path, monkeypatch)
    assert req.input_trust == "local" and req.scope_grants


async def test_operator_schedule_is_explicitly_local():
    from loom.autonomy.daemon import AutonomyDaemon
    from unittest.mock import AsyncMock
    daemon = SimpleNamespace(deliver_chime=AsyncMock(return_value=True))
    plan = SimpleNamespace(trigger_name="operator", prompt="trusted", context={
        "target": {"type": "discord_thread", "id": "1"}, "input_trust": "external"})
    assert await AutonomyDaemon._deliver_chime(daemon, plan)
    assert daemon.deliver_chime.await_args.args[0].input_trust == "local"
