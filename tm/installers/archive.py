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


def pick_asset_for_os(assets: list[dict], os_label: str, explicit: str = "") -> dict | None:
    """Best release asset for a given OS (windows/macos/linux). None if nothing suitable."""
    if explicit:
        return next((a for a in assets if a["name"] == explicit), None)
    if os_label == "windows":
        ranked = sorted(assets, key=lambda a: score_asset(a["name"], want_windows=True), reverse=True)
        best = ranked[0] if ranked else None
        return best if best and score_asset(best["name"], want_windows=True) > 0 else None
    if os_label == "linux":
        try:
            return pick_asset(assets, want_windows=False)
        except InstallError:
            return None
    if os_label == "macos":
        ranked = sorted(assets, key=lambda a: (("darwin" in a["name"].lower()
                        or "macos" in a["name"].lower() or "osx" in a["name"].lower()),), reverse=True)
        for a in ranked:
            low = a["name"].lower()
            if ("darwin" in low or "macos" in low or "osx" in low) and not low.endswith(BAD_EXT):
                return a
    return None


def pick_assets_by_os(assets: list[dict], mapping: dict[str, str]) -> dict[str, dict]:
    """Resolve {os -> asset name|''} to {os -> asset dict}, skipping OSes with no match."""
    out: dict[str, dict] = {}
    for os_label, explicit in (mapping or {}).items():
        a = pick_asset_for_os(assets, os_label, explicit or "")
        if a:
            out[os_label] = a
    return out


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


SKIP_SUFFIXES = {".so", ".dll", ".dylib", ".a", ".o", ".txt", ".md", ".json",
                 ".yml", ".yaml", ".cfg", ".ini", ".toml", ".lock", ".log",
                 ".png", ".jpg", ".gif", ".svg", ".csv", ".html"}


def detect_os(p: Path) -> str:
    """Target OS of an executable from its magic bytes. '' if unknown.

    linux (ELF or shebang script), windows (PE/MZ), macos (Mach-O, incl. fat).
    """
    try:
        with p.open("rb") as f:
            head = f.read(4)
    except OSError:
        return ""
    if head[:4] == b"\x7fELF":
        return "linux"
    if head[:2] == b"MZ":
        return "windows"
    macho = {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",      # 32/64-bit BE
             b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",      # 32/64-bit LE
             b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}      # fat
    if head[:4] in macho:
        return "macos"
    if head[:2] == b"#!":
        return "linux"
    return ""


def shebang_interpreter(p: Path) -> str:
    """Return the interpreter from a '#!' line, resolving '/usr/bin/env X' to X. '' if none."""
    try:
        with p.open("rb") as f:
            first = f.readline(256)
    except OSError:
        return ""
    if not first.startswith(b"#!"):
        return ""
    parts = first[2:].decode("utf-8", "replace").strip().split()
    if not parts:
        return ""
    if parts[0].endswith("/env") and len(parts) > 1:
        return parts[1]
    return parts[0]


def _has_py_main(p: Path) -> bool:
    try:
        return '__main__' in p.read_text(errors="ignore")
    except OSError:
        return False


def find_programs(root: Path) -> list[Path]:
    """Runnable files: ELF, +x bit, shebang scripts (even without +x), or .py with a main.

    Superset of find_executables used by `tm import`; libraries/data are skipped.
    """
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        if p.suffix.lower() in SKIP_SUFFIXES:
            continue
        st = p.stat()
        # detect_os covers ELF (linux), PE/.exe (windows), Mach-O (macos) and shebang scripts —
        # including foreign binaries that arrive without a +x bit.
        runnable = bool(st.st_mode & stat.S_IXUSR) or bool(detect_os(p)) \
            or (p.suffix == ".py" and _has_py_main(p))
        if runnable:
            out.append(p)
    out.sort(key=lambda p: (len(p.parts), p.name))
    return out


def make_executable(p: Path) -> None:
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
