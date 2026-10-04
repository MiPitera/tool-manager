"""Per-tool manifest.json — the source of truth. registry.db is only an index."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from tm import tags as tagmod

Method = Literal[
    "release_binary", "release_windows", "uv_project", "uv_script",
    "go_install", "docker", "source_build", "apt", "imported",
]


class Recommended(BaseModel):
    found: bool = False
    quote: str = ""
    source_file: str = ""


class Manifest(BaseModel):
    name: str
    source: str  # repo URL or "apt:<pkg>"
    method: Method
    language: str = ""
    version: str = ""
    platform: Literal["linux", "windows", "both"] = "linux"
    description: str = ""
    entrypoints: dict[str, str] = Field(default_factory=dict)  # command name -> absolute target
    tags_auto: list[str] = Field(default_factory=list)
    tags_user: list[str] = Field(default_factory=list)
    tags_removed: list[str] = Field(default_factory=list)
    recommended_by_repo: Recommended = Field(default_factory=Recommended)
    deviation_reason: str = ""
    docker_image: str = ""
    docker_flags: list[str] = Field(default_factory=list)
    apt_package: str = ""
    apt_deps: list[str] = Field(default_factory=list)
    notes: str = ""
    installed_at: str = Field(default_factory=lambda: now())
    updated_at: str = Field(default_factory=lambda: now())

    @property
    def tags(self) -> list[str]:
        return tagmod.merge(self.tags_auto, self.tags_user, self.tags_removed)

    def add_user_tags(self, new: list[str]) -> None:
        for t in tagmod.clean(new):
            if t not in self.tags_user:
                self.tags_user.append(t)
            if t in self.tags_removed:
                self.tags_removed.remove(t)

    def remove_tags(self, rm: list[str]) -> None:
        for t in tagmod.clean(rm):
            if t in self.tags_user:
                self.tags_user.remove(t)
            if t in self.tags_auto and t not in self.tags_removed:
                self.tags_removed.append(t)

    def set_tags(self, new: list[str]) -> None:
        """Replace the whole tag set: everything becomes user-owned, auto tags not listed are suppressed."""
        new = tagmod.clean(new)
        self.tags_removed = [t for t in tagmod.clean(self.tags_auto) if t not in new]
        self.tags_user = new


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def path_for(tool_dir: Path) -> Path:
    return tool_dir / "manifest.json"


def load(tool_dir: Path) -> Manifest:
    return Manifest.model_validate_json(path_for(tool_dir).read_text())


def save(tool_dir: Path, m: Manifest) -> None:
    tool_dir.mkdir(parents=True, exist_ok=True)
    m.updated_at = now()
    tmp = path_for(tool_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(m.model_dump(), indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path_for(tool_dir))


def iter_all(root: Path):
    if not root.exists():
        return
    for d in sorted(root.iterdir()):
        if d.is_dir() and path_for(d).exists():
            yield d, load(d)
