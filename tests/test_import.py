"""Tests for `tm import` local-program import (no network; agent mocked)."""
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tm import core
from tm.config import Config
from tm.installers import archive
from tm.agents import TagBatch, TagItem


def _cfg(tmp_path) -> Config:
    cfg = Config(root=tmp_path, models={"tagger": "haiku"}, agents={"extra_args": []}, docker={})
    cfg.bin_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "tags.txt").write_text("web: web stuff\n")
    return cfg


def _elf(p: Path):
    # minimal "runs and prints" — use a shebang shell script that mimics a binary for smoke
    p.write_text("#!/bin/sh\necho ok\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)


@pytest.fixture(autouse=True)
def _no_agent(monkeypatch):
    from tm.agents import ImportResult, EntryDecision

    def fake(cfg, tool_name, help_head, entries, vocab, log=None):
        return ImportResult(
            description="desc", tags=["web"],
            entrypoints=[EntryDecision(name=e["name"],
                                       runs_locally=e["detected_os"] in ("", "linux"),
                                       target_os=e["detected_os"]) for e in entries])
    monkeypatch.setattr(core.agents, "classify_import", fake)


# ---- find_programs / shebang

def test_find_programs_detects_kinds(tmp_path):
    (tmp_path / "lib.so").write_bytes(b"\x7fELF\x00")
    (tmp_path / "data.json").write_text("{}")
    exe = tmp_path / "tool"; exe.write_text("#!/bin/sh\n"); exe.chmod(0o755)
    script = tmp_path / "run.py"; script.write_text("#!/usr/bin/env python3\nif __name__=='__main__':\n pass\n")
    noexec_py = tmp_path / "mod.py"; noexec_py.write_text("if __name__ == '__main__':\n pass\n")
    found = {p.name for p in archive.find_programs(tmp_path)}
    assert "tool" in found and "run.py" in found and "mod.py" in found
    assert "lib.so" not in found and "data.json" not in found


def test_shebang_interpreter(tmp_path):
    p = tmp_path / "s"; p.write_text("#!/usr/bin/env python3\n")
    assert archive.shebang_interpreter(p) == "python3"
    q = tmp_path / "q"; q.write_text("#!/bin/bash\n")
    assert archive.shebang_interpreter(q) == "/bin/bash"
    r = tmp_path / "r"; r.write_text("no shebang\n")
    assert archive.shebang_interpreter(r) == ""


# ---- entry selection

def test_choose_entry_explicit(tmp_path):
    a = tmp_path / "a"; a.write_text(""); b = tmp_path / "b"; b.write_text("")
    assert core._choose_entries([a, b], tmp_path, "x", "b", True) == [b]

def test_choose_entry_name_match(tmp_path):
    a = tmp_path / "a"; a.write_text(""); b = tmp_path / "mytool"; b.write_text("")
    assert core._choose_entries([a, b], tmp_path, "mytool", "", True) == [b]

def test_choose_entry_single(tmp_path):
    a = tmp_path / "a"; a.write_text("")
    assert core._choose_entries([a], tmp_path, "x", "", True) == [a]

def test_choose_entry_yes_first(tmp_path):
    a = tmp_path / "a"; a.write_text(""); b = tmp_path / "b"; b.write_text("")
    assert core._choose_entries([a, b], tmp_path, "x", "", True) == [a]

def test_choose_entry_bad_explicit(tmp_path):
    a = tmp_path / "a"; a.write_text("")
    with pytest.raises(core.InstallError):
        core._choose_entries([a], tmp_path, "x", "nope", True)


# ---- end to end: file import

def test_import_file_moves_and_shims(tmp_path):
    cfg = _cfg(tmp_path / "root")
    prog = tmp_path / "myprog"
    _elf(prog)
    m = core.import_local(cfg, str(prog), assume_yes=True)
    assert m.method == "imported" and m.name == "myprog"
    assert not prog.exists()  # moved
    target = cfg.tool_dir("myprog") / "app" / "myprog"
    assert target.exists()
    shim = cfg.bin_dir / "myprog"
    assert shim.exists()
    assert str(target.resolve()) in shim.read_text()  # absolute path -> sudo-safe
    assert m.tags == ["web"] and m.description == "desc"


def test_import_copy_keeps_original(tmp_path):
    cfg = _cfg(tmp_path / "root")
    prog = tmp_path / "keepme"; _elf(prog)
    core.import_local(cfg, str(prog), copy=True, assume_yes=True)
    assert prog.exists()  # original retained
    assert (cfg.tool_dir("keepme") / "app" / "keepme").exists()


def test_import_dir_detects_entry(tmp_path):
    cfg = _cfg(tmp_path / "root")
    d = tmp_path / "bundle"; d.mkdir()
    _elf(d / "bundle")  # name-matching binary
    (d / "README.md").write_text("docs")
    (d / "lib.so").write_bytes(b"\x7fELF")
    m = core.import_local(cfg, str(d), assume_yes=True)
    assert set(m.entrypoints) == {"bundle"}


def test_import_script_uses_interpreter(tmp_path):
    cfg = _cfg(tmp_path / "root")
    s = tmp_path / "scr.py"
    s.write_text("#!/usr/bin/env python3\nif __name__=='__main__':\n print('hi')\n")  # no +x
    m = core.import_local(cfg, str(s), name="scr", assume_yes=True)
    shim = (cfg.bin_dir / "scr").read_text()
    assert "python" in shim  # interpreter wired in
    assert m.language == "python"


def test_import_name_conflict(tmp_path):
    cfg = _cfg(tmp_path / "root")
    p1 = tmp_path / "dup"; _elf(p1)
    core.import_local(cfg, str(p1), assume_yes=True)
    p2 = tmp_path / "dup2"; _elf(p2)
    with pytest.raises(core.InstallError):
        core.import_local(cfg, str(p2), name="dup", assume_yes=True)


def test_import_no_runnable(tmp_path):
    cfg = _cfg(tmp_path / "root")
    d = tmp_path / "empty"; d.mkdir()
    (d / "notes.txt").write_text("hello")
    with pytest.raises(core.InstallError):
        core.import_local(cfg, str(d), assume_yes=True)
    assert not cfg.tool_dir("empty").exists()  # cleaned up
