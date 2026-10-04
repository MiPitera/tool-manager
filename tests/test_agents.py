"""Agent wrapper tests with a fake `claude` binary on PATH (no real model calls)."""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tm import agents
from tm.config import Config
from tm.util import InstallError, Logger


def _fake_claude(tmp_path: Path, result: dict, structured: bool = True, is_error: bool = False) -> Path:
    """Create a fake `claude` that echoes a result wrapper regardless of args."""
    out = {"is_error": is_error}
    if structured:
        out["structured_output"] = result
    out["result"] = json.dumps(result)
    payload = json.dumps(out).replace("'", "'\\''")
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\ncat >/dev/null\nprintf '%s' '{payload}'\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _cfg(tmp_path) -> Config:
    return Config(root=tmp_path, models={"classifier": "sonnet", "tagger": "haiku"},
                  agents={"extra_args": []}, docker={})


def test_classify_parses_structured(tmp_path, monkeypatch):
    _fake_claude(tmp_path, {
        "method": "release_binary", "language": "go", "platform": "linux",
        "description": "a fuzzer", "tags": ["web", "python", "docker"],  # origin tags present
        "confidence": 0.9,
    })
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    cls = agents.classify(_cfg(tmp_path), "ctx", "vocab", "", Logger(None))
    assert cls.method == "release_binary"
    # the agent returned forbidden tags; cleaning happens at the manifest layer,
    # but Classification keeps raw — core cleans. Here just assert raw passthrough:
    assert "web" in cls.tags


def test_classify_falls_back_to_result_field(tmp_path, monkeypatch):
    _fake_claude(tmp_path, {"method": "docker", "description": "x"}, structured=False)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    cls = agents.classify(_cfg(tmp_path), "ctx", "vocab", "", Logger(None))
    assert cls.method == "docker"


def test_agent_error_raises(tmp_path, monkeypatch):
    _fake_claude(tmp_path, {"method": "docker", "description": "x"}, is_error=True)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    with pytest.raises(InstallError):
        agents.classify(_cfg(tmp_path), "ctx", "vocab", "", Logger(None))


def test_missing_claude_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # no claude here
    with pytest.raises(InstallError):
        agents.classify(_cfg(tmp_path), "ctx", "vocab", "", Logger(None))


def test_tagger_batch(tmp_path, monkeypatch):
    _fake_claude(tmp_path, {
        "items": [{"name": "nmap", "description": "network scanner", "tags": ["networking"]}],
        "new_tags": {},
    })
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    tb = agents.tag_packages(_cfg(tmp_path), [{"name": "nmap"}], "vocab", Logger(None))
    assert tb.items[0].name == "nmap" and "networking" in tb.items[0].tags
