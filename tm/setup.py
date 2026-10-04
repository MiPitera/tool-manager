"""`tm setup` — make ~/tools/bin usable from the interactive shell and under sudo.

We do NOT touch /etc/sudoers ourselves. Editing sudoers wrong can lock you out, so
tm generates a validated snippet and prints the single command for you to run.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from tm.config import Config, ensure_layout
from tm.util import console, err

SUDOERS_PATH = "/etc/sudoers.d/tm"
MARKER = "# added by tm"
DEFAULT_SECURE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def setup(cfg: Config) -> None:
    ensure_layout(cfg)
    _patch_rc_files(cfg)
    _emit_sudoers(cfg)
    console.print(
        f"\n[green]✓ setup done.[/] For this shell now: "
        f'[bold]export PATH="{cfg.bin_dir}:$PATH"[/]'
    )


def _patch_rc_files(cfg: Config) -> None:
    line = f'export PATH="{cfg.bin_dir}:$PATH"  {MARKER}\n'
    for rc in (Path.home() / ".bashrc", Path.home() / ".zshrc"):
        if not rc.exists():
            continue
        text = rc.read_text()
        if MARKER in text or str(cfg.bin_dir) in text:
            continue
        rc.write_text(text.rstrip("\n") + "\n" + line)
        console.print(f"  added PATH to {rc}")


def _emit_sudoers(cfg: Config) -> None:
    """Write a validated snippet to ~/tools and tell the user how to install it."""
    content = f'{MARKER}\nDefaults secure_path="{DEFAULT_SECURE_PATH}:{cfg.bin_dir}"\n'
    snippet = cfg.root / "sudoers-tm"
    snippet.write_text(content)
    snippet.chmod(0o440)
    ok = _validate(snippet)
    console.print(f"  wrote sudoers snippet to {snippet}" + ("" if ok else " [yellow](unvalidated)[/]"))
    console.print(
        "\n[bold]To let shims resolve under sudo[/], run:\n"
        f"  [cyan]sudo install -m 0440 -o root -g root {snippet} {SUDOERS_PATH} "
        f"&& sudo visudo -cf {SUDOERS_PATH}[/]"
    )


def _validate(path: Path) -> bool:
    try:
        return subprocess.run(["visudo", "-cf", str(path)],
                              capture_output=True, text=True).returncode == 0
    except FileNotFoundError:
        err.print("  [yellow]visudo not found; cannot pre-validate the snippet[/]")
        return False
