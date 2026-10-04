"""Tests for `tm forget` and the single-word / target-count tag rule."""
import stat
import sys
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tm import agents, core, manifest
from tm.agents import TagBatch, TagItem
from tm.cli import app
from tm.config import Config
from tm.registry import Registry

runner = CliRunner()


def _seed_tool(root: Path, monkeypatch) -> Config:
    cfg = Config(root=root, models={"tagger": "haiku"},
                 agents={"extra_args": [], "target_tags": 3}, docker={})
    cfg.bin_dir.mkdir(parents=True, exist_ok=True)
    (root / "tags.txt").write_text("web: web stuff\n")
    monkeypatch.setattr(core.agents, "describe_local",
                        lambda c, items, vocab, log: TagBatch(
                            items=[TagItem(name=items[0]["name"], description="d", tags=["web"])]))
    prog = root.parent / "prog"
    prog.write_text("#!/bin/sh\necho ok\n")
    prog.chmod(prog.stat().st_mode | stat.S_IXUSR)
    core.import_local(cfg, str(prog), name="prog", assume_yes=True)
    return cfg


def test_forget_keeps_files_and_shim(tmp_path, monkeypatch):
    root = tmp_path / "root"
    cfg = _seed_tool(root, monkeypatch)
    monkeypatch.setenv("TM_ROOT", str(root))

    r = runner.invoke(app, ["forget", "prog", "--yes"])
    assert r.exit_code == 0, r.output

    # registry entry gone
    reg = Registry(cfg.db_path)
    assert reg.search() == []
    reg.close()
    # manifest gone -> not tracked, reindex won't re-add
    assert not manifest.path_for(cfg.tool_dir("prog")).exists()
    # but the program and its shim remain and still run
    assert (cfg.tool_dir("prog") / "app" / "prog").exists()
    shim = cfg.bin_dir / "prog"
    assert shim.exists()


def test_forget_then_reindex_does_not_readd(tmp_path, monkeypatch):
    root = tmp_path / "root"
    cfg = _seed_tool(root, monkeypatch)
    monkeypatch.setenv("TM_ROOT", str(root))
    runner.invoke(app, ["forget", "prog", "--yes"])
    r = runner.invoke(app, ["reindex"])
    assert r.exit_code == 0
    reg = Registry(cfg.db_path)
    assert reg.search() == []
    reg.close()


def test_remove_still_deletes_everything(tmp_path, monkeypatch):
    root = tmp_path / "root"
    cfg = _seed_tool(root, monkeypatch)
    monkeypatch.setenv("TM_ROOT", str(root))
    r = runner.invoke(app, ["remove", "prog", "--yes"])
    assert r.exit_code == 0
    assert not cfg.tool_dir("prog").exists()
    assert not (cfg.bin_dir / "prog").exists()


def test_tag_rule_mentions_target(monkeypatch):
    cfg = Config(root=Path("/tmp"), models={}, agents={"target_tags": 4}, docker={})
    rule = agents._tag_rule(cfg)
    assert "SINGLE" in rule and "4" in rule


def test_tags_single_token():
    from tm import tags as tagmod
    # spaces collapse to a single hyphenated token; no tag ever contains a space
    out = tagmod.clean(["web app", "recon", "Password Cracking"])
    assert all(" " not in t for t in out)
    assert out == ["web-app", "recon", "password-cracking"]
