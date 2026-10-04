"""Per-method deterministic installers. Each returns {command: absolute_target} entrypoints."""
from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path

from tm import naming
from tm.config import Config
from tm.installers import archive
from tm.shims import docker_shim, exec_shim
from tm.util import InstallError, Logger, run


def _uv_env(cfg: Config) -> dict:
    # keep uv-managed Pythons under ~/tools/.python so root sees the same interpreter
    return {"UV_PYTHON_INSTALL_DIR": str(cfg.python_dir)}


def _install_uv_deps(py: Path, src: Path, env: dict, log: Logger) -> None:
    """Install a Python project's deps into its venv.

    Installs the project itself when it's packaged, and ALWAYS installs requirements.txt /
    requirements/*.txt when present — many repos declare deps only in requirements.txt while
    also shipping a pyproject.toml for tool config (so this must not be an either/or).
    """
    if (src / "pyproject.toml").exists() or (src / "setup.py").exists():
        run(["uv", "pip", "install", "--python", str(py), "-e", str(src)], log=log, env=env, check=False)
    req = src / "requirements.txt"
    if req.exists():
        run(["uv", "pip", "install", "--python", str(py), "-r", str(req)], log=log, env=env, check=False)
    req_dir = src / "requirements"
    if req_dir.is_dir():
        for f in sorted(req_dir.glob("*.txt")):
            run(["uv", "pip", "install", "--python", str(py), "-r", str(f)], log=log, env=env, check=False)


def clone(url: str, src: Path, log: Logger) -> None:
    if src.exists():
        shutil.rmtree(src)
    run(["git", "clone", "--depth", "1", _clone_url(url), str(src)], log=log)


def _clone_url(url: str) -> str:
    """Normalize a GitHub shorthand (owner/repo) or page URL into a git-clonable URL."""
    u = url.strip()
    if u.startswith(("http://", "https://", "git@", "ssh://", "file://", "/")) or u.endswith(".git"):
        return u
    try:
        from tm.github import parse_url
        owner, repo = parse_url(u)
        return f"https://github.com/{owner}/{repo}.git"
    except Exception:
        return u


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

    _install_uv_deps(py, src, env, log)

    entries = _console_scripts(venv, entry_hint, name, src)
    if entries:
        return entries
    return _uv_script_entry(cfg, name, src, py, entry_hint, env, log)


# console scripts that belong to common dependencies, never to the tool itself
DEP_SCRIPTS = {
    "cffi-gen-src", "pyinstaller", "pyi-archive_viewer", "pyi-bindepend",
    "pyi-grab_version", "pyi-makespec", "pyi-set_version", "nuitka", "nuitka3",
    "normalizer", "httpx", "dotenv", "tqdm", "markdown-it", "pygmentize",
    "chardetect", "rst2html", "rst2html5", "wheel", "distro", "jsonschema",
    "tabulate", "humanfriendly", "coloredlogs", "wsdump", "f2py", "isympy",
    "docutils", "watchmedo", "ruff", "black", "isort", "flake8", "mypy", "pytest",
}


def _project_scripts(src: Path) -> set[str]:
    """Console-script names declared by the project itself ([project.scripts] / entry_points)."""
    names: set[str] = set()
    pp = src / "pyproject.toml"
    if pp.exists():
        try:
            data = tomllib.loads(pp.read_text())
            names |= set((data.get("project", {}).get("scripts") or {}).keys())
            poetry = data.get("tool", {}).get("poetry", {})
            names |= set((poetry.get("scripts") or {}).keys())
        except (tomllib.TOMLDecodeError, OSError):
            pass
    return names


def _console_scripts(venv: Path, hints: list[str], name: str, src: Path | None = None) -> dict[str, str]:
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
    # prefer scripts the project declares for itself
    declared = {s for s in _project_scripts(src) if s in scripts} if src else set()
    if declared:
        return {s: scripts[s] for s in declared}
    if name in scripts:
        return {name: scripts[name]}
    # drop known dependency scripts; if exactly one real candidate remains, use it
    candidates = {k: v for k, v in scripts.items() if k not in DEP_SCRIPTS}
    if len(candidates) == 1:
        return candidates
    # ambiguous and nothing declared → let the caller fall back to a script entrypoint
    return {}


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
    # command alias from the script's stem, but a generic stem (main/run/app) -> tool name
    cmd = naming.command_name(script.stem, name)
    # shim: venv python (NOT symlink-resolved) + script, both absolute
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
    cmd = entry_hint[0] if entry_hint else naming.command_name(target.name, name)
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


# ---------------------------------------------------------------- static (standalone build)

