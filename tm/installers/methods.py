"""Per-method deterministic installers. Each returns {command: absolute_target} entrypoints."""
from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path

from tm.config import Config
from tm.installers import archive
from tm.shims import docker_shim, exec_shim
from tm.util import InstallError, Logger, run


def _uv_env(cfg: Config) -> dict:
    # keep uv-managed Pythons under ~/tools/.python so root sees the same interpreter
    return {"UV_PYTHON_INSTALL_DIR": str(cfg.python_dir)}


def clone(url: str, src: Path, log: Logger) -> None:
    if src.exists():
        shutil.rmtree(src)
    run(["git", "clone", "--depth", "1", url, str(src)], log=log)


# ---------------------------------------------------------------- release binaries

def install_release(cfg: Config, name: str, tool_dir: Path, assets: list[dict],
                    explicit_asset: str, entry_hint: list[str], log: Logger) -> dict[str, str]:
    rel = tool_dir / "release"
    rel.mkdir(parents=True, exist_ok=True)
    asset = archive.pick_asset(assets, want_windows=False, explicit=explicit_asset)
    dl = archive.download(asset["url"], rel / asset["name"], log)
    extracted = archive.extract(dl, rel, log)
    if not extracted:
        archive.make_executable(dl)
        target = dl
        exes = [dl]
    else:
        dl.unlink(missing_ok=True)
        exes = archive.find_executables(rel)
        if not exes:
            raise InstallError("no executable found in release archive")
        for e in exes:
            archive.make_executable(e)
        target = _pick_entry(exes, name, entry_hint)
    cmd = _cmd_name(name, entry_hint, target)
    return {cmd: str(target.resolve())}


def _pick_entry(exes: list[Path], name: str, hints: list[str]) -> Path:
    wanted = [h for h in (hints or []) if h]
    for h in wanted + [name]:
        for e in exes:
            if e.name == h or e.stem == h:
                return e
    return exes[0]


def _cmd_name(name: str, hints: list[str], target: Path) -> str:
    return (hints[0] if hints else "") or name


# ---------------------------------------------------------------- windows (store only)

def install_release_windows(cfg: Config, name: str, tool_dir: Path, assets: list[dict],
                            explicit_asset: str, log: Logger) -> dict[str, str]:
    win = tool_dir / "windows"
    win.mkdir(parents=True, exist_ok=True)
    asset = archive.pick_asset(assets, want_windows=True, explicit=explicit_asset)
    dl = archive.download(asset["url"], win / asset["name"], log)
    extracted = archive.extract(dl, win, log)
    # point the shim at the primary .exe (or the downloaded file if not an archive)
    if extracted:
        dl.unlink(missing_ok=True)
        exes = sorted(win.rglob("*.exe"), key=lambda p: (len(p.parts), p.name))
        target = exes[0] if exes else next((p for p in win.rglob("*") if p.is_file()), win)
    else:
        target = dl
    from tm.shims import path_shim
    path_shim(cfg.bin_dir, name, name, target, os_label="windows")
    log.write(f"stored Windows artifact in {win}; `{name}` prints its path")
    return {name: str(target.resolve())}


# ---------------------------------------------------------------- uv project / script

def install_uv(cfg: Config, name: str, tool_dir: Path, url: str, is_script: bool,
               entry_hint: list[str], python_version: str, log: Logger) -> dict[str, str]:
    src = tool_dir / "src"
    clone(url, src, log)
    venv = tool_dir / "venv"
    env = _uv_env(cfg)
    args = ["uv", "venv", str(venv)]
    if python_version:
        args += ["--python", python_version]
    run(args, log=log, env=env)
    py = venv / "bin" / "python"

    if is_script:
        return _uv_script_entry(cfg, name, src, py, entry_hint, env, log)

    installed = False
    if (src / "pyproject.toml").exists() or (src / "setup.py").exists():
        run(["uv", "pip", "install", "--python", str(py), "-e", str(src)], log=log, env=env)
        installed = True
    elif (src / "requirements.txt").exists():
        run(["uv", "pip", "install", "--python", str(py), "-r", str(src / "requirements.txt")], log=log, env=env)

    entries = _console_scripts(venv, entry_hint, name)
    if entries:
        return entries
    if installed:
        # fall back to a module entrypoint if the hint names one
        pass
    return _uv_script_entry(cfg, name, src, py, entry_hint, env, log)


