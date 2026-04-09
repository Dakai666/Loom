from pathlib import Path
import tomllib

import loom


def test_package_version_matches_pyproject():
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert loom.__version__ == data["project"]["version"]
