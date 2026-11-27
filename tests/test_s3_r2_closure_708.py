"""R2-2: real native filesystem executors must bind the policy workspace."""
import pytest

from loom.platform.cli.tools import external_call_allowed, make_filesystem_tools
from loom.core.harness.middleware import ToolCall


@pytest.mark.parametrize("name", ["read_file", "list_dir"])
@pytest.mark.parametrize("binding", ["same", "alias", "different", "missing"])
def test_real_filesystem_closure_matches_canonical_workspace(tmp_path, name, binding):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    other = tmp_path / "other"
    other.mkdir()
    bound = {"same": workspace, "alias": alias, "different": other, "missing": None}[binding]
    tool = next(t for t in make_filesystem_tools(bound if bound is not None else workspace) if t.name == name)
    if binding == "missing":
        cells = dict(zip(tool.executor.__code__.co_freevars, tool.executor.__closure__))
        del cells["workspace"].cell_contents
    call = ToolCall(tool_name=name, args={}, trust_level=tool.trust_level, session_id="r2", origin="chime")
    assert external_call_allowed(tool, call, workspace) is (binding in ("same", "alias"))