def _detect_build_entries(src: Path) -> list[Path]:
    """Python scripts that look like a program entry (argparse / __main__ / __name__==...)."""
    roots = [src, src / "Linux", src / "linux", src / "src"]
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.glob("*.py")):
            try:
                txt = p.read_text(errors="ignore")
            except OSError:
                continue
            if "__main__" in txt or "argparse" in txt or "sys.argv" in txt:
                found.append(p)
        if found:
            break
    return found


def _top_packages(src: Path) -> list[str]:
    """Top-level importable packages in the repo (dirs with __init__.py), for --collect-all."""
    skip = {"tests", "test", "docs", "examples", "build"}
    return [p.name for p in sorted(src.iterdir())
            if p.is_dir() and (p / "__init__.py").exists() and p.name not in skip]


def install_static(cfg: Config, name: str, tool_dir: Path, url: str, tool: str,
                   entry_rel: list[str], release_assets: list[dict],
                   release_by_os: dict[str, str], log: Logger,
                   extra_args: list[str] | None = None) -> dict[str, str]:
    """Compile a standalone Linux binary (PyInstaller/Nuitka) and fetch prebuilt release
    binaries for other OSes. Returns {os_label: absolute_path} for every artifact."""
    tool = tool or "pyinstaller"
    src = tool_dir / "src"
    clone(url, src, log)
    venv = tool_dir / "venv"
    env = _uv_env(cfg)
    run(["uv", "venv", str(venv)], log=log, env=env)
    py = venv / "bin" / "python"
    _install_uv_deps(py, src, env, log)  # project + requirements
    run(["uv", "pip", "install", "--python", str(py), tool], log=log, env=env)  # the packager

    entries = [src / e for e in (entry_rel or []) if (src / e).exists()]
    if not entries:
        entries = _detect_build_entries(src)
    if not entries:
        raise InstallError("static build: could not find an entry script to compile")

    extra_args = extra_args or []
    dist = tool_dir / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    built: list[Path] = []
    for entry in entries:
        out_name = naming.command_name(entry.stem, name)  # main.py -> tool name, not "main"
        # build from the entry's own directory so `--additional-hooks-dir=.` and a sibling
        # package (e.g. Linux/lazagne/) resolve the way the repo's own command expects.
        bcwd = entry.parent
        # project may ship requirements next to the entry (per-OS dir)
        if (bcwd / "requirements.txt").exists():
            run(["uv", "pip", "install", "--python", str(py), "-r", str(bcwd / "requirements.txt")],
                log=log, env=env, check=False)
        packages = _top_packages(bcwd) or _top_packages(src)  # e.g. lazagne (+ its softwares.*)
        log.write(f"static build: entry={entry} cwd={bcwd} packages={packages} extra_args={extra_args}")
        if tool == "nuitka":
            collect = [f"--include-package={p}" for p in packages]
            run([str(py), "-m", "nuitka", "--onefile", "--assume-yes-for-downloads",
                 *collect, *extra_args, f"--output-dir={dist}",
                 f"--output-filename={out_name}", entry.name],
                log=log, cwd=bcwd, env=env, timeout=2400)
            cand = dist / out_name
        else:
            collect = [a for p in packages for a in ("--collect-all", p)]
            run([str(venv / "bin" / "pyinstaller"), "--onefile", "--distpath", str(dist),
                 "--workpath", str(tool_dir / "build"), "--specpath", str(tool_dir / "build"),
                 *collect, *extra_args, "--name", out_name, entry.name],
                log=log, cwd=bcwd, env=env, timeout=2400)
            cand = dist / out_name
        if cand.exists():
            archive.make_executable(cand)
            built.append(cand)
    if not built:
        raise InstallError("static build produced no binary")

    artifacts: dict[str, str] = {"linux": str(built[0].resolve())}
    for i, b in enumerate(built[1:], 2):  # extra linux builds, if several entries
        artifacts[f"linux-{i}"] = str(b.resolve())

    # prebuilt release binaries for other OSes
    for os_label, asset in archive.pick_assets_by_os(release_assets, release_by_os).items():
        if os_label == "linux":
            continue
        dest_dir = tool_dir / f"release-{os_label}"
        dl = archive.download(asset["url"], dest_dir / asset["name"], log)
        extracted = archive.extract(dl, dest_dir, log)
        if extracted:
            dl.unlink(missing_ok=True)
            exe = next((p for p in sorted(dest_dir.rglob("*")) if p.is_file()
                        and (p.suffix.lower() == ".exe" or archive.detect_os(p) == os_label)), None)
            artifacts[os_label] = str((exe or dest_dir).resolve())
        else:
            artifacts[os_label] = str(dl.resolve())
    return artifacts


def _norm(s: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9_-]", "-", s).strip("-").lower()
