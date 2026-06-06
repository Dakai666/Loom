from __future__ import annotations

from loom.platform.cli.ui import tool_begin_line


def test_tool_begin_line_lifts_justification_above_call() -> None:
    line = tool_begin_line(
        "run_bash",
        {
            "command": "pytest -q tests/test_app.py",
            "justification": "較長的測試操作，觀察 footer TOOLING 狀態",
            "timeout": 30,
        },
        width=100,
    )

    text = line.plain

    assert text.startswith("  [·] 較長的測試操作，觀察 footer TOOLING 狀態\n")
    assert "\n      run_bash(" in text
    assert 'command="pytest -q tests/test_app.py"' in text
    assert "timeout=30" in text
    assert "justification=" not in text


def test_tool_begin_line_without_justification_preserves_single_line_shape() -> None:
    line = tool_begin_line(
        "read_file",
        {"path": "loom/platform/cli/ui.py"},
        width=100,
    )

    assert line.plain == '  [·] read_file(path="loom/platform/cli/ui.py")'


def test_tool_begin_line_truncates_long_justification() -> None:
    long_why = "因為" + ("這段理由很長" * 20)

    line = tool_begin_line(
        "run_bash",
        {
            "command": "pytest -q",
            "justification": long_why,
        },
        width=100,
    )

    first_line = line.plain.splitlines()[0]
    assert first_line.startswith("  [·] 因為這段理由很長")
    assert first_line.endswith("…")
    assert len(first_line.removeprefix("  [·] ")) <= 81
    assert long_why not in line.plain