def _console_scripts(venv: Path, hints: list[str], name: str) -> dict[str, str]:
    binp = venv / "bin"
    standard = {"python", "python3", "pip", "pip3", "activate", "activate.csh",
                "activate.fish", "uv", "uvx"}
    scripts = {p.name: str(p.resolve()) for p in binp.iterdir()
               if p.is_file() and os.access(p, os.X_OK) and p.name not in standard
               and not p.name.startswith("activate") and not p.name.startswith("python")}
    if not scripts:
        return {}
    wanted = [h for h in (hints or []) if h in scripts]
    if wanted:
        return {h: scripts[h] for h in wanted}
    if name in scripts:
        return {name: scripts[name]}
    return scripts


def _uv_script_entry(cfg: Config, name: str, src: Path, py: Path, hints: list[str],
                     env: dict, log: Logger) -> dict[str, str]:
    script = None
    for h in (hints or []):
        cand = src / h
        if cand.exists():
            script = cand
            break
    if script is None:
        for cand in (src / f"{name}.py", src / "main.py", src / "__main__.py",
                     src / name / "__main__.py", src / "src" / f"{name}.py"):
            if cand.exists():
                script = cand
                break
    if script is None:
        raise InstallError(f"could not locate an entry script for {name}; set entrypoints via source build")
    # shim: venv python + script, both absolute
    cmd = (hints[0] if hints else name)
    exec_shim(cfg.bin_dir, cmd, name, script, interpreter=py)
    return {cmd: str(script.resolve())}


# ---------------------------------------------------------------- go install

def install_go(cfg: Config, name: str, tool_dir: Path, url: str, go_package: str,
               entry_hint: list[str], log: Logger) -> dict[str, str]:
    gobin = tool_dir / "bin"
    gobin.mkdir(parents=True, exist_ok=True)
    pkg = go_package or _guess_go_pkg(url)
    run(["go", "install", f"{pkg}@latest"], log=log, env={"GOBIN": str(gobin)})
    exes = [p for p in gobin.iterdir() if p.is_file() and os.access(p, os.X_OK)]
    if not exes:
        raise InstallError("go install produced no binary")
    target = _pick_entry(exes, name, entry_hint)
    cmd = (entry_hint[0] if entry_hint else "") or target.name
    return {cmd: str(target.resolve())}


def _guess_go_pkg(url: str) -> str:
    from tm.github import parse_url
    owner, repo = parse_url(url)
    return f"github.com/{owner}/{repo}/..."


# ---------------------------------------------------------------- docker

def install_docker(cfg: Config, name: str, tool_dir: Path, url: str, image: str,
                   log: Logger) -> tuple[dict[str, str], str]:
    flags = cfg.docker.get("run_flags", [])
    if image:
        run(["docker", "pull", image], log=log)
    else:
        src = tool_dir / "src"
        clone(url, src, log)
        image = f"tm/{name}:latest"
        run(["docker", "build", "-t", image, str(src)], log=log)
    docker_shim(cfg.bin_dir, name, name, image, flags)
    return {name: f"docker:{image}"}, image


# ---------------------------------------------------------------- shim creation for file targets

def make_shims(cfg: Config, name: str, entrypoints: dict[str, str]) -> None:
    """Create exec shims for entrypoints that are plain file paths (not docker:/already-shimmed)."""
    for cmd, target in entrypoints.items():
        if target.startswith("docker:"):
            continue
        tp = Path(target)
        shim = cfg.bin_dir / cmd
        if shim.exists():
            from tm.shims import owner_tool
            if owner_tool(shim) == name:
                continue
        exec_shim(cfg.bin_dir, cmd, name, tp)
