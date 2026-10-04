"""Tag vocabulary, normalization and auto/user merge rules.

Tags describe what a tool is FOR (its use), never where it came from.
Origin data (language, install method, package source) lives in manifest fields.
"""
from __future__ import annotations

import re
from pathlib import Path

# Minimal seed vocabulary. The tagger agent extends it as new tools arrive;
# the user curates it with `tm tags rename/merge` or by editing tags.txt.
DEFAULT_TAGS = """\
# tag: definition  (tags describe USE, not origin; edit freely)
linux: runs on Linux
windows: runs on Windows (stored for transfer to a Windows host)
networking: network utilities, scanning, traffic, protocols
web: web applications, HTTP tooling
dns: DNS tooling
files: file handling, conversion, archives
forensics: evidence acquisition and analysis
reversing: binary analysis, disassembly, decompilation
development: build, debug, developer tooling
monitoring: logging, metrics, observability
cli-utility: general command-line helper
"""

# Origin-type tags that must never be used (they belong in manifest fields).
FORBIDDEN = {
    # languages
    "python", "python3", "go", "golang", "rust", "c", "cpp", "c++", "csharp", "c#",
    "dotnet", ".net", "java", "javascript", "typescript", "node", "nodejs", "ruby",
    "perl", "php", "powershell", "bash", "shell", "lua", "nim",
    # install methods / sources
    "apt", "deb", "debian", "docker", "container", "pip", "pypi", "uv", "pipx",
    "cargo", "npm", "github", "gitlab", "source", "binary", "release", "snap",
    "flatpak", "go-install", "source-build",
    # debian sections
    "admin", "utils", "net", "devel", "libs", "misc", "x11", "universe",
}

PLATFORM_TAGS = {"linux", "windows"}

_TAG_RE = re.compile(r"[^a-z0-9+#.-]+")


def normalize(tag: str) -> str:
    t = tag.strip().lower().replace("_", "-").replace(" ", "-")
    t = _TAG_RE.sub("", t).strip("-")
    return t


def is_forbidden(tag: str) -> bool:
    return normalize(tag) in FORBIDDEN


def clean(tags: list[str]) -> list[str]:
    """Normalize, drop forbidden/empty, dedupe preserving order."""
    out: list[str] = []
    for t in tags:
        n = normalize(t)
        if n and n not in FORBIDDEN and n not in out:
            out.append(n)
    return out


def load_vocab(path: Path) -> dict[str, str]:
    vocab: dict[str, str] = {}
    if not path.exists():
        return vocab
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, desc = line.partition(":")
        vocab[normalize(name)] = desc.strip()
    return vocab


def add_to_vocab(path: Path, tags: dict[str, str]) -> list[str]:
    """Append new tags (with definitions) to tags.txt. Returns tags actually added."""
    vocab = load_vocab(path)
    new = {normalize(k): v for k, v in tags.items() if normalize(k) not in vocab and not is_forbidden(k)}
    if new:
        with path.open("a") as f:
            for k, v in new.items():
                f.write(f"{k}: {v}\n")
    return list(new)


def vocab_text(path: Path) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in load_vocab(path).items())


def merge(auto: list[str], user: list[str], removed: list[str]) -> list[str]:
    """Final tag list: user tags always kept; auto tags kept unless the user removed them."""
    removed_set = set(clean(removed))
    out = clean(user)
    for t in clean(auto):
        if t not in removed_set and t not in out:
            out.append(t)
    return out
