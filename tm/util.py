"""Shared helpers: subprocess with logging, console."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from rich.console import Console

console = Console()
err = Console(stderr=True)


class InstallError(RuntimeError):
    pass


class Logger:
    """Appends everything to <tool>/install.log."""

    def __init__(self, path: Path | None):
        self.path = path

    def write(self, text: str) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(text.rstrip("\n") + "\n")


def run(cmd: list[str], log: Logger | None = None, cwd: Path | None = None,
        env: dict | None = None, check: bool = True, timeout: int | None = None,
        capture: bool = True) -> subprocess.CompletedProcess:
    if log:
        log.write(f"$ {shlex.join(cmd)}" + (f"   (cwd={cwd})" if cwd else ""))
    full_env = {**os.environ, **(env or {})}
    p = subprocess.run(cmd, cwd=cwd, env=full_env, text=True, timeout=timeout,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.STDOUT if capture else None)
    if log and capture:
        log.write(p.stdout or "")
    if check and p.returncode != 0:
        tail = "\n".join((p.stdout or "").splitlines()[-20:])
        raise InstallError(f"command failed ({p.returncode}): {shlex.join(cmd)}\n{tail}")
    return p


def confirm(question: str, default: bool = True, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    from rich.prompt import Confirm

    return Confirm.ask(question, default=default)
