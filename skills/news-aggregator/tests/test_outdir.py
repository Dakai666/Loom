"""``--outdir`` stays inside reports/ (#690 S7 review).

An unattended phase may run fetch_news.py through a command shape that
accepts any extra argument, so the script itself refuses an output
directory outside its reports/ tree. The check runs before any fetch.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fetch_news.py"
REPORTS = SCRIPT.parent.parent / "reports"


def _run(*args, cwd):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--source", "no_such_source", *args],
        cwd=cwd, capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("outdir", [
    "{tmp}/elsewhere",
    "{reports}/../escape",
    "{reports}-sibling",
])
def test_outdir_outside_reports_is_refused_before_writing(tmp_path, outdir):
    target = outdir.format(tmp=tmp_path, reports=REPORTS)
    for form in (["--outdir", target], [f"--outdir={target}"]):
        proc = _run(*form, cwd=tmp_path)
        assert proc.returncode == 2, proc.stderr
        assert "--outdir must be inside" in proc.stderr
        assert not os.path.exists(target)


def test_symlink_out_of_reports_is_refused(tmp_path):
    REPORTS.mkdir(exist_ok=True)
    link = REPORTS / f"_test_link_{os.getpid()}"
    link.symlink_to(tmp_path)
    try:
        proc = _run("--outdir", str(link / "x"), cwd=tmp_path)
    finally:
        link.unlink()
    assert proc.returncode == 2
    assert not (tmp_path / "x").exists()


def test_outdir_inside_reports_is_accepted(tmp_path):
    target = REPORTS / f"_test_{os.getpid()}"
    proc = _run("--outdir", str(target), cwd=tmp_path)
    try:
        assert proc.returncode == 0, proc.stderr
    finally:
        if target.exists():
            for f in target.iterdir():
                f.unlink()
            target.rmdir()
