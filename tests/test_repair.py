"""Auto-repair of failed installs: deps install (fix elif), missing-module pip, agent repair."""
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tm import core
from tm.agents import BuildResult
from tm.config import Config
from tm.installers import methods
from tm.manifest import Manifest


def _cfg(tmp_path) -> Config:
    cfg = Config(root=tmp_path, models={"builder": "sonnet"},
                 agents={"extra_args": [], "builder_max_turns": 40}, docker={})
    cfg.bin_dir.mkdir(parents=True, exist_ok=True)
    cfg.python_dir.mkdir(parents=True, exist_ok=True)
    return cfg


# ---- 1. deps: pyproject AND requirements.txt both installed (the Apepe bug)

def test_install_uv_deps_installs_both(tmp_path, monkeypatch):
    src = tmp_path / "src"; src.mkdir()
    (src / "pyproject.toml").write_text("[project]\nname='x'\n")
    (src / "requirements.txt").write_text("rich\n")
    (src / "requirements").mkdir()
    (src / "requirements" / "extra.txt").write_text("httpx\n")
    calls = []
    monkeypatch.setattr(methods, "run", lambda cmd, **k: calls.append(cmd) or _ok())
    methods._install_uv_deps(Path("/venv/bin/python"), src, {}, _Log())
    joined = [" ".join(c) for c in calls]
    assert any("-e" in c and "pyproject" not in c for c in joined)         # editable project
    assert any("requirements.txt" in c for c in joined)                    # NOT skipped (was elif)
    assert any("extra.txt" in c for c in joined)                           # requirements/*.txt


# ---- 2. missing-module pip fix

def test_pip_fix_missing_parses_and_installs(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    tool_dir = cfg.tool_dir("apepe")
    py = tool_dir / "venv" / "bin" / "python"; py.parent.mkdir(parents=True); py.write_text("")
    installed = []
    monkeypatch.setattr(core, "run", lambda cmd, **k: installed.append(cmd[-1]) or _ok())
    ok = core._pip_fix_missing(cfg, tool_dir, "ModuleNotFoundError: No module named 'rich'", _Log())
    assert ok and "rich" in installed


def test_pip_fix_missing_maps_pypi_name(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    tool_dir = cfg.tool_dir("t")
    py = tool_dir / "venv" / "bin" / "python"; py.parent.mkdir(parents=True); py.write_text("")
    installed = []
    monkeypatch.setattr(core, "run", lambda cmd, **k: installed.append(cmd[-1]) or _ok())
    core._pip_fix_missing(cfg, tool_dir, "No module named 'yaml'", _Log())
    assert "pyyaml" in installed


def test_pip_fix_missing_ignores_local_layout(tmp_path):
    cfg = _cfg(tmp_path)
    tool_dir = cfg.tool_dir("t2")
    (tool_dir / "venv" / "bin").mkdir(parents=True)
    (tool_dir / "venv" / "bin" / "python").write_text("")
    # "src" is a local package, never a PyPI package
    assert core._pip_fix_missing(cfg, tool_dir, "No module named 'src'", _Log()) is False


# ---- 3. agent repair escalation

def test_repair_flow_runs_agent_and_resmokes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    tool_dir = cfg.tool_dir("tool"); (tool_dir / "venv" / "bin").mkdir(parents=True)
    (tool_dir / "venv" / "bin" / "python").write_text("")
    (tool_dir / "src").mkdir()
    shim = cfg.bin_dir / "tool"; shim.write_text("#!/bin/sh\n# managed by tm — tool: tool\nexit 1\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    m = Manifest(name="tool", source="s", method="uv_project", entrypoints={"tool": "/x"})

    smokes = iter([(False, "No module named 'rich'"), (False, "No module named 'rich'"), (True, "")])
    monkeypatch.setattr(core, "_smoke_test", lambda *a, **k: next(smokes))
    monkeypatch.setattr(core, "_pip_fix_missing", lambda *a, **k: False)  # deterministic can't fix
    called = {}
    def fake_build(*a, **k):
        called["yes"] = True
        return BuildResult(status="ok")
    monkeypatch.setattr(core.agents, "build", fake_build)
    core._run_smoke_with_repair(cfg, "tool", tool_dir, m, assume_yes=True, log=_Log())
    assert called.get("yes")  # agent repair was invoked, then re-smoked ok


def test_no_repair_for_apt(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    m = Manifest(name="nmap", source="apt:nmap", method="apt", entrypoints={"nmap": "/usr/bin/nmap"})
    monkeypatch.setattr(core, "_smoke_test", lambda *a, **k: (False, "boom"))
    monkeypatch.setattr(core.agents, "build",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    core._run_smoke_with_repair(cfg, "nmap", cfg.tool_dir("nmap"), m, assume_yes=True, log=_Log())


# ---- helpers

class _Log:
    def write(self, *a, **k): pass


class _P:
    returncode = 0


def _ok():
    return _P()
