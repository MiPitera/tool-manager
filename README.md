# tm — agent-assisted tool manager

Install any tool with one command — `tm install <github-url>` — and get back a single
runnable command, isolated, cataloged, tagged and searchable. Built for a Debian VM.

**Idea:** deterministic code does the installing; a Claude agent only *decides the method*
and *handles the hard cases* (building from source). Uses your Claude Code login — **no API
key or credits needed** (Pro works).

## Install

```sh
git clone <this-repo> ~/tool-manager
~/tool-manager/install-tm.sh      # installs uv, optional docker/go/build-essential, tm shim
# new shell, then:
tm setup                          # PATH + prints the one sudo command for sudo-compatibility
```

`claude` must be installed and logged in (`claude` → `/login`). Headless VM: run
`claude setup-token` on a desktop and export `CLAUDE_CODE_OAUTH_TOKEN` on the VM.

## Use

```sh
tm install https://github.com/ffuf/ffuf        # picks release_binary -> `ffuf`
tm install OWNER/REPO --docker                 # force docker; `repo args` instead of docker run…
tm install OWNER/REPO --method source_build    # force a path
tm install --apt nmap                          # install + catalog an apt package
tm install OWNER/REPO --tag web --tag recon    # add your own tags up front

tm import /path/to/binary --name tool          # import a local file  -> `tool`
tm import /path/to/dir                          # dir contents = one tool (auto entrypoint)
tm import /path/to/dir --entry bin/app --copy  # pick entrypoint, keep the original

tm list                          tm list --method apt
tm search kerberos               tm search --tag windows --tag ad
tm info ffuf                     tm doctor          # health + find your old scattered binaries
tm import-apt                    # catalog + auto-tag packages you already apt-installed
tm doctor --prune                # drop cataloged apt entries that ship no program

tm tag add ffuf web             tm tag rm ffuf dns       tm tag set ffuf web fuzzing
tm edit ffuf                    # manifest.json in $EDITOR
tm tags list                    tm tags rename old new   tm tags merge a b
tm update ffuf                  tm remove ffuf           tm reindex
tm forget ffuf                  # drop from catalog but KEEP files + commands working
```

## How it works

```
tm install <url>
  ├─ gather repo context (GitHub API: README install sections, markers, release assets)
  ├─ Agent 1 "classifier" (sonnet, no tools): reads the repo's RECOMMENDED install method,
  │     maps it to one of: release_binary | release_windows | uv_project | uv_script |
  │     go_install | docker | source_build. Also: description + use-tags.
  ├─ show plan, confirm
  ├─ deterministic installer runs the chosen method
  │     └─ on failure / source_build → Agent 2 "builder" (sonnet→opus, Bash+files in src/,
  │        no sudo; asks for apt deps via need_apt)
  ├─ smoke test (`cmd --help`)
  └─ write manifest.json + index into registry.db (SQLite FTS5)
```

### Layout

- Code: `~/tool-manager/` (this repo).
- Data: `~/tools/` — `config.toml`, `registry.db`, `tags.txt`, `bin/` (shims on PATH),
  `.python/` (uv interpreters, visible to root), and one dir per tool holding
  `manifest.json` + sources/venv/binaries (`app/` for imported tools) + `install.log`.

### Importing local programs

`tm import <path>` catalogs something already on disk. A **file** is imported as one program; a
**directory**'s contents are treated as a single tool (no recursion). The target is moved into
`~/tools/<name>/app/` (use `--copy` to keep the original), the entrypoint is auto-detected (ELF
binaries, `+x` files, shebang scripts, `.py` with a main; override with `--entry`), shimmed with
absolute paths, and described + tagged by the agent. `tm doctor` lists unmanaged binaries you can
pull in this way.

### apt — programs, not packages

The catalog holds things you can **run**. `tm import-apt` and `interesting_packages` skip any
package that ships no executable in a standard bin dir (`/usr/bin`, `/usr/sbin`, …) — so
libraries and data packages like `libobasis*-librelogo` or `python3-requests` never get
cataloged, while `libreoffice` (which does ship a program) still does. `tm install --apt <pkg>`
on a package with no program warns and asks before cataloging (`--force` to skip the prompt);
either way the package stays installed on the system. `tm doctor` flags any already-cataloged
apt entry that isn't a program, and `tm doctor --prune` drops those catalog entries (the
packages themselves are left installed).

### sudo compatibility

Shims are `/bin/sh` scripts with **absolute paths only**, so they work identically under
`sudo`. `tm setup` prints one command to add `~/tools/bin` to sudo's `secure_path`
(tm never edits `/etc/sudoers` itself).

### Tags

Tags describe **what a tool is for** (`recon`, `web`, `ad`, `windows`, …) — never its
language, install method or source (those are manifest fields, filter with `--method`).
Each tag is a **single lowercase word**; the tagger aims for ~3 per tool (configurable via
`[agents] target_tags`). Auto-tagged by the agent from a curated vocabulary (`~/tools/tags.txt`);
fully editable.

`tm remove` deletes a tool entirely (apt packages are uninstalled); `tm forget` only drops it
from the catalog — the files stay in `~/tools/<name>/` and the commands stay on PATH, so it keeps
working, it's just no longer tracked (and `tm reindex` won't bring it back).

### Config (`~/tools/config.toml`)

Model per role (`classifier`/`builder`/`builder_escalation`/`tagger`), builder turn limit,
docker run flags. Models are aliases (`sonnet`/`opus`/`haiku`), so they track latest versions.

## Tests

```sh
uv run --with pytest --with typer --with rich --with httpx --with pydantic pytest tests/ -q
```

Covers asset selection, shim generation (incl. sudo/absolute-path + docker), tag merge rules,
registry/FTS, and agent JSON parsing (with a fake `claude`). No network or model calls.
