"""release_binary / release_windows: download prebuilt assets from the latest GitHub release."""
from __future__ import annotations

import platform
import re
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path

from tm.installers.common import Job, clone, download, is_elf
from tm.util import InstallError

ARCH_ALIASES = {
    "x86_64": ["x86_64", "amd64", "x64", "64bit", "64-bit"],
    "aarch64": ["aarch64", "arm64", "armv8"],
    "armv7l": ["armv7", "armhf", "arm"],
    "i686": ["i386", "i686", "x86", "32bit"],
}
SKIP_RE = re.compile(r"\.(sha\d*|sig|asc|pem|sbom|txt|md|json|ya?ml|deb|rpm|apk|dmg|pkg|msi)$|checksum", re.I)
OTHER_OS = re.compile(r"darwin|macos|osx|apple|freebsd|openbsd|netbsd|android|ios", re.I)
WIN_RE = re.compile(r"windows|win32|win64|\.exe$|-win[-_.]", re.I)


def machine() -> str:
    return platform.machine().lower()


def score_linux(name: str, arch: str) -> int:
    n = name.lower()
    if SKIP_RE.search(n) or OTHER_OS.search(n) or WIN_RE.search(n):
        return -1
    s = 0
    if "linux" in n:
        s += 10
    aliases = ARCH_ALIASES.get(arch, [arch])
    if any(a in n for a in aliases):
        s += 8
    elif any(a in n for k, v in ARCH_ALIASES.items() if k != arch for a in v):
        return -1
    if "musl" in n or "static" in n:
        s += 2  # fewer runtime deps
    if n.endswith((".tar.gz", ".tgz", ".tar.xz", ".zip", ".gz")):
        s += 1
    return s


def pick_linux_asset(assets: list[dict], arch: str, preferred: str = "") -> dict | None:
    if preferred:
        for a in assets:
            if a["name"] == preferred:
                return a
    scored = sorted(((score_linux(a["name"], arch), a) for a in assets), key=lambda x: -x[0])
    return scored[0][1] if scored and scored[0][0] > 0 else None


def extract(archive: Path, dest: Path) -> Path:
    d