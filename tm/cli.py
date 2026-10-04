"""Typer CLI for tm."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import typer
from rich.panel import Panel
from rich.table import Table

from tm import config as cfgmod
from tm import core, manifest, tags as tagmod
from tm.registry import Registry
from tm.util import InstallError, console, err

app = typer.Typer(help="tm — agent-assisted tool manager", no_args_is_help=True)
tags_app = typer.Typer(help="Edit tags for a tool")
vocab_app = typer.Typer(help="Manage the tag vocabulary")
app.add_typer(tags_app, name="tag")
app.add_typer(vocab_app, name="tags")


def _cfg():
    cfg = cfgmod.load()
    cfgmod.ensure_layout(cfg)
    return cfg


def _complete_tool(incomplete: str) -> list[str]:
    """Shell completion for cataloged tool names. Must be fast and side-effect free."""
    try:
        cfg = cfgmod.load()  # no ensure_layout: completion must not create anything
        names = []
        if cfg.db_path.exists():
            reg = Registry(cfg.db_path)
            names = [r["name"] for r in reg.search()]
            reg.close()
        if not names:  # fallback: scan manifests
            names = [m.name for _, m in manifest.iter_all(cfg.root)]
        return [n for n in names if n.startswith(incomplete)]
    except Exception:
        return []


def _die(msg: str):
    err.print(f"[red]error:[/] {msg}")
    raise typer.Exit(1)


def _load_tool(cfg, name: str):
    d = cfg.tool_dir(name)
    if not manifest.path_for(d).exists():
        _die(f"no such tool: {name}")
    return d, manifest.load(d)


@app.command()
def setup():
    """Create ~/tools layout, add bin dir to PATH, print the sudo step."""
    from tm.setup import setup as _setup
    _setup(_cfg())


@app.command()
def install(
    target: str = typer.Argument(..., help="GitHub URL/owner-repo, or apt:<pkg>"),
    docker: bool = typer.Option(False, "--docker", help="Force install via Docker"),
    apt_pkg: bool = typer.Option(False, "--apt", help="Treat target as an apt package name"),
    name: str = typer.Option("", "--name", help="Override the tool name"),
    method: str = typer.Option("", "--method", help="Force install method"),
    tag: list[str] = typer.Option([], "--tag", help="Add a user tag (repeatable)"),
    static: bool = typer.Option(False, "--static", help="Compile to a standalone binary for deploying to other hosts"),
    static_tool: str = typer.Option("", "--static-tool", help="Packager for --static: pyinstaller | nuitka"),
    force: bool = typer.Option(False, "--force", help="Catalog an apt package even if it ships no program"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Assume yes to prompts"),
):
    """Install a tool from GitHub (or apt) in its own isolated directory."""
    cfg = _cfg()
    try:
        if apt_pkg or target.startswith("apt:"):
            pkg = target.removeprefix("apt:")
            core.install_apt(cfg, pkg, user_tags=tag, assume_yes=yes, force=force)
        else:
            core.install_from_github(cfg, target, name=name, docker=docker, method=method,
                                     user_tags=tag, static=static, static_tool=static_tool,
                                     assume_yes=yes)
    except (InstallError, ValueError) as e:
        _die(str(e))


@app.command("import")
def import_local(
    path: str = typer.Argument(..., help="Directory (its contents = one tool) or a file (one program)"),
    name: str = typer.Option("", "--name", help="Override the tool name"),
    entry: str = typer.Option("", "--entry", help="Entrypoint file when ambiguous (rel path or basename)"),
    tag: list[str] = typer.Option([], "--tag", help="Add a user tag (repeatable)"),
    copy: bool = typer.Option(False, "--copy", help="Copy instead of moving into ~/tools"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Assume yes to prompts"),
):
    """Import a local program (directory or file) into tm: move to ~/tools, shim, tag."""
    cfg = _cfg()
    try:
        core.import_local(cfg, path, name=name, entry=entry, user_tags=tag, copy=copy, assume_yes=yes)
    except (InstallError, ValueError) as e:
        _die(str(e))


@app.command("import-apt")
def import_apt(yes: bool = typer.Option(False, "--yes", "-y"), batch: int = typer.Option(30, "--batch")):
    """Catalog and auto-tag manually-installed apt packages already on the system."""
    core.import_apt(_cfg(), batch_size=batch, assume_yes=yes)


@app.command("list")
def list_tools(
    method: str = typer.Option("", "--method"),
    platform: str = typer.Option("", "--platform"),
):
    """List installed tools."""
    cfg = _cfg()
    reg = Registry(cfg.db_path)
    rows = reg.search(method=method or None, platform=platform or None)
    reg.close()
    _print_rows(rows)


@app.command()
def search(
    query: str = typer.Argument("", help="Free-text query (FTS over name/description/tags)"),
    tag: list[str] = typer.Option([], "--tag", help="Require this tag (repeatable, AND)"),
    method: str = typer.Option("", "--method"),
    platform: str = typer.Option("", "--platform"),
):
    """Search tools by text and/or tags."""
    cfg = _cfg()
    reg = Registry(cfg.db_path)
    rows = reg.search(query=query, tags=tagmod.clean(tag), method=method or None, platform=platform or None)
    reg.close()
    _print_rows(rows)


def _print_rows(rows):
    if not rows:
        console.print("[dim]no matches[/]")
        return
    t = Table(show_lines=False)
    t.add_column("name", style="bold"); t.add_column("method"); t.add_column("tags"); t.add_column("description")
    for r in rows:
        t.add_row(r["name"], r["method"], r["tags"], (r["description"] or "")[:70])
    console.print(t)


@app.command()
def info(name: str = typer.Argument(..., autocompletion=_complete_tool)):
    """Show everything about one tool."""
    cfg = _cfg()
    d, m = _load_tool(cfg, name)
    t = Table(show_header=False, box=None)
    t.add_row("name", m.name)
    t.add_row("source", m.source)
    t.add_row("method", m.method)
    t.add_row("language", m.language or "-")
    t.add_row("version", m.version or "-")
    t.add_row("platform", m.platform)
    t.add_row("description", m.description)
    t.add_row("tags", ", ".join(m.tags) or "-")
    t.add_row("  user tags", ", ".join(m.tags_user) or "-")
    t.add_row("  removed", ", ".join(m.tags_removed) or "-")
    if m.entrypoints:
        def _annot(cmd):
            os_ = m.entry_os.get(cmd, "")
            return f"→ copy to {os_}" if m.is_foreign(cmd) else "run here"
        t.add_row("commands", "\n".join(f"{k} -> {v}  [{_annot(k)}]" for k, v in m.entrypoints.items()))
    if m.artifacts:
        t.add_row("artifacts", "\n".join(f"{os_} -> {p}" for os_, p in m.artifacts.items()))
    if m.method == "release_windows":
        t.add_row("windows files", str(d / "windows"))
    if m.docker_image:
        t.add_row("docker image", m.docker_image)
    if m.recommended_by_repo.found:
        t.add_row("repo recommends", f'"{m.recommended_by_repo.quote}" ({m.recommended_by_repo.source_file})')
    if m.deviation_reason:
        t.add_row("deviation", m.deviation_reason)
    t.add_row("dir", str(d))
    console.print(Panel(t, title=m.name))


@app.command()
def remove(name: str = typer.Argument(..., autocompletion=_complete_tool), yes: bool = typer.Option(False, "--yes", "-y")):
    """Remove a tool: shims, directory and registry entry (apt packages are uninstalled).

    Also clears an orphaned ~/tools/<name> directory that has no manifest (e.g. a failed import).
    """
    cfg = _cfg()
    name = name.rstrip("/")  # tolerate tab-completed "name/" from a directory
    from tm.util import confirm, Logger
    d = cfg.tool_dir(name)
    m = manifest.load(d) if manifest.path_for(d).exists() else None
    if m is None and not d.exists():
        _die(f"no such tool: {name}")

    label = m.method if m else "untracked directory"
    if not confirm(f"Remove {name} ({label})?", assume_yes=yes):
        raise typer.Exit()
    if m and m.method == "apt":
        from tm.installers import apt as aptmod
        try:
            aptmod.apt_remove(m.apt_package or name, Logger(None))
        except InstallError as e:
            err.print(f"[yellow]apt remove failed: {e}[/]")
    from tm.shims import remove_for
    removed = remove_for(cfg.bin_dir, name)
    if d.exists():
        shutil.rmtree(d)
    reg = Registry(cfg.db_path)
    reg.delete(name)
    reg.close()
    console.print(f"[green]✓ removed[/] {name}" + (f" (shims: {', '.join(removed)})" if removed else ""))


@app.command()
def forget(name: str = typer.Argument(..., autocompletion=_complete_tool), yes: bool = typer.Option(False, "--yes", "-y")):
    """Drop a tool from the catalog WITHOUT deleting it — files and shims stay, it keeps working."""
    cfg = _cfg()
    d, m = _load_tool(cfg, name)
    from tm.util import confirm
    if not confirm(f"Forget {name} (keep files + commands, stop tracking)?", assume_yes=yes):
        raise typer.Exit()
    mf = manifest.path_for(d)
    if mf.exists():
        mf.unlink()
    reg = Registry(cfg.db_path)
    reg.delete(name)
    reg.close()
    still = ", ".join(m.entrypoints) or "none"
    console.print(f"[green]✓ forgot[/] {name} — no longer cataloged; commands still on PATH: {still}\n"
                  f"[dim]files remain in {d} (and {cfg.bin_dir} shims). `tm reindex` won't re-add it.[/]")


@app.command()
def update(name: str = typer.Argument(..., autocompletion=_complete_tool), yes: bool = typer.Option(False, "--yes", "-y")):
    """Update a tool (apt: upgrade; git-based: re-pull + re-run method)."""
    cfg = _cfg()
    d, m = _load_tool(cfg, name)
    from tm.util import Logger
    log = Logger(d / "install.log")
    if m.method == "apt":
        from tm.installers import apt as aptmod
        aptmod.apt_upgrade(m.apt_package or name, log)
        m.version = aptmod.version_installed(m.apt_package or name)
        manifest.save(d, m)
        core._index(cfg, m)
        console.print(f"[green]✓ updated[/] {name} -> {m.version}")
        return
    console.print("[yellow]Re-installing to update (keeps your user tags).[/]")
    saved_user, saved_removed = m.tags_user, m.tags_removed
    url = m.source
    from tm.shims import remove_for
    remove_for(cfg.bin_dir, name)
    shutil.rmtree(d)
    reg = Registry(cfg.db_path); reg.delete(name); reg.close()
    try:
        nm = core.install_from_github(cfg, url, name=name, method=m.method, assume_yes=yes)
        nm.tags_user, nm.tags_removed = saved_user, saved_removed
        manifest.save(cfg.tool_dir(name), nm)
        core._index(cfg, nm)
    except (InstallError, ValueError) as e:
        _die(str(e))


@app.command()
def reindex():
    """Rebuild registry.db from manifests."""
    cfg = _cfg()
    reg = Registry(cfg.db_path)
    reg.clear()
    n = 0
    for _, m in manifest.iter_all(cfg.root):
        reg.upsert(m); n += 1
    reg.close()
    console.print(f"[green]✓ reindexed[/] {n} tools")


@app.command()
def doctor(prune: bool = typer.Option(False, "--prune",
                                      help="Drop cataloged apt entries that ship no program")):
    """Health check, find unmanaged binaries, and (with --prune) drop apt entries with no program."""
    from tm.doctor import doctor as _doctor
    _doctor(_cfg(), prune=prune)


@app.command()
def edit(name: str = typer.Argument(..., autocompletion=_complete_tool)):
    """Open the tool's manifest.json in $EDITOR, then validate + reindex."""
    cfg = _cfg()
    d, _ = _load_tool(cfg, name)
    editor = os.environ.get("EDITOR", "nano")
    subprocess.run([editor, str(manifest.path_for(d))])
    try:
        m = manifest.load(d)  # re-validates
    except Exception as e:
        _die(f"manifest invalid after edit: {e}")
    core._index(cfg, m)
    console.print(f"[green]✓ saved[/] {name} — tags: {', '.join(m.tags) or '-'}")


