"""Regression: uv console-script selection must not pick dependency scripts (cffi-gen-src)."""
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tm.installers import methods


def _venv_with_scripts(tmp_path: Path, names: list[str]) -> Path:
    binp = tmp_path / "venv" / "bin"
    binp.mkdir(parents=True)
    for n in ["python", "python3", "pip"] + names:
        p = binp / n
        p.write_text("#!/bin/sh\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return tmp_path / "venv"


def _src_with_scripts(tmp_path: Path, scripts: dict[str, str]) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    if scripts:
        body = "\n".join(f'{k} = "{v}"' for k, v in scripts.items())
        (src / "pyproject.toml").write_text(f"[project]\nname='x'\n[project.scripts]\n{body}\n")
    else:
        (src / "pyproject.toml").write_text("[project]\nname='x'\n")
    return src


def test_prefers_project_declared_script(tmp_path):
    venv = _venv_with_scripts(tmp_path, ["laZagne", "cffi-gen-src", "normalizer"])
    src = _src_with_scripts(tmp_path, {"laZagne": "laZagne.main:run"})
    out = methods._console_scripts(venv, [], "lazagne", src)
    assert list(out) == ["laZagne"]


def test_drops_dep_scripts_single_candidate(tmp_path):
    venv = _venv_with_scripts(tmp_path, ["mytool", "cffi-gen-src", "normalizer"])
    src = _src_with_scripts(tmp_path, {})  # nothing declared
    out = methods._console_scripts(venv, [], "somethingelse", src)
    assert list(out) == ["mytool"]  # dep scripts filtered, one real left


def test_ambiguous_returns_empty(tmp_path):
    venv = _venv_with_scripts(tmp_path, ["toola", "toolb", "cffi-gen-src"])
    src = _src_with_scripts(tmp_path, {})
    out = methods._console_scripts(venv, [], "zzz", src)
    assert out == {}  # can't tell → caller falls back to a script entrypoint


def test_hint_wins(tmp_path):
    venv = _venv_with_scripts(tmp_path, ["laZagne", "cffi-gen-src"])
    src = _src_with_scripts(tmp_path, {})
    out = methods._console_scripts(venv, ["laZagne"], "x", src)
    assert list(out) == ["laZagne"]
