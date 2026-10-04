"""Shim scripts in ~/tools/bin. Absolute paths only, so they work identically under sudo."""
from __future__ import annotations

import shlex
import stat
from pathlib import Path

HEADER = "#!/bin/sh\n# managed by tm — tool: {tool}\n"


def _write(path: Path, body: str, tool: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER.format(tool=tool) + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH | stat.S_IRGRP | stat.S_IROTH)
    return path


def exec_shim(bin_dir: Path, cmd: str, tool: str, target: Path, interpreter: Path | None = None,
              env: dict[str, str] | None = None) -> Path:
    target = target.resolve()
    lines = [f"export {k}={shlex.quote(v)}\n" for k, v in (env or {}).items()]
    argv = [str(interpreter.resolve())] if interpreter else []
    argv.append(str(target))
    lines.append(f'exec {" ".join(shlex.quote(a) for a in argv)} "$@"\n')
    return _write(bin_dir / cmd, "".join(lines), tool)


def path_shim(bin_dir: Path, cmd: str, tool: str, target: Path, os_label: str = "",
              hint: bool = True) -> Path:
    """Shim for a program meant to run on ANOTHER machine/OS: print its path, don't execute.

    stdout = the absolute path (so `scp $(cmd) host:` works); a human hint goes to stderr.
    """
    target = target.resolve()
    built = f"built for {os_label}" if os_label else "meant for another machine"
    lines = ""
    if hint:
        msg = f"tm: '{cmd}' is {built}; copy it to the target host:"
        lines = f">&2 printf '%s\\n' {shlex.quote(msg)}\n"
    lines += f"printf '%s\\n' {shlex.quote(str(target))}\n"
    return _write(bin_dir / cmd, lines, tool)


def docker_shim(bin_dir: Path, cmd: str, tool: str, image: str, flags: list[str]) -> Path:
    # "$PWD" stays expandable at run time; every other flag is quoted literally
    rendered = " ".join(
        '"' + f.replace('"', '\\"') + '"' if "$PWD" in f else shlex.quote(f) for f in flags
    )
    body = (
        'if [ -t 0 ] && [ -t 1 ]; then TM_TTY=-it; else TM_TTY=-i; fi\n'
        f'exec docker run $TM_TTY {rendered} {shlex.quote(image)} "$@"\n'
    )
    return _write(bin_dir / cmd, body, tool)


def is_managed(path: Path) -> bool:
    try:
        return "# managed by tm" in path.read_text(errors="ignore")[:200]
    except OSError:
        return False


def owner_tool(path: Path) -> str | None:
    try:
        for line in path.read_text(errors="ignore").splitlines()[:3]:
            if line.startswith("# managed by tm — tool: "):
                return line.split(": ", 1)[1].strip()
    except OSError:
        pass
    return None


def remove_for(bin_dir: Path, tool: str) -> list[str]:
    removed = []
    if bin_dir.exists():
        for p in bin_dir.iterdir():
            if p.is_file() and owner_tool(p) == tool:
                p.unlink()
                removed.append(p.name)
    return removed


def check_free(bin_dir: Path, cmd: str, tool: str) -> None:
    p = bin_dir / cmd
    if p.exists() and owner_tool(p) not in (None, tool):
        raise FileExistsError(f"command '{cmd}' already provided by tool '{owner_tool(p)}'")
    if p.exists() and not is_managed(p):
        raise FileExistsError(f"{p} exists and is not managed by tm")

