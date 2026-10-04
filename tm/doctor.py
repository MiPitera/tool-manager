"""`tm doctor` — health checks + help migrating the old scattered-binary mess."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from rich.table import Table

from tm import manifest, shims
from tm.config import Config
from tm.installers import apt
from tm.util import console


def doctor(cfg: Config) -> None:
    _check_path(cfg)
    _check_shims(cfg)
    _check_apt(cfg)
    _find_orphans(cfg)


def _check_path(cfg: Config) -> None:
    on_path = str(cfg.bin_dir) in os.environ.get("PATH", "").split(":")
    console.print(f"PATH contains {cfg.bin_dir}: " + ("[green]yes[/]" if on_path else "[red]no — run `tm setup`[/]"))
    try:
        sp = subprocess.run(["sudo", "-n", "sh", "-c", "echo $PATH"], capture_output=True, text=True, timeout=5)
        if sp.returncode == 0:
            ok = str(cfg.bin_dir) in sp.stdout
            console.print(f"sudo secure_path includes {cfg.bin_dir}: " + ("[green]yes[/]" if ok else "[yellow]no[/]"))
    except (subprocess.TimeoutExpired, OSError):
        pass


def _check_shims(cfg: Config) -> None:
    t = Table(title="tools", show_lines=False)
    t.add_column("name"); t.add_column("method"); t.add_column("commands"); t.add_column("status")
    for tool_dir, m in manifest.iter_all(cfg.root):
        issues = []
        for cmd, target in m.entrypoints.items():
            if m.method == "apt":
                if not shutil.which(cmd):
                    issues.append(f"{cmd} missing on PATH")
                continue
            if target.startswith("docker:"):
                continue
            if not (cfg.bin_dir / cmd).exists():
                issues.append(f"shim {cmd} missing")
            elif not Path(target).exists():
                issues.append(f"target of {cmd} gone")
        status = "[green]ok[/]" if not issues else "[red]" + "; ".join(issues) + "[/]"
        t.add_row(m.name, m.method, ", ".join(m.entrypoints) or "-", status)
    console.print(t)


def _check_apt(cfg: Config) -> None:
    gone = []
    for _, m in manifest.iter_all(cfg.root):
        if m.method == "apt" and not apt.version_installed(m.apt_package or m.name):
            gone.append(m.name)
    if gone:
        console.print(f"[yellow]apt packages in registry but not installed:[/] {', '.join(gone)} "
                      "(run `tm remove <name>` to drop the record)")


def _find_orphans(cfg: Config) -> None:
    """Unmanaged binaries in common dirs + uv tools — the old mess to migrate."""
    managed = {Path(t).resolve() for _, m in manifest.iter_all(cfg.root)
               for t in m.entrypoints.values() if not t.startswith("docker:")}
    dirs = [Path.home() / ".local/bin", Path.home() / "go/bin", Path("/usr/local/bin")]
    found = []
    for d in dirs:
        if not d.exists():
            continue
        for p in d.iterdir():
            if p.is_file() and os.access(p, os.X_OK) and p.resolve() not in managed \
                    and not shims.is_managed(p):
                found.append(str(p))
    if found:
        console.print(f"\n[bold]Unmanaged binaries[/] ({len(found)}) — pull in with "
                      "[cyan]tm import <path>[/] (or re-install via tm):")
        for f in found[:40]:
            console.print(f"  {f}")
        if len(found) > 40:
            console.print(f"  … and {len(found) - 40} more")
    if shutil.which("uv"):
        r = subprocess.run(["uv", "tool", "list"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            console.print("\n[bold]uv global tools[/] (from your old global-scope setup):")
            console.print(r.stdout.strip())
