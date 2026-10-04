"""Paths and user configuration (~/tools/config.toml)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = CODE_DIR / "prompts"

DEFAULT_CONFIG = """\
# tm configuration

[models]
classifier = "sonnet"
builder = "sonnet"
builder_escalation = "opus"
tagger = "haiku"

[agents]
builder_max_turns = 40
# how many tags the tagger should aim for per tool (tags are single words)
target_tags = 3
# extra flags passed to every `claude -p` call
extra_args = ["--strict-mcp-config", "--no-session-persistence"]

[docker]
# -it/-i chosen automatically by the shim
run_flags = ["--rm", "--network", "host", "-v", "$PWD:/work", "-w", "/work"]
"""


@dataclass
class Config:
    root: Path
    models: dict = field(default_factory=dict)
    agents: dict = field(default_factory=dict)
    docker: dict = field(default_factory=dict)

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def python_dir(self) -> Path:
        return self.root / ".python"

    @property
    def db_path(self) -> Path:
        return self.root / "registry.db"

    @property
    def tags_file(self) -> Path:
        return self.root / "tags.txt"

    def tool_dir(self, name: str) -> Path:
        return self.root / name

    def model(self, role: str) -> str:
        return self.models.get(role, "sonnet")


def tools_root() -> Path:
    return Path(os.environ.get("TM_ROOT", Path.home() / "tools")).resolve()


def load() -> Config:
    root = tools_root()
    path = root / "config.toml"
    data = tomllib.loads(path.read_text() if path.exists() else DEFAULT_CONFIG)
    defaults = tomllib.loads(DEFAULT_CONFIG)
    merged = {k: {**defaults.get(k, {}), **data.get(k, {})} for k in defaults}
    return Config(root=root, **merged)


def ensure_layout(cfg: Config) -> None:
    cfg.root.mkdir(parents=True, exist_ok=True)
    cfg.bin_dir.mkdir(exist_ok=True)
    cfg.python_dir.mkdir(exist_ok=True)
    conf = cfg.root / "config.toml"
    if not conf.exists():
        conf.write_text(DEFAULT_CONFIG)
    if not cfg.tags_file.exists():
        from tm.tags import DEFAULT_TAGS

        cfg.tags_file.write_text(DEFAULT_TAGS)
