"""--static standalone build + multi-OS artifacts (build + network mocked)."""
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tm import core, shims
from tm.agents import Classification
from tm.config import Config
from tm.installers import archive


def _cfg(tmp_path) -> Config:
    cfg = Config(root=tmp_path, models={"classifier": "sonnet", "tagger": "haiku"},
                 agents={"extra_args": [], "target_tags": 3}, docker={})
    cfg.bin_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "tags.txt").write_text("recon: recon\n")
    return cfg


def _exe(p: Path, data: bytes = b"\x7fELF"):
    p.write_bytes(data)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)


# ---- multipath_shim

def test_multipath_shim_prints_all(tmp_path):
    a = tmp_path / "dist" / "lin"; a.parent.mkdir(); _exe(a)
    b = tmp_path / "rel" / "x.exe"; b.parent.mkdir(); b.write_bytes(b"MZ")
    p = shims.multipath_shim(tmp_path / "bin", "lazagne", "lazagne",
                             {"linux": str(a), "windows": str(b)})
    assert shims.owner_tool(p) == "lazagne"
    out = subprocess.run([str(p)], capture_output=True, text=True)
    assert out.returncode == 0
    printed = out.stdout.strip().splitlines()
    assert str(a.resolve()) in printed and str(b.resolve()) in printed  # all paths on stdout
    assert "linux" in out.stderr and "windows" in out.stderr            # labels on stderr


# ---- pick_assets_by_os

def test_pick_assets_by_os(tmp_path):
    assets = [
        {"name": "lazagne.exe", "url": "u1", "size": 1},
        {"name": "tool_linux_amd64.tar.gz", "url": "u2", "size": 1},
    ]
    got = archive.pick_assets_by_os(assets, {"windows": "lazagne.exe", "linux": ""})
    assert got["windows"]["name"] == "lazagne.exe"
    assert got["linux"]["name"] == "tool_linux_amd64.tar.gz"


# ---- end-to-end static install (mock classifier, build + release download)

def test_static_install_builds_and_fetches(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path / "root")
    url = "AlessandroZ/LaZagne"

    cls = Classification(method="uv_project", language="python", description="creds",
                         tags=["recon"], static_recommended=True, static_tool="pyinstaller",
                         static_entrypoints=["Linux/laZagne.py"],
                         release_assets_by_os={"windows": "lazagne.exe"})
    monkeypatch.setattr(core.agents, "classify", lambda *a, **k: cls)

    # fake repo context with a windows release asset
    class Ctx:
        release_tag = "v1.0"
        release_assets = [{"name": "lazagne.exe", "url": "http://x/lazagne.exe", "size": 10}]
        def as_prompt(self): return ""
    monkeypatch.setattr(core.github, "gather", lambda u: Ctx())

    # mock the static builder to avoid real clone/pyinstaller/network
    def fake_static(cfg_, name, tool_dir, url_, tool, entry_rel, release_assets, release_by_os, log, extra_args=None):
        dist = tool_dir / "dist"; dist.mkdir(parents=True)
        lin = dist / "lazagne"; _exe(lin)
        rel = tool_dir / "release-windows"; rel.mkdir(parents=True)
        win = rel / "lazagne.exe"; win.write_bytes(b"MZ")
        assert tool == "pyinstaller" and entry_rel == ["Linux/laZagne.py"]
        assert release_by_os == {"windows": "lazagne.exe"}
        return {"linux": str(lin.resolve()), "windows": str(win.resolve())}
    monkeypatch.setattr(core.methods, "install_static", fake_static)

    m = core.install_from_github(cfg, url, assume_yes=True)
    assert m.method == "static"
    assert set(m.artifacts) == {"linux", "windows"}
    assert m.platform == "both"
    # single foreign command that prints all artifact paths
    assert m.is_foreign("lazagne")
    out = subprocess.run([str(cfg.bin_dir / "lazagne")], capture_output=True, text=True)
    assert out.returncode == 0
    assert m.artifacts["windows"] in out.stdout and m.artifacts["linux"] in out.stdout


def test_static_autodetect_without_flag(tmp_path, monkeypatch):
    """static_recommended from the classifier triggers the static path without --static."""
    cfg = _cfg(tmp_path / "root")
    cls = Classification(method="uv_project", description="x", static_recommended=True)
    monkeypatch.setattr(core.agents, "classify", lambda *a, **k: cls)

    class Ctx:
        release_tag = ""
        release_assets = []
        def as_prompt(self): return ""
    monkeypatch.setattr(core.github, "gather", lambda u: Ctx())

    called = {}
    def fake_static(*a, **k):
        called["yes"] = True
        d = a[2] / "dist"; d.mkdir(parents=True)
        b = d / "tool"; _exe(b)
        return {"linux": str(b.resolve())}
    monkeypatch.setattr(core.methods, "install_static", fake_static)

    m = core.install_from_github(cfg, "owner/tool", assume_yes=True)
    assert called.get("yes") and m.method == "static"


def test_method_override_skips_static(tmp_path, monkeypatch):
    """An explicit --method must win over auto static detection."""
    cfg = _cfg(tmp_path / "root")
    cls = Classification(method="uv_project", description="x", static_recommended=True)
    monkeypatch.setattr(core.agents, "classify", lambda *a, **k: cls)

    class Ctx:
        release_tag = ""
        release_assets = []
        def as_prompt(self): return ""
    monkeypatch.setattr(core.github, "gather", lambda u: Ctx())
    monkeypatch.setattr(core.methods, "install_static",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))
    # force a method → static must be skipped; stub the normal runner
    monkeypatch.setattr(core, "_run_method", lambda *a, **k: {"tool": "/x"})
    m = core.install_from_github(cfg, "owner/tool", method="uv_project", assume_yes=True)
    assert m.method == "uv_project"


def test_clone_url_normalization():
    from tm.installers.methods import _clone_url
    assert _clone_url("AlessandroZ/LaZagne") == "https://github.com/AlessandroZ/LaZagne.git"
    assert _clone_url("https://github.com/x/y") == "https://github.com/x/y"
    assert _clone_url("https://github.com/x/y.git") == "https://github.com/x/y.git"
    assert _clone_url("git@github.com:x/y.git") == "git@github.com:x/y.git"
    assert _clone_url("/tmp/local/repo") == "/tmp/local/repo"
