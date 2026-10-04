"""Pure-logic unit tests (no network, no claude). Run: uv run --with pytest ... pytest"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tm import tags as tagmod
from tm.installers import archive
from tm import shims
from tm.manifest import Manifest
from tm.registry import Registry
from tm.github import parse_url, extract_sections


# ---- tags

def test_tags_reject_origin():
    assert tagmod.clean(["python", "Docker", "Recon", "web", "apt"]) == ["recon", "web"]

def test_tags_normalize_dedupe():
    assert tagmod.clean(["Password Cracking", "password-cracking", "AD"]) == ["password-cracking", "ad"]

def test_tag_merge_user_wins_over_removal():
    # user removed an auto tag; it stays gone until re-added
    assert tagmod.merge(["web", "recon"], [], ["recon"]) == ["web"]
    assert tagmod.merge(["web", "recon"], ["recon"], ["recon"]) == ["recon", "web"]


# ---- manifest tag ownership

def test_manifest_remove_then_retag_keeps_removed():
    m = Manifest(name="x", source="s", method="go_install")
    m.tags_auto = ["web", "recon"]
    m.remove_tags(["recon"])
    assert "recon" not in m.tags
    m.tags_auto = ["web", "recon", "dns"]  # simulate retag
    assert "recon" not in m.tags and "dns" in m.tags

def test_manifest_set_tags_suppresses_auto():
    m = Manifest(name="x", source="s", method="go_install")
    m.tags_auto = ["web", "recon"]
    m.set_tags(["custom"])
    assert m.tags == ["custom"]


# ---- release asset selection

ASSETS = [
    {"name": "tool_1.0_linux_amd64.tar.gz", "url": "u", "size": 1},
    {"name": "tool_1.0_linux_arm64.tar.gz", "url": "u", "size": 1},
    {"name": "tool_1.0_windows_amd64.zip", "url": "u", "size": 1},
    {"name": "tool_1.0_darwin_amd64.tar.gz", "url": "u", "size": 1},
    {"name": "checksums.txt", "url": "u", "size": 1},
    {"name": "tool_1.0_linux_amd64.tar.gz.sha256", "url": "u", "size": 1},
]

def test_pick_linux_asset(monkeypatch):
    monkeypatch.setattr(archive.platform, "machine", lambda: "x86_64")
    a = archive.pick_asset(ASSETS, want_windows=False)
    assert a["name"] == "tool_1.0_linux_amd64.tar.gz"

def test_pick_windows_asset():
    a = archive.pick_asset(ASSETS, want_windows=True)
    assert a["name"] == "tool_1.0_windows_amd64.zip"

def test_pick_arm(monkeypatch):
    monkeypatch.setattr(archive.platform, "machine", lambda: "aarch64")
    a = archive.pick_asset(ASSETS, want_windows=False)
    assert "arm64" in a["name"]

def test_no_suitable_asset():
    with pytest.raises(archive.InstallError):
        archive.pick_asset([{"name": "checksums.txt", "url": "u", "size": 1}], want_windows=False)

def test_checksum_never_picked(monkeypatch):
    monkeypatch.setattr(archive.platform, "machine", lambda: "x86_64")
    a = archive.pick_asset(ASSETS, want_windows=False)
    assert not a["name"].endswith(".sha256")


# ---- shims

def test_exec_shim_absolute(tmp_path):
    target = tmp_path / "prog.py"
    target.write_text("print()")
    interp = tmp_path / "python"
    interp.write_text("")
    bin_dir = tmp_path / "bin"
    p = shims.exec_shim(bin_dir, "prog", "mytool", target, interpreter=interp)
    body = p.read_text()
    assert str(target.resolve()) in body
    assert str(interp.resolve()) in body
    assert shims.owner_tool(p) == "mytool"
    assert body.startswith("#!/bin/sh")

def test_docker_shim_tty_and_pwd(tmp_path):
    p = shims.docker_shim(tmp_path / "bin", "tool", "tool", "repo/tool:latest",
                          ["--rm", "-v", "$PWD:/work", "-w", "/work"])
    body = p.read_text()
    assert "repo/tool:latest" in body and '"$@"' in body
    assert '"$PWD:/work"' in body
    assert "TM_TTY" in body

def test_shim_conflict_detection(tmp_path):
    bin_dir = tmp_path / "bin"
    t = tmp_path / "t"; t.write_text("")
    shims.exec_shim(bin_dir, "x", "toolA", t)
    shims.check_free(bin_dir, "x", "toolA")  # same owner ok
    with pytest.raises(FileExistsError):
        shims.check_free(bin_dir, "x", "toolB")

def test_remove_for(tmp_path):
    bin_dir = tmp_path / "bin"
    t = tmp_path / "t"; t.write_text("")
    shims.exec_shim(bin_dir, "a", "tool", t)
    shims.exec_shim(bin_dir, "b", "tool", t)
    assert set(shims.remove_for(bin_dir, "tool")) == {"a", "b"}


# ---- registry / FTS

def test_registry_search(tmp_path):
    reg = Registry(tmp_path / "r.db")
    m = Manifest(name="ffuf", source="s", method="release_binary", description="web fuzzer")
    m.tags_auto = ["web", "fuzzing"]
    reg.upsert(m)
    m2 = Manifest(name="rubeus", source="s", method="release_windows", platform="windows",
                  description="kerberos abuse")
    m2.tags_auto = ["ad", "kerberos", "windows"]
    reg.upsert(m2)
    assert [r["name"] for r in reg.search(query="fuzz")] == ["ffuf"]
    assert [r["name"] for r in reg.search(tags=["windows"])] == ["rubeus"]
    assert [r["name"] for r in reg.search(tags=["ad", "kerberos"])] == ["rubeus"]
    assert reg.search(tags=["ad", "web"]) == []
    assert [r["name"] for r in reg.search(method="release_binary")] == ["ffuf"]
    reg.close()

def test_registry_upsert_replaces(tmp_path):
    reg = Registry(tmp_path / "r.db")
    m = Manifest(name="x", source="s", method="apt", description="old")
    reg.upsert(m)
    m.description = "new"
    reg.upsert(m)
    rows = reg.search()
    assert len(rows) == 1 and rows[0]["description"] == "new"
    reg.close()


# ---- github helpers

def test_parse_url_forms():
    assert parse_url("https://github.com/foo/bar") == ("foo", "bar")
    assert parse_url("https://github.com/foo/bar.git") == ("foo", "bar")
    assert parse_url("foo/bar") == ("foo", "bar")
    assert parse_url("git@github.com:foo/bar.git") == ("foo", "bar")

def test_extract_sections():
    md = "# Title\nintro\n## Installation\nrun make\n## Something\nnoise\n## Usage\ntool -h\n"
    out = extract_sections(md)
    assert "run make" in out and "tool -h" in out and "noise" not in out
