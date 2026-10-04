"""Install orchestration: gather -> classify -> confirm -> install -> (build) -> smoke -> record."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from rich.panel import Panel
from rich.table import Table

from tm import agents, github, manifest, shims, tags as tagmod
from tm.config import Config
from tm.installers import apt, archive, methods
from tm.manifest import Manifest
from tm.registry import Registry
from tm.util import InstallError, Logger, confirm, console, err


def _derive_name(url: str) -> str:
    _, repo = github.parse_url(url)
    return _norm_name(repo)


def _norm_name(raw: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "-", raw).strip("-").lower()


def install_from_github(cfg: Config, url: str, *, name: str = "", docker: bool = False,
                        method: str = "", user_tags: list[str] | None = None,
                        static: bool = False, static_tool: str = "",
                        assume_yes: bool = False) -> Manifest:
    name = name or _derive_name(url)
    tool_dir = cfg.tool_dir(name)
    if tool_dir.exists() and any(tool_dir.iterdir()):
        raise InstallError(f"'{name}' already exists ({tool_dir}); use `tm remove {name}` or --name")
    log = Logger(tool_dir / "install.log")

    console.print(f"[bold]Gathering repo context[/] for {url}")
    ctx = github.gather(url)

    console.print(f"[bold]Classifying[/] with model {cfg.model('classifier')} …")
    hint = ""
    if docker:
        hint = "The user requires installation via Docker. Set method=docker."
    elif method:
        hint = f"The user requires method={method}."
    vocab = tagmod.vocab_text(cfg.tags_file)
    cls = agents.classify(cfg, ctx.as_prompt(), vocab, hint, log)
    if docker:
        cls.method = "docker"
    elif method:
        cls.method = method  # type: ignore

    # static standalone build: forced with --static, or auto-detected from the repo's docs,
    # unless the user forced a different method explicitly.
    go_static = (static or cls.static_recommended) and not method and not docker
    if go_static:
        return _install_static(cfg, name, tool_dir, url, ctx, cls,
                               static_tool or cls.static_tool, user_tags, assume_yes, log)

    _show_plan(cls, name, url)
    if not confirm("Proceed with this plan?", assume_yes=assume_yes):
        raise InstallError("aborted by user")

    if cls.apt_deps:
        if confirm(f"Install system deps via apt: {', '.join(cls.apt_deps)}?", assume_yes=assume_yes):
            for dep in cls.apt_deps:
                apt.apt_install(dep, log, assume_yes=True)

    entrypoints = _run_method(cfg, name, tool_dir, url, ctx, cls, log)

    entry_os = {c: "windows" for c in entrypoints} if cls.method == "release_windows" else {}
    m = Manifest(
        name=name, source=url, method=cls.method, language=cls.language,
        version=ctx.release_tag, platform=cls.platform, description=cls.description,
        entrypoints=entrypoints, entry_os=entry_os, recommended_by_repo=cls.recommended_by_repo,
        deviation_reason=cls.deviation_reason, docker_image=cls.docker_image,
        apt_deps=cls.apt_deps,
    )
    m.tags_auto = tagmod.clean(cls.tags)
    if user_tags:
        m.add_user_tags(user_tags)
    if cls.new_tags:
        tagmod.add_to_vocab(cfg.tags_file, cls.new_tags)

    _smoke_test(cfg, m, log)
    manifest.save(tool_dir, m)
    _index(cfg, m)
    console.print(f"[green]✓ installed[/] {name} — commands: {', '.join(m.entrypoints) or '(none, see tm info)'}")
    return m


def _install_static(cfg, name, tool_dir, url, ctx, cls, tool, user_tags, assume_yes, log) -> Manifest:
    tool = tool or "pyinstaller"
    console.print(f"[bold]Static build[/] with {tool} (standalone, for deploying to other hosts)")
    _show_plan(cls, name, url)
    if not confirm(f"Build a standalone binary with {tool} and fetch any prebuilt release "
                   f"binaries for other OSes?", assume_yes=assume_yes):
        raise InstallError("aborted by user")
    if cls.apt_deps and confirm(f"Install build deps via apt: {', '.join(cls.apt_deps)}?",
                                assume_yes=assume_yes):
        for dep in cls.apt_deps:
            apt.apt_install(dep, log, assume_yes=True)
    try:
        artifacts = methods.install_static(cfg, name, tool_dir, url, tool,
                                           cls.static_entrypoints, ctx.release_assets,
                                           cls.release_assets_by_os, log,
                                           extra_args=cls.static_extra_args)
    except InstallError as e:
        console.print(f"[yellow]static build failed ({e}); escalating to the build agent…[/]")
        log.write(f"static build failed, escalating: {e}")
        cls.install_steps = cls.install_steps or [f"build a standalone binary with {tool} --onefile"]
        ep = _source_build(cfg, name, tool_dir, url, ctx, cls, str(e), log)
        artifacts = {"linux": next(iter(ep.values()))} if ep else {}
        if not artifacts:
            raise
    cmd = name
    shims.multipath_shim(cfg.bin_dir, cmd, name, artifacts)
    oses = set(artifacts.keys())
    platform = "both" if (oses - {"linux"} and "linux" in oses) else (
        "windows" if oses == {"windows"} else "linux")
    m = Manifest(
        name=name, source=url, method="static", language=cls.language or "python",
        version=ctx.release_tag, platform=platform, description=cls.description,
        entrypoints={cmd: artifacts.get("linux", next(iter(artifacts.values())))},
        entry_os={cmd: "linux-other"},  # foreign: the command prints paths, never runs
        artifacts=artifacts, recommended_by_repo=cls.recommended_by_repo,
    )
    m.tags_auto = tagmod.clean(cls.tags)
    if user_tags:
        m.add_user_tags(user_tags)
    if cls.new_tags:
        tagmod.add_to_vocab(cfg.tags_file, cls.new_tags)
    manifest.save(tool_dir, m)
    _index(cfg, m)
    console.print(f"[green]✓ built[/] {name} — `{cmd}` prints {len(artifacts)} artifact path(s): "
                  f"{', '.join(artifacts)}")
    return m


def _run_method(cfg, name, tool_dir, url, ctx, cls, log) -> dict[str, str]:
    mth = cls.method
    try:
        if mth == "release_binary":
            ep = methods.install_release(cfg, name, tool_dir, ctx.release_assets, cls.release_asset, cls.entrypoints, log)
            methods.make_shims(cfg, name, ep)
            return ep
        if mth == "release_windows":
            return methods.install_release_windows(cfg, name, tool_dir, ctx.release_assets, cls.release_asset, log)
        if mth in ("uv_project", "uv_script"):
            return methods.install_uv(cfg, name, tool_dir, url, mth == "uv_script",
                                      cls.entrypoints, cls.python_version, log)
        if mth == "go_install":
            ep = methods.install_go(cfg, name, tool_dir, url, cls.go_package, cls.entrypoints, log)
            methods.make_shims(cfg, name, ep)
            return ep
        if mth == "docker":
            ep, image = methods.install_docker(cfg, name, tool_dir, url, cls.docker_image, log)
            cls.docker_image = image
            return ep
        if mth == "source_build":
            return _source_build(cfg, name, tool_dir, url, ctx, cls, "", log)
        raise InstallError(f"unknown method {mth}")
    except InstallError as e:
        if mth == "source_build":
            raise
        console.print(f"[yellow]method {mth} failed:[/] {e}\n[yellow]escalating to the build agent…[/]")
        log.write(f"method {mth} failed, escalating to builder: {e}")
        return _source_build(cfg, name, tool_dir, url, ctx, cls, str(e), log)


def _source_build(cfg, name, tool_dir, url, ctx, cls, failure, log) -> dict[str, str]:
    src = tool_dir / "src"
    if not src.exists():
        methods.clone(url, src, log)
    model = cfg.model("builder")
    for attempt in range(3):
        console.print(f"[bold]Build agent[/] ({model}) attempt {attempt + 1} …")
        res = agents.build(cfg, src, tool_dir, cls.install_steps, ctx.as_prompt(), failure, model, log)
        if res.status == "ok":
            ep = {cmd: str((src / tgt).resolve()) if not Path(tgt).is_absolute() else tgt
                  for cmd, tgt in res.entrypoints.items()}
            if not ep:
                raise InstallError("build agent reported ok but produced no entrypoints")
            methods.make_shims(cfg, name, ep)
            return ep
        if res.status == "need_apt" and res.apt_needed:
            if confirm(f"Build needs system packages: {', '.join(res.apt_needed)}. Install?"):
                for dep in res.apt_needed:
                    apt.apt_install(dep, log, assume_yes=True)
                failure = f"installed apt deps {res.apt_needed}; retry the build"
                model = cfg.model("builder")
                continue
            raise InstallError("build needs apt packages the user declined")
        # failed: escalate model once
        if model != cfg.models.get("builder_escalation") and attempt >= 1:
            model = cfg.models.get("builder_escalation", model)
            failure = res.notes
            continue
        failure = res.notes
    raise InstallError(f"source build failed: {failure}")


def _show_plan(cls: agents.Classification, name: str, url: str) -> None:
    t = Table(show_header=False, box=None)
    t.add_row("name", name)
    t.add_row("method", f"[bold]{cls.method}[/]")
    t.add_row("language", cls.language or "-")
    t.add_row("platform", cls.platform)
    t.add_row("description", cls.description)
    t.add_row("tags", ", ".join(tagmod.clean(cls.tags)) or "-")
    if cls.apt_deps:
        t.add_row("apt deps", ", ".join(cls.apt_deps))
    if cls.recommended_by_repo.found:
        t.add_row("repo recommends", f'"{cls.recommended_by_repo.quote}" ({cls.recommended_by_repo.source_file})')
    if cls.deviation_reason:
        t.add_row("deviation", cls.deviation_reason)
    t.add_row("confidence", f"{cls.confidence:.0%}")
    console.print(Panel(t, title=f"Install plan — {url}"))


def _smoke_test(cfg: Config, m: Manifest, log: Logger) -> None:
    if m.method == "release_windows" or not m.entrypoints:
        return
    for cmd in m.entrypoints:
        if m.is_foreign(cmd):  # foreign shims just print a path — nothing to smoke-test
            continue
        shim = cfg.bin_dir / cmd
        if not shim.exists():
            continue
        for flag in (["--help"], ["-h"], []):
            try:
                p = subprocess.run([str(shim), *flag], capture_output=True, text=True, timeout=15)
                log.write(f"smoke `{cmd} {' '.join(flag)}` -> exit {p.returncode}")
                if p.returncode == 0:
                    return
            except subprocess.TimeoutExpired:
                log.write(f"smoke `{cmd} {' '.join(flag)}` timed out")
            except OSError as e:
                log.write(f"smoke `{cmd}` error: {e}")
        err.print(f"[yellow]⚠ smoke test for '{cmd}' did not return cleanly; check `tm info {m.name}` / install.log[/]")
        return


def _index(cfg: Config, m: Manifest) -> None:
    reg = Registry(cfg.db_path)
    reg.upsert(m)
    reg.close()


# ---------------------------------------------------------------- apt entry points

def install_apt(cfg: Config, pkg: str, user_tags: list[str] | None = None,
                assume_yes: bool = False, force: bool = False) -> Manifest | None:
    if not apt.pkg_exists(pkg):
        raise InstallError(f"apt package not found: {pkg}")
    name = pkg
    tool_dir = cfg.tool_dir(name)
    log = Logger(tool_dir / "install.log")
    if not assume_yes and not confirm(f"Install apt package '{pkg}'?"):
        raise InstallError("aborted")
    apt.apt_install(pkg, log, assume_yes=True)
    meta = apt.pkg_metadata(pkg)
    if not meta["binaries"] and not force:
        err.print(f"[yellow]'{pkg}' ships no executable program (looks like a library/data "
                  f"package).[/] It stays installed on the system.")
        if not confirm("Catalog it anyway?", default=False, assume_yes=assume_yes):
            console.print(f"[dim]not cataloged; `{pkg}` remains installed via apt.[/]")
            return None
    vocab = tagmod.vocab_text(cfg.tags_file)
    try:
        batch = agents.tag_packages(cfg, [meta], vocab, log)
        item = batch.items[0] if batch.items else None
        if batch.new_tags:
            tagmod.add_to_vocab(cfg.tags_file, batch.new_tags)
    except InstallError as e:
        err.print(f"[yellow]tagging failed ({e}); storing without auto tags[/]")
        item = None
    m = Manifest(
        name=name, source=f"apt:{pkg}", method="apt", apt_package=pkg,
        version=apt.version_installed(pkg),
        description=(item.description if item else meta["description"]),
        entrypoints={b: f"/usr/bin/{b}" for b in meta["binaries"]},
    )
    m.tags_auto = tagmod.clean(item.tags) if item else []
    if user_tags:
        m.add_user_tags(user_tags)
    manifest.save(tool_dir, m)
    _index(cfg, m)
    console.print(f"[green]✓ cataloged apt[/] {pkg} — tags: {', '.join(m.tags) or '-'}")
    return m


def import_apt(cfg: Config, batch_size: int = 30, assume_yes: bool = False) -> int:
    pkgs = apt.interesting_packages()
    existing = {m.name for _, m in manifest.iter_all(cfg.root)}
    pkgs = [p for p in pkgs if p not in existing]
    if not pkgs:
        console.print("Nothing new to import.")
        return 0
    console.print(f"Found [bold]{len(pkgs)}[/] manually-installed packages to catalog.")
    if not assume_yes and not confirm("Catalog and auto-tag them?"):
        return 0
    vocab = tagmod.vocab_text(cfg.tags_file)
    count = 0
    reg = Registry(cfg.db_path)
    try:
        for i in range(0, len(pkgs), batch_size):
            chunk = pkgs[i:i + batch_size]
            metas = [apt.pkg_metadata(p) for p in chunk]
            metas = [mt for mt in metas if mt["binaries"]]  # programs only, never libraries/data
            if not metas:
                continue
            console.print(f"Tagging {i + 1}–{i + len(chunk)} of {len(pkgs)} …")
            try:
                tb = agents.tag_packages(cfg, metas, vocab)
                if tb.new_tags:
                    tagmod.add_to_vocab(cfg.tags_file, tb.new_tags)
                    vocab = tagmod.vocab_text(cfg.tags_file)
                by_name = {it.name: it for it in tb.items}
            except InstallError as e:
                err.print(f"[yellow]batch tagging failed ({e}); storing without tags[/]")
                by_name = {}
            for meta in metas:
                it = by_name.get(meta["name"])
                m = Manifest(
                    name=meta["name"], source=f"apt:{meta['name']}", method="apt",
                    apt_package=meta["name"], version=apt.version_installed(meta["name"]),
                    description=(it.description if it else meta["description"]),
                    entrypoints={b: f"/usr/bin/{b}" for b in meta["binaries"]},
                )
                m.tags_auto = tagmod.clean(it.tags) if it else []
                manifest.save(cfg.tool_dir(meta["name"]), m)
                reg.upsert(m)
                count += 1
    finally:
        reg.close()
    console.print(f"[green]✓ cataloged {count} apt packages[/]")
    return count


# ---------------------------------------------------------------- local import

def import_local(cfg: Config, path: str, *, name: str = "", entry: str = "",
                 user_tags: list[str] | None = None, copy: bool = False,
                 assume_yes: bool = False) -> Manifest:
    src = Path(path).expanduser()
    if not src.exists():
        raise InstallError(f"path does not exist: {src}")
    src = src.resolve()
    name = name or _norm_name(src.stem if src.is_file() else src.name)
    if not name:
        raise InstallError("could not derive a name; pass --name")
    tool_dir = cfg.tool_dir(name)
    # a directory already sitting in ~/tools (source == destination) → import in place
    in_place = src.is_dir() and src == tool_dir
    if not in_place and not manifest.path_for(tool_dir).exists() \
            and tool_dir.exists() and any(tool_dir.iterdir()):
        raise InstallError(f"'{name}' directory already exists ({tool_dir}); "
                           f"use `tm remove {name}` to clear it, or pass --name")
    if not in_place and manifest.path_for(tool_dir).exists():
        raise InstallError(f"'{name}' already exists; use `tm remove {name}` or --name")
    log = Logger(tool_dir / "install.log")

    app = tool_dir / "app"
    app.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        dest = app / src.name
        _place(src, dest, copy, log)
        candidates = [dest]
    elif in_place:
        _restructure_in_place(tool_dir, app, log)
        candidates = archive.find_programs(app)
    else:
        _place_dir(src, app, copy, log)
        candidates = archive.find_programs(app)
    if not candidates:
        if not in_place:  # never delete the user's own directory on an in-place import
            shutil.rmtree(tool_dir, ignore_errors=True)
        raise InstallError("no runnable file found; pass --entry, or use `tm install` for "
                           "projects that need building")

    chosen = _choose_entries(candidates, app, name, entry, assume_yes)

    # per-entrypoint facts for the agent + deterministic fallback (unique command names)
    progs: dict[str, Path] = {}
    entries_info = []
    for prog in chosen:
        cmd = _unique_cmd(prog, name, progs)
        progs[cmd] = prog
        entries_info.append({"name": cmd, "file_type": _file_type(prog),
                             "detected_os": archive.detect_os(prog)})

    # agent decides description/tags + which entrypoints run locally vs are for another OS
    description, tags_auto, decisions = _classify_import(cfg, name, progs, entries_info, app, log)

    entrypoints: dict[str, str] = {}
    entry_os: dict[str, str] = {}
    for cmd, prog in progs.items():
        archive.make_executable(prog)
        shims.check_free(cfg.bin_dir, cmd, name)
        runs_locally, target_os = decisions[cmd]
        if runs_locally:
            interp = archive.shebang_interpreter(prog)
            interpreter = _resolve_interpreter(interp) if interp and not archive._looks_elf(prog) else None
            shims.exec_shim(cfg.bin_dir, cmd, name, prog, interpreter=interpreter)
            entry_os[cmd] = target_os or "linux"
        else:
            shims.path_shim(cfg.bin_dir, cmd, name, prog, os_label=target_os)
            entry_os[cmd] = target_os or "other"
        entrypoints[cmd] = str(prog.resolve())

    m = Manifest(name=name, source=f"local:{src}", method="imported",
                 language=_guess_language(chosen[0]), entrypoints=entrypoints,
                 entry_os=entry_os, platform=_platform_from(entry_os),
                 description=description)
    m.tags_auto = tags_auto
    if user_tags:
        m.add_user_tags(user_tags)

    _smoke_test(cfg, m, log)
    manifest.save(tool_dir, m)
    _index(cfg, m)
    foreign = m.foreign_commands
    note = f" (prints path, run elsewhere: {', '.join(foreign)})" if foreign else ""
    console.print(f"[green]✓ imported[/] {name} — commands: {', '.join(entrypoints)}{note}")
    return m


def _platform_from(entry_os: dict[str, str]) -> str:
    oses = {o for o in entry_os.values()}
    has_linux = bool(oses & {"linux", "local", ""})
    has_foreign = bool(oses - {"linux", "local", ""})
    if has_linux and has_foreign:
        return "both"
    if has_foreign and not has_linux:
        return "windows" if oses <= {"windows"} else "both"
    return "linux"


def _place(src: Path, dest: Path, copy: bool, log: Logger) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copy2(src, dest)
        log.write(f"copied {src} -> {dest}")
    else:
        shutil.move(str(src), str(dest))
        log.write(f"moved {src} -> {dest}")


def _place_dir(src: Path, app: Path, copy: bool, log: Logger) -> None:
    if copy:
        shutil.copytree(src, app, dirs_exist_ok=True, symlinks=True)
        log.write(f"copied dir {src} -> {app}")
        return
    try:
        for child in list(src.iterdir()):
            shutil.move(str(child), str(app / child.name))
        log.write(f"moved contents of {src} -> {app}")
    except OSError as e:  # cross-device or similar
        log.write(f"move failed ({e}); falling back to copy")
        shutil.copytree(src, app, dirs_exist_ok=True, symlinks=True)
        shutil.rmtree(src, ignore_errors=True)


def _restructure_in_place(tool_dir: Path, app: Path, log: Logger) -> None:
    """Source dir already IS the tool dir: move its contents into app/ (keep tm's own files)."""
    app.mkdir(exist_ok=True)
    keep = {"app", "manifest.json", "install.log"}
    for child in list(tool_dir.iterdir()):
        if child.name in keep:
            continue
        shutil.move(str(child), str(app / child.name))
    log.write(f"restructured {tool_dir} in place -> {app}")


def _choose_entries(candidates: list[Path], app: Path, name: str, entry: str,
                    assume_yes: bool) -> list[Path]:
    if entry:
        want = (app / entry).resolve()
        for c in candidates:
            if c.resolve() == want or c.name == entry or c.stem == entry:
                return [c]
        raise InstallError(f"--entry '{entry}' not found among runnable files")
    if len(candidates) == 1:
        return candidates
    # cross-OS builds of one tool (e.g. tool, tool.exe, tool-mac): keep one per OS
    cross = _cross_os_builds(candidates, name)
    if cross:
        return cross
    for c in candidates:  # same-OS: a single named program
        if c.stem == name or c.name == name:
            return [c]
    if assume_yes:
        return [candidates[0]]
    return _prompt_entries(candidates, app)


def _cross_os_builds(candidates: list[Path], name: str) -> list[Path]:
    """If the candidates are the same program built for several OSes, return one per OS.

    Returns [] when they are not a cross-OS set (so normal single-entrypoint logic applies).
    """
    oses = {archive.detect_os(c) for c in candidates}
    if not (oses - {"", "linux"}) or len(oses) < 2:
        return []  # no foreign builds, or all one OS
    picks: dict[str, Path] = {}
    for c in sorted(candidates, key=lambda p: (len(p.parts), p.name)):
        o = archive.detect_os(c)
        if o not in picks or (c.stem == name and picks[o].stem != name):
            picks[o] = c
    return list(picks.values())


def _prompt_entries(candidates: list[Path], app: Path) -> list[Path]:
    from rich.prompt import Prompt
    console.print("[bold]Multiple runnable files found:[/]")
    for i, c in enumerate(candidates, 1):
        console.print(f"  {i}. {c.relative_to(app)}")
    raw = Prompt.ask("Pick entrypoint number(s), comma-separated", default="1")
    picks = []
    for tok in raw.replace(" ", "").split(","):
        if tok.isdigit() and 1 <= int(tok) <= len(candidates):
            picks.append(candidates[int(tok) - 1])
    return picks or [candidates[0]]


def _unique_cmd(prog: Path, fallback: str, taken: dict) -> str:
    """A command name that doesn't collide — stem first, then full name, then a numeric suffix.

    Lets a tool ship same-named builds for several OSes (foo + foo.exe -> foo, foo-exe).
    """
    base = _norm_name(prog.stem) or fallback
    if base not in taken:
        return base
    alt = _norm_name(prog.name) or fallback
    if alt not in taken:
        return alt
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def _resolve_interpreter(interp: str) -> Path | None:
    p = shutil.which(interp)
    return Path(p) if p else Path(interp)


def _guess_language(prog: Path) -> str:
    if archive._looks_elf(prog):
        return ""
    interp = archive.shebang_interpreter(prog)
    if prog.suffix == ".py" or "python" in interp:
        return "python"
    if interp:
        return Path(interp).name
    return ""


def _classify_import(cfg: Config, name: str, progs: dict[str, Path], entries_info: list[dict],
                     app: Path, log: Logger):
    """Ask the agent for description/tags + per-entrypoint run location.

    Returns (description, tags_auto, {cmd: (runs_locally, target_os)}). Falls back to magic-byte
    detection for any decision the agent omits or if the agent call fails.
    """
    help_head = _help_from_native(progs, entries_info)
    vocab = tagmod.vocab_text(cfg.tags_file)
    description, tags_auto, decisions = "", [], {}
    try:
        res = agents.classify_import(cfg, name, help_head, entries_info, vocab, log)
        description = res.description
        tags_auto = tagmod.clean(res.tags)
        if res.new_tags:
            tagmod.add_to_vocab(cfg.tags_file, res.new_tags)
        for d in res.entrypoints:
            if d.name in progs:
                decisions[d.name] = (d.runs_locally, d.target_os)
    except InstallError as e:
        err.print(f"[yellow]classify failed ({e}); deciding run location by file type[/]")
    for info in entries_info:  # deterministic fallback for anything missing
        decisions.setdefault(info["name"], (info["detected_os"] in ("", "linux"), info["detected_os"]))
    return description, tags_auto, decisions


def _help_from_native(progs: dict[str, Path], entries_info: list[dict]) -> str:
    """--help output from a native (Linux/script) entrypoint; never run foreign binaries."""
    native = [i["name"] for i in entries_info if i["detected_os"] in ("", "linux")]
    for cmd in native:
        prog = progs[cmd]
        try:
            archive.make_executable(prog)
        except OSError:
            continue
        for flag in (["--help"], ["-h"]):
            try:
                p = subprocess.run([str(prog), *flag], capture_output=True, text=True, timeout=10)
                text = (p.stdout or p.stderr).strip()
                if text:
                    return "\n".join(text.splitlines()[:8])
            except (OSError, subprocess.SubprocessError):
                pass
    return ""


def _file_type(prog: Path) -> str:
    try:
        p = subprocess.run(["file", "-b", str(prog)], capture_output=True, text=True, timeout=10)
        return p.stdout.strip()[:200]
    except (OSError, subprocess.SubprocessError):
        return ""
