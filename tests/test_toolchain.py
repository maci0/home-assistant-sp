"""One Python version, one uv version: every place that names them agrees.

Nothing forces ``.python-version``, ``requires-python``, the mypy target and
the ruff target to move together, so a bump in one leaves the others checking
against a release the build no longer uses. These fail on the disagreement
instead.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
PINNED = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
MINOR = ".".join(PINNED.split(".")[:2])
# "py312" is how ruff spells 3.12.
RUFF_TARGET = re.fullmatch(r"py(\d)(\d+)", PYPROJECT["tool"]["ruff"]["target-version"])
# ">=3.12" is how the floor of requires-python is spelled.
REQUIRES_PYTHON = re.fullmatch(r">=(\d+\.\d+)", PYPROJECT["project"]["requires-python"])
REQUIRED_VERSION = re.fullmatch(
    r">=(\d+\.\d+\.\d+),<\d+\.\d+", PYPROJECT["tool"]["uv"]["required-version"]
)


def _declared_python_versions() -> dict[str, str]:
    """Every spelling of the language version in the build configuration."""
    assert RUFF_TARGET is not None
    assert REQUIRES_PYTHON is not None
    return {
        ".python-version": PINNED,
        "requires-python": REQUIRES_PYTHON.group(1),
        "tool.mypy.python_version": PYPROJECT["tool"]["mypy"]["python_version"],
        "tool.ruff.target-version": f"{RUFF_TARGET.group(1)}.{RUFF_TARGET.group(2)}",
    }


def test_python_version_declared_once() -> None:
    declared = _declared_python_versions()
    assert declared == dict.fromkeys(declared, MINOR)


def test_uv_version_pinned_once() -> None:
    """CI installs the uv floor ``pyproject.toml`` declares, not some other one."""
    assert REQUIRED_VERSION is not None
    # setup-uv is the only tool in the workflow carrying a version key; a
    # second would be a second pin to keep in step with the floor.
    pins = set(re.findall(r'version:\s*"([^"]+)"', CI))
    assert pins == {REQUIRED_VERSION.group(1)}
