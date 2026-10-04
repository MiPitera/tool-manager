"""Shell completion: command list + dynamic tool-name completion."""
import sys
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tm import cli, manifest
from tm.cli import app, _complete_tool
from tm.config import Config
from tm.manifest import Manifest
from tm.registry import Registry

runner = CliRunner()


def _seed(root: Path):
    cfg = Config(root=root, models={}, agents={}, docker={})
    cfg.bin_dir.mkdir(parents=True, exist_ok=True)
    for nm, desc in [("ffuf", "web fuzzer"), ("nuclei", "scanner"), ("ffmpeg", "media")]:
        m = Manifest(name=nm, source="s", method="release_binary", description=desc)
        manifest.save(cfg.tool_dir(nm), m)
        reg = Registry(cfg.db_path); reg.upsert(m); reg.close()
    return cfg


def test_complete_tool_prefix(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setenv("TM_ROOT", str(tmp_path))
    assert sorted(_complete_tool("ff")) == ["ffmpeg", "ffuf"]
    assert _complete_tool("nu") == ["nuclei"]
    assert _complete_tool("zzz") == []


def test_complete_tool_no_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_ROOT", str(tmp_path / "empty"))
    # must not raise and must not create anything
    assert _complete_tool("x") == []
    assert not (tmp_path / "empty").exists()


def test_completion_flag_available():
    # Typer exposes --install-completion once add_completion is on
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    assert "--install-completion" in r.output