@app.command()
def retag(name: str = typer.Argument("", help="Tool name, or empty with --all", autocompletion=_complete_tool),
          all_: bool = typer.Option(False, "--all", help="Retag every tool")):
    """Re-run auto-tagging (keeps user tags and removals)."""
    cfg = _cfg()
    from tm.installers import apt as aptmod
    from tm import agents
    from tm.util import Logger
    targets = [m for _, m in manifest.iter_all(cfg.root)] if all_ else [_load_tool(cfg, name)[1]]
    vocab = tagmod.vocab_text(cfg.tags_file)
    for m in targets:
        if m.method != "apt":
            console.print(f"[yellow]skip {m.name}: retag currently supports apt tools only[/]")
            continue
        meta = aptmod.pkg_metadata(m.apt_package or m.name)
        try:
            tb = agents.tag_packages(cfg, [meta], vocab, Logger(None))
            if tb.items:
                m.tags_auto = tagmod.clean(tb.items[0].tags)
            if tb.new_tags:
                tagmod.add_to_vocab(cfg.tags_file, tb.new_tags)
        except InstallError as e:
            err.print(f"[yellow]{m.name}: {e}[/]")
            continue
        manifest.save(cfg.tool_dir(m.name), m)
        core._index(cfg, m)
        console.print(f"  {m.name}: {', '.join(m.tags)}")


