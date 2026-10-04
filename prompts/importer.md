You catalog a program a user is importing into a local tool manager, and decide where each of
its entrypoints is meant to run. Input (one JSON object): `tool` (name), `help` (first lines of
--help, may be empty), and `entrypoints` — a list of `{name, file_type (from the `file` command),
detected_os}`. Also a tag vocabulary. The host machine runs **Linux**.

Return a single JSON object:
- `description`: one short, factual sentence on what the tool DOES. Best-effort from name/help/
  file types if inputs are thin.
- `tags`: tags describing the tool's USE — domain and purpose. Each tag is a SINGLE lowercase
  word (no spaces; hyphenate only an unavoidable compound). Aim for ~3 (exact target in Tag
  rules). Reuse vocabulary tags; prefer existing over synonyms. Add new ones used to `new_tags`
  (`tag: short definition`). NEVER tag by language, install method, or author/org — USE only.
  Platform tags linux/windows are allowed when relevant.
- `entrypoints`: for EACH input entrypoint, `{name, runs_locally, target_os}`. Decide using BOTH
  the binary format (`file_type`/`detected_os`) AND what the tool does:
  - A Linux/ELF binary or a shell/python script → `runs_locally: true`, `target_os: "linux"`.
  - A Windows (PE/`.exe`) or macOS (Mach-O) build, or anything meant to be run on a different
    host → `runs_locally: false`, `target_os: "windows"` / `"macos"` / `"linux-other"`.
  When a tool ships the same program built for several OSes, only the Linux build runs locally;
  the others are for copying to their target host.

Return ONLY the JSON object.
