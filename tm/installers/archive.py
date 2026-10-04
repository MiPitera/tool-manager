"""Download + extract release assets; pick the right asset for this OS/arch."""
from __future__ import annotations

import os
import platform
import re
import stat
import tarfile
import zipfile
from pathlib import Path

import httpx

from tm.util import InstallError, Logger


def _arch_aliases() -> list[str]:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return ["x86_64", "amd64", "x64", "64bit", "64-bit"]
    if m in ("aarch64", "arm64"):
        return ["aarch64", "arm64"]
    if m in ("armv7l", "armv7", "armhf"):
        return ["armv7", "armhf", "arm"]
    if m in ("i386", "i686", "x86"):
        return ["i386", "i686", "386", "x86", "32bit"]
    return [m]


LINUX_HINTS = ["linux", "gnu", "unknown-linux"]
WINDOWS_HINTS = ["windows", "win", ".exe", "win64", "win32"]
MUSL_PENALTY = ["musl"]
BAD_EXT = (".sha256", ".sha1", ".md5", ".sig", ".asc", ".pem", ".txt", ".sbom", ".json")


def score_asset(name: str, want_windows: bool) -> int:
    low = name.lower()
    if low.endswith(BAD_EXT):
        return -1000
    score = 0
    hints = WINDOWS_HINTS if want_windows else LINUX_HINTS
    other = LINUX_HINTS if want_windows else WINDOWS_HINTS
    if any(h in low for h in hints):
        score += 50
    if any(h in low for h in other):
        score -= 100
    if not want_windows:
        if any(a in low for a in _arch_aliases()):
            score += 40
        else:
            # penalize assets that clearly target another arch
            for other_arch in ("x86_64", "amd64", "aarch64", "arm64", "armv7", "i386", "386"):
                if other_arch in low and other_arch not in _arch_aliases():
                    score -= 60
                    break
        if any(p in low for p in MUSL_PENALTY):
            score -= 5  # prefer glibc on Debian, but still usable
    if low.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".zip")):
        score += 10
    if want_windows and low.endswith(".exe"):
        score += 20
    return score


def pick_asset(assets: list[dict], want_windows: bool, explicit: str = "") -> dict:
    if explicit:
        for a in assets:
            if a["name"] == explicit:
                return a
        raise InstallError(f"release asset '{explicit}' not found")
    ranked = sorted(assets, key=lambda a: score_asset(a["name"], want_windows), reverse=True)
    if not ranked or score_asset(ranked[0]["name"], want_windows) <= 0:
        raise InstallError("no suitable release asset for this platform; pass --method source_build or --name/asset")
    return ranked[0]


def download(url: str, dest: Path, log: Logger) -> Path:
    log.write(f"download {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return dest


def _safe_extract_tar(tf: tarfile.TarFile, dest: Path) -> None:
    dest = dest.resolve()
    for member in tf.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest)):
            raise InstallError(f"unsafe path in archive: {member.name}")
    tf.extractall(dest)


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    for member in zf.namelist():
        target = (dest / member).resolve()
        if not str(target).startswith(str(dest)):
            raise InstallError(f"unsafe path in archive: {member}")
    zf.extractall(dest)


def extract(archive: Path, dest: Path, log: Logger) -> bool:
    """Extract if it's an archive. Returns True if extracted, False if it's a plain binary."""
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")):
        with tarfile.open(archive) as tf:
            _safe_extract_tar(tf, dest)
        log.write(f"extracted {archive.name}")
        return True
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            _safe_extract_zip(zf, dest)
        log.write(f"extracted {archive.name}")
        return True
    if name.endswith(".gz") and not name.endswith(".tar.gz"):
        import gzip
        out = dest / archive.stem
        with gzip.open(archive) as src, out.open("wb") as dst:
            dst.write(src.read())
        log.write(f"gunzipped {archive.name}")
        return True
    return False


def find_executables(root: Path) -> list[Path]:
    """Executable regular files, preferring ones not in obvious doc/lib dirs."""
    exe = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() in (".so", ".dll", ".dylib", ".a", ".txt", ".md", ".json", ".yml", ".yaml"):
            continue
        st = p.stat()
        if st.st_mode & stat.S_IXUSR or _looks_elf(p):
            exe.append(p)
    exe.sort(key=lambda p: (len(p.parts), p.name))
    return exe


def _looks_elf(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def make_executable(p: Path) -> None:
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
