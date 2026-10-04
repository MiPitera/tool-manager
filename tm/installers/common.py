"""State shared by all installers."""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from tm.agents import Classification
from tm.config import Config
from tm.github import RepoContext
from tm.util import InstallError, Logger, run


@dataclass
class Job:
    cfg: Config
    name: str
    url: str
    repo: RepoContext | None
    plan: Classification
    log: Logger
    assume_yes: bool = False
    # filled by installers: command name -> (target, interpreter or None)
    commands: dict[str, tuple[Path, Path | None]] = field(default_factory=dict)
    docker_image: str = ""
    version: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def dir(self) -> Path:
        return self.cfg.tool_dir(self.name)

    @property
    def src(self) -> Path:
        return self.dir / "src"


def clone(job: Job, dest: Path | None = None) -> Path:
    dest = dest or job.src
    if (dest / ".git").exists():
        run(["git", "-C", str(dest), "pull", "--ff-only"], job.log, check=False)
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    run(["git", "clone", "--depth", "1", "--recurse-submodules", "--shallow-submodules", job.url, str(dest)], job.log)
    rev = run(["git", "-C", str(dest), "rev-parse", "--short", "HEAD"], job.log, check=False)
    job.version = job.version or (rev.stdout or "").strip()
    return dest


def download(url: str, dest_dir: Path, log: Logger) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = url.rstrip("/").rsplit("/", 1)[-1]
    out = dest_dir / name
    log.write(f"download {url} -> {out}")
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
        if r.status_code != 200:
            raise InstallError(f"download failed ({r.status_code}): {url}")
        with out.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return out


def is_elf(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False
