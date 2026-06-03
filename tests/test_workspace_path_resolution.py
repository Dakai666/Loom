from pathlib import Path

from loom.platform.cli.tools import _resolve_workspace_path


def test_resolve_workspace_path_accepts_relative_workspace_without_doubling(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    workspace = Path("outputs/self_portrait")

    resolved = _resolve_workspace_path("sisi-starchart_00001_.png", workspace)

    assert resolved == (
        tmp_path / "outputs" / "self_portrait" / "sisi-starchart_00001_.png"
    ).resolve()