# ---- `tm tag ...`

@tags_app.command("add")
def tag_add(name: str = typer.Argument(..., autocompletion=_complete_tool), tags: list[str] = typer.Argument(...)):
    cfg = _cfg()
    d, m = _load_tool(cfg, name)
    m.add_user_tags(tags)
    manifest.save(d, m); core._index(cfg, m)
    console.print(f"tags: {', '.join(m.tags)}")


@tags_app.command("rm")
def tag_rm(name: str = typer.Argument(..., autocompletion=_complete_tool), tags: list[str] = typer.Argument(...)):
    cfg = _cfg()
    d, m = _load_tool(cfg, name)
    m.remove_tags(tags)
    manifest.save(d, m); core._index(cfg, m)
    console.print(f"tags: {', '.join(m.tags)}")


@tags_app.command("set")
def tag_set(name: str = typer.Argument(..., autocompletion=_complete_tool), tags: list[str] = typer.Argument(...)):
    cfg = _cfg()
    d, m = _load_tool(cfg, name)
    m.set_tags(tags)
    manifest.save(d, m); core._index(cfg, m)
    console.print(f"tags: {', '.join(m.tags)}")


# ---- `tm tags ...`  (vocabulary / global ops)

@vocab_app.command("list")
def vocab_list():
    cfg = _cfg()
    reg = Registry(cfg.db_path)
    t = Table("tag", "tools")
    for tag, n in reg.tag_counts():
        t.add_row(tag, str(n))
    reg.close()
    console.print(t)


@vocab_app.command("rename")
def vocab_rename(old: str, new: str):
    _retag_global(_cfg(), {tagmod.normalize(old): tagmod.normalize(new)})


@vocab_app.command("merge")
def vocab_merge(source: str, into: str):
    _retag_global(_cfg(), {tagmod.normalize(source): tagmod.normalize(into)})


def _retag_global(cfg, mapping: dict[str, str]):
    changed = 0
    for d, m in manifest.iter_all(cfg.root):
        def remap(lst):
            return [mapping.get(t, t) for t in lst]
        before = (m.tags_auto, m.tags_user, m.tags_removed)
        m.tags_auto = tagmod.clean(remap(m.tags_auto))
        m.tags_user = tagmod.clean(remap(m.tags_user))
        m.tags_removed = tagmod.clean(remap(m.tags_removed))
        if (m.tags_auto, m.tags_user, m.tags_removed) != before:
            manifest.save(d, m); core._index(cfg, m); changed += 1
    console.print(f"[green]✓ updated {changed} tools[/]")


if __name__ == "__main__":
    app()
