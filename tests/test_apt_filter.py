"""apt: only packages that ship a runnable program get cataloged."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tm import core, doctor, manifest
from tm.agents import TagBatch, TagItem
from tm.config import Config
from tm.installers import apt
from tm.manifest import Manifest
from tm.registry import Registry


def _cfg(tmp_path) -> Config:
    cfg = Config(root=tmp_path, models={"tagger": "haiku"},
                 agents={"extra_args": [], "target_tags": 3}, docker={})
    cfg.bin_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "tags.txt").write_text("web: web stuff\n")
    return cfg


# binaries by package used across the module
BINS = {
    "nmap": ["nmap", "ncat"],
    "libreoffice": ["libreoffice", "soffice"],
    "libobasis25.2-librelogo": [],      # library component — no program
    "python3-requests": [],             # library — no program
    "fonts-dejavu": ["mkfontscale"],    # excluded by name prefix anyway
}


def _patch_apt(monkeypatch, manual, priorities=None):
    priorities = priorities or {}
    monkeypatch.setattr(apt, "manual_packages", lambda: manual)
    monkeypatch.setattr(apt, "priority_of", lambda p: priorities.get(p, "optional"))
    monkeypatch.setattr(apt, "list_binaries", lambda p: BINS.get(p, []))


def test_interesting_packages_filters_non_programs(monkeypatch):
    manual = ["nmap", "libreoffice", "libobasis25.2-librelogo", "python3-requests",
              "fonts-dejavu", "libfoo-dev", "bash"]
    _patch_apt(monkeypatch, manual, priorities={"bash": "required"})
    out = apt.interesting_packages()
    assert "nmap" in out
    assert "libreoffice" in out                    # starts with "lib" but HAS a program
    assert "libobasis25.2-librelogo" not in out    # the reported bug
    assert "python3-requests" not in out           # library, no program
    assert "fonts-dejavu" not in out               # name pre-filter
    assert "libfoo-dev" not in out                 # -dev suffix
    assert "bash" not in out                        # base priority


def test_has_program(monkeypatch):
    monkeypatch.setattr(apt, "list_binaries", lambda p: BINS.get(p, []))
    assert apt.has_program("nmap")
    assert not apt.has_program("libobasis25.2-librelogo")


def test_import_apt_skips_non_programs(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(apt, "interesting_packages", lambda: ["nmap", "python3-requests"])
    monkeypatch.setattr(apt, "version_installed", lambda p: "1.0")
    monkeypatch.setattr(apt, "pkg_metadata", lambda p: {
        "name": p, "description": "d", "section": "", "version": "1.0", "binaries": BINS.get(p, [])})
    monkeypatch.setattr(core.agents, "tag_packages",
                        lambda c, metas, vocab, log=None: TagBatch(
                            items=[TagItem(name=mt["name"], description="d", tags=["web"]) for mt in metas]))
    n = core.import_apt(cfg, assume_yes=True)
    assert n == 1  # only nmap
    names = {m.name for _, m in manifest.iter_all(cfg.root)}
    assert names == {"nmap"}


def test_install_apt_library_declined(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(apt, "pkg_exists", lambda p: True)
    monkeypatch.setattr(apt, "apt_install", lambda p, log, assume_yes=False: None)
    monkeypatch.setattr(apt, "version_installed", lambda p: "1.0")
    monkeypatch.setattr(apt, "pkg_metadata", lambda p: {
        "name": p, "description": "d", "section": "", "version": "1.0", "binaries": []})
    # allow the install prompt, decline the "catalog anyway?" prompt
    monkeypatch.setattr(core, "confirm", lambda q, *a, **k: "Catalog" not in q)
    res = core.install_apt(cfg, "python3-requests", assume_yes=False, force=False)
    assert res is None
    assert list(manifest.iter_all(cfg.root)) == []


def test_install_apt_library_forced(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(apt, "pkg_exists", lambda p: True)
    monkeypatch.setattr(apt, "apt_install", lambda p, log, assume_yes=False: None)
    monkeypatch.setattr(apt, "version_installed", lambda p: "1.0")
    monkeypatch.setattr(apt, "pkg_metadata", lambda p: {
        "name": p, "description": "lib", "section": "", "version": "1.0", "binaries": []})
    monkeypatch.setattr(core.agents, "tag_packages",
                        lambda c, metas, vocab, log=None: TagBatch(items=[]))
    m = core.install_apt(cfg, "python3-requests", assume_yes=True, force=True)
    assert m is not None and m.method == "apt"


def test_doctor_prune_drops_non_program_entry(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    # seed a bogus apt entry with no program
    bad = Manifest(name="libobasis25.2-librelogo", source="apt:libobasis25.2-librelogo",
                   method="apt", apt_package="libobasis25.2-librelogo", entrypoints={})
    manifest.save(cfg.tool_dir(bad.name), bad)
    reg = Registry(cfg.db_path); reg.upsert(bad); reg.close()

    monkeypatch.setattr(apt, "version_installed", lambda p: "1.0")  # still installed
    monkeypatch.setattr(apt, "has_program", lambda p: False)
    monkeypatch.setattr(doctor, "confirm", lambda *a, **k: True)
    # silence the other doctor sections' side effects is fine; just run prune
    doctor._check_apt_nonprograms(cfg, prune=True)

    assert not manifest.path_for(cfg.tool_dir(bad.name)).exists()
    reg = Registry(cfg.db_path)
    assert reg.search() == []
    reg.close()
