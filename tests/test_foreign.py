"""Foreign-OS entrypoints: agent-decided, print-path shims; plus detect_os and path_shim."""
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tm import core, shims
from tm.agents import EntryDecision, ImportResult
from tm.config import Config
from tm.installers import archive


def _cfg(tmp_path) -> Config:
    cfg = Config(root=tmp_path, models={"tagger": "haiku"},
                 agents={"extra_args": [], "target_tags": 3}, docker={})
    cfg.bin_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "tags.txt").write_text("web: web stuff\n")
    return cfg


ELF = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8
PE = b"MZ\x90\x00" + b"\x00" * 8
MACHO = b"\xcf\xfa\xed\xfe" + b"\x00" * 8
FAT = b"\xca\xfe\xba\xbe" + b"\x00" * 8


def _write(p: Path, data: bytes):
    p.write_bytes(data)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)


# ---- detect_os

def test_detect_os(tmp_path):
    cases = {"e": (ELF, "linux"), "w": (PE, "windows"), "m": (MACHO, "macos"),
             "f": (FAT, "macos"), "plain": (b"hello\n", "")}
    for nm, (data, want) in cases.items():
        p = tmp_path / nm; p.write_bytes(data)
        assert archive.detect_os(p) == want, nm
    s = tmp_path / "s.sh"; s.write_text("#!/bin/sh\n")
    assert archive.detect_os(s) == "linux"


# ---- path_shim

def test_path_shim_prints_path_not_runs(tmp_path):
    target = tmp_path / "tool.exe"; _write(target, PE)
    p = shims.path_shim(tmp_path / "bin", "tool", "mytool", target, os_label="windows")
    body = p.read_text()
    assert str(target.resolve()) in body
    assert shims.owner_tool(p) == "mytool"
    out = subprocess.run([str(p)], capture_output=True, text=True)
    assert out.stdout.strip() == str(target.resolve())   # path on stdout
    assert "windows" in out.stderr                        # hint on stderr
    assert out.returncode == 0


# ---- end-to-end import with mixed OS builds

def _agent_by_detected(cfg_items):
    return None


def test_import_mixed_os_builds(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path / "root")
    d = tmp_path / "multibuild"; d.mkdir()
    _write(d / "mytool", ELF)          # linux native
    _write(d / "mytool.exe", PE)       # windows foreign
    _write(d / "mytool-mac", MACHO)    # macos foreign

    def fake(c, tool_name, help_head, entries, vocab, log=None):
        return ImportResult(description="x", tags=["web"], entrypoints=[
            EntryDecision(name=e["name"], runs_locally=e["detected_os"] == "linux",
                          target_os=e["detected_os"]) for e in entries])
    monkeypatch.setattr(core.agents, "classify_import", fake)
    # import all three as entrypoints
    monkeypatch.setattr(core, "_choose_entries",
                        lambda candidates, app, name, entry, yes: candidates)

    m = core.import_local(cfg, str(d), name="multibuild", assume_yes=True)
    # native gets exec shim, foreign get path shims
    assert not m.is_foreign("mytool")
    foreign = set(m.foreign_commands)
    assert len(foreign) == 2
    assert m.platform == "both"

    # the windows shim prints a path instead of executing
    winc = next(c for c in m.foreign_commands if "exe" in c)
    out = subprocess.run([str(cfg.bin_dir / winc)], capture_output=True, text=True)
    assert out.stdout.strip().endswith(".exe")
    assert out.returncode == 0


def test_import_fallback_without_agent(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    exe = tmp_path / "foo.exe"; _write(exe, PE)

    def boom(*a, **k):
        raise core.InstallError("agent down")
    monkeypatch.setattr(core.agents, "classify_import", boom)

    m = core.import_local(cfg, str(exe), name="foo", assume_yes=True)
    # detect_os fallback → windows → foreign path shim
    assert m.is_foreign("foo")
    out = subprocess.run([str(cfg.bin_dir / "foo")], capture_output=True, text=True)
    assert out.stdout.strip().endswith(".exe") and out.returncode == 0
