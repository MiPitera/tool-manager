"""Command/alias naming. A generic entry name (main, run, app, …) is a poor alias —
fall back to the tool's own name instead."""
from __future__ import annotations

import re

# generic entry-point names that should never become a command alias on their own
GENERIC_NAMES = {
    "main", "run", "app", "cli", "index", "start", "entry", "tool", "program",
    "script", "exe", "aout", "a-out", "init", "launch", "launcher", "__main__",
}


def norm(s: str) -> str:
    """Lowercase, hyphenate separators, strip to a safe command token."""
    t = s.strip().lower().replace(" ", "-").replace("_", "-")
    return re.sub(r"[^a-z0-9+.-]", "", t).strip("-")


def command_name(raw: str, fallback: str) -> str:
    """Command alias for an entry: its own name, unless that's empty/generic → tool name."""
    n = norm(raw)
    if not n or n in GENERIC_NAMES:
        return norm(fallback) or n or "tool"
    return n
