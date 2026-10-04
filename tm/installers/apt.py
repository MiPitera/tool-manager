"""apt support: install/catalog Debian packages, no shims (binaries already on PATH)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tm.util import InstallError, Logger, run

BASE_PRIORITIES = {"required", "important", "standard"}
EXE_DIRS = ("/usr/bin/", "/usr/sbin/", "/bin/", "/sbin/", "/usr/games/", "/usr/local/bin/")


def apt_install(pkg: str, log: Logger, assume_yes: bool = False) -> None:
    run(["sudo", "apt-get", "install", "-y", pkg], log=log)


def apt_remove(pkg: str, log: Logger) -> None:
    run(["sudo", "apt-get", "remove", "-y", pkg], log=log)


def apt_upgrade(pkg: str, log: Logger) -> None:
    run(["sudo", "apt-get", "install", "-y", "--only-upgrade", pkg], log=log)


def pkg_exists(pkg: str) -> bool:
    p = subprocess.run(["apt-cache", "show", pkg], capture_output=True, text=True)
    return p.returncode == 0 and bool(p.stdout.strip())


def pkg_metadata(pkg: str) -> dict:
    """description, section, version, binaries from apt-cache + dpkg."""
    out = subprocess.run(["apt-cache", "show", pkg], capture_output=True, text=True).stdout
    fields = {}
    desc_lines, in_desc = [], False
    for line in out.splitlines():
        if line.startswith("Description") and ":" in line:
            in_desc = True
            desc_lines.append(line.split(":", 1)[1].strip())
            continue
        if in_desc:
            if line.startswith(" "):
                desc_lines.append(line.strip())
                continue
            in_desc = False
        m = re.match(r"^([A-Za-z-]+):\s*(.*)$", line)
        if m and m.group(1) not in fields:
            fields[m.group(1)] = m.group(2)
        if line.strip() == "" and fields:
            break
    short = desc_lines[0] if desc_lines else (fields.get("Description", ""))
    return {
        "name": pkg,
        "description": short,
        "section": fields.get("Section", ""),
        "version": fields.get("Version", ""),
        "binaries": list_binaries(pkg),
    }


def list_binaries(pkg: str) -> list[str]:
    p = subprocess.run(["dpkg", "-L", pkg], capture_output=True, text=True)
    if p.returncode != 0:
        return []
    out = []
    for line in p.stdout.splitlines():
        if any(line.startswith(d) for d in EXE_DIRS) and "/" in line:
            name = Path(line).name
            if name and name not in out:
                out.append(name)
    return out


def version_installed(pkg: str) -> str:
    p = subprocess.run(["dpkg-query", "-W", "-f=${Version}", pkg], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def manual_packages() -> list[str]:
    p = subprocess.run(["apt-mark", "showmanual"], capture_output=True, text=True)
    if p.returncode != 0:
        raise InstallError("apt-mark not available")
    return [x for x in p.stdout.split() if x]


def priority_of(pkg: str) -> str:
    p = subprocess.run(["dpkg-query", "-W", "-f=${Priority}", pkg], capture_output=True, text=True)
    return p.stdout.strip().lower() if p.returncode == 0 else ""


def interesting_packages() -> list[str]:
    """Manual packages minus base-system ones and meta/task packages."""
    out = []
    for pkg in manual_packages():
        if pkg.startswith(("kali-", "task-", "lib")) and pkg.startswith(("kali-", "task-")):
            continue
        if priority_of(pkg) in BASE_PRIORITIES:
            continue
        out.append(pkg)
    return out
