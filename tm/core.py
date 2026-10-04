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

    _show_plan(cls, name, url)
    if not confirm("Proceed with this plan?", assume_yes=assume_yes):
        raise InstallError("aborted by user")

    if cls.apt_deps:
        if confirm(f"Install system deps via apt: {', '.join(cls.apt_deps)}?", assume_yes=assume_yes):
            for dep in cls.apt_deps:
                apt.apt_install(dep, log, assume_yes=True)

    entrypoints = _run_method(cfg, name, tool_dir, url, ctx, cls, log)

    m = Manifest(
        name=name, source=url, method=cls.method, language=cls.language,
        version=ctx.release_tag, platform=cls.platform, description=cls.description,
        entrypoints=entrypoints, recommended_by_repo=cls.recommended_by_repo,
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
    if tool_dir.exists() and any(tool_dir.iterdir()):
        raise InstallError(f"'{name}' already exists ({tool_dir}); use `tm remove {name}` or --name")
    log = Logger(tool_dir / "install.log")

    app = tool_dir / "app"
    app.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        dest = app / src.name
        _place(src, dest, copy, log)
        candidates = [dest]
    else:
        _place_dir(src, app, copy, log)
        candidates = archive.find_programs(app)
    if not candidates:
        shutil.rmtree(tool_dir, ignore_errors=True)
        raise InstallError("no runnable file found; pass --entry, or use `tm install` for "
                           "projects that need building")

    chosen = _choose_entries(candidates, app, name, entry, assume_yes)
    entrypoints: dict[str, str] = {}
    for prog in chosen:
        archive.make_executable(prog)
        interp = archive.shebang_interpreter(prog)
        cmd = _norm_name(prog.stem) or name
        shims.check_free(cfg.bin_dir, cmd, name)
        interpreter = _resolve_interpreter(interp) if interp and not archive._looks_elf(prog) else None
        shims.exec_shim(cfg.bin_dir, cmd, name, prog, interpreter=interpreter)
        entrypoints[cmd] = str(prog.resolve())

    language = _guess_language(chosen[0])
    m = Manifest(name=name, source=f"local:{src}", method="imported",
                 language=language, entrypoints=entrypoints, description="")
    _describe_imported(cfg, m, chosen, app, log)
    if user_tags:
        m.add_user_tags(user_tags)

    _smoke_test(cfg, m, log)
    manifest.save(tool_dir, m)
    _index(cfg, m)
    console.print(f"[green]✓ imported[/] {name} — commands: {', '.join(entrypoints)}")
    return m


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
    for c in candidates:  # name match
        if c.stem == name or c.name == name:
            return [c]
    if assume_yes:
        return [candidates[0]]
    return _prompt_entries(candidates, app)


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


def _describe_imported(cfg: Config, m: Manifest, progs: list[Path], app: Path, log: Logger) -> None:
    item = {
        "name": m.name,
        "file_type": _file_type(progs[0]),
        "help": _help_head(cfg, m),
        "files": [p.name for p in list(app.iterdir())[:30]],
    }
    vocab = tagmod.vocab_text(cfg.tags_file)
    try:
        batch = agents.describe_local(cfg, [item], vocab, log)
        if batch.items:
            m.description = batch.items[0].description
            m.tags_auto = tagmod.clean(batch.items[0].tags)
        if batch.new_tags:
            tagmod.add_to_vocab(cfg.tags_file, batch.new_tags)
    except InstallError as e:
        err.print(f"[yellow]tagging failed ({e}); imported without tags[/]")


def _file_type(prog: Path) -> str:
    try:
        p = subprocess.run(["file", "-b", str(prog)], capture_output=True, text=True, timeout=10)
        return p.stdout.strip()[:200]
    except (OSError, subprocess.SubprocessError):
        return ""


def _help_head(cfg: Config, m: Manifest) -> str:
    for cmd in m.entrypoints:
        shim = cfg.bin_dir / cmd
        if not shim.exists():
            continue
        for flag in (["--help"], ["-h"]):
            try:
                p = subprocess.run([str(shim), *flag], capture_output=True, text=True, timeout=10)
                text = (p.stdout or p.stderr).strip()
                if text:
                    return "\n".join(text.splitlines()[:8])
            except (OSError, subprocess.SubprocessError):
                pass
    return ""
