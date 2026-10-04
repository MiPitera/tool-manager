"""Wrappers around headless Claude Code (`claude -p`). Uses the user's Claude login — no API key needed.

Three roles:
  classifier — no tools, picks the install method from repo context (follows repo's recommended way)
  builder    — Bash/file tools inside the tool's src dir, handles source builds and failed installs
  tagger     — no tools, short descriptions + use-tags for apt packages
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from tm.config import PROMPTS_DIR, Config
from tm.manifest import Method, Recommended
from tm.util import InstallError, Logger


class Classification(BaseModel):
    method: Method
    language: str = ""
    recommended_by_repo: Recommended = Field(default_factory=Recommended)
    deviation_reason: str = ""
    install_steps: list[str] = Field(default_factory=list)
    release_asset: str = ""
    entrypoints: list[str] = Field(default_factory=list)
    go_package: str = ""
    python_version: str = ""
    apt_deps: list[str] = Field(default_factory=list)
    docker_image: str = ""
    platform: Literal["linux", "windows", "both"] = "linux"
    description: str
    tags: list[str] = Field(default_factory=list)
    new_tags: dict[str, str] = Field(default_factory=dict)
    confidence: float = 0.5
    reasoning: str = ""


class BuildResult(BaseModel):
    status: Literal["ok", "need_apt", "failed"]
    entrypoints: dict[str, str] = Field(default_factory=dict)  # command name -> path relative to tool dir
    apt_needed: list[str] = Field(default_factory=list)
    notes: str = ""


class TagItem(BaseModel):
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)


class TagBatch(BaseModel):
    items: list[TagItem]
    new_tags: dict[str, str] = Field(default_factory=dict)


def _prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text()


def _schema(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema())


def run_claude(cfg: Config, *, prompt: str, system: str, schema: type[BaseModel], model: str,
               tools: str = "", cwd: Path | None = None, max_turns: int | None = None,
               log: Logger | None = None, retries: int = 2, timeout: int = 1800):
    if not shutil.which("claude"):
        raise InstallError("`claude` CLI not found — install Claude Code and log in (`claude` → /login)")
    cmd = [
        "claude", "-p", "--model", model, "--output-format", "json",
        "--json-schema", _schema(schema), "--append-system-prompt", system,
        "--tools", tools, *cfg.agents.get("extra_args", []),
    ]
    if tools:
        cmd += ["--allowedTools", tools, "--permission-mode", "acceptEdits"]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    last_err = ""
    for attempt in range(retries + 1):
        p_text = prompt if not last_err else f"{prompt}\n\nPrevious answer was invalid: {last_err}\nAnswer again."
        if log:
            log.write(f"--- claude ({model}) attempt {attempt + 1} ---\n{p_text[:4000]}")
        p = subprocess.run(cmd, input=p_text, capture_output=True, text=True, cwd=cwd, timeout=timeout)
        if log:
            log.write(f"--- claude exit {p.returncode} ---\n{p.stdout[-8000:]}\n{p.stderr[-2000:]}")
        if p.returncode != 0 and not p.stdout:
            last_err = p.stderr.strip()[-500:]
            continue
        try:
            out = json.loads(p.stdout)
            if out.get("is_error"):
                last_err = str(out.get("result"))[:500]
                continue
            data = out.get("structured_output")
            if data is None:
                data = json.loads(out.get("result", ""))
            return schema.model_validate(data)
        except (json.JSONDecodeError, ValidationError, TypeError) as e:
            last_err = str(e)[:500]
    raise InstallError(f"agent failed after {retries + 1} attempts: {last_err}")


def _tag_rule(cfg: Config) -> str:
    n = cfg.agents.get("target_tags", 3)
    return (f"\n## Tag rules\nEvery tag is a SINGLE lowercase word (no spaces; hyphenate only an "
            f"unavoidable compound). Aim for about {n} tags per item.\n")


def classify(cfg: Config, repo_context: str, vocab: str, hint: str, log: Logger) -> Classification:
    prompt = f"{repo_context}\n\n## Tag vocabulary\n{vocab}\n{_tag_rule(cfg)}"
    if hint:
        prompt += f"\n## User constraint\n{hint}\n"
    return run_claude(cfg, prompt=prompt, system=_prompt("classifier"), schema=Classification,
                      model=cfg.model("classifier"), log=log)


def build(cfg: Config, src_dir: Path, tool_dir: Path, steps: list[str], context: str,
          failure: str, model: str, log: Logger) -> BuildResult:
    prompt = (
        f"Tool directory: {tool_dir}\nSource directory (your cwd): {src_dir}\n\n"
        f"Install steps suggested by the repo:\n" + ("\n".join(steps) or "-") +
        (f"\n\nA previous automatic install attempt failed:\n{failure}\n" if failure else "") +
        f"\n\nRepo context:\n{context[:8000]}"
    )
    return run_claude(cfg, prompt=prompt, system=_prompt("builder"), schema=BuildResult, model=model,
                      tools="Bash Read Write Edit Glob Grep", cwd=src_dir,
                      max_turns=cfg.agents.get("builder_max_turns", 40), log=log, retries=1)


def tag_packages(cfg: Config, packages: list[dict], vocab: str, log: Logger | None = None) -> TagBatch:
    lines = [json.dumps(p, ensure_ascii=False) for p in packages]
    prompt = f"## Tag vocabulary\n{vocab}\n{_tag_rule(cfg)}\n## Packages (one JSON per line)\n" + "\n".join(lines)
    return run_claude(cfg, prompt=prompt, system=_prompt("tagger"), schema=TagBatch,
                      model=cfg.model("tagger"), log=log)


def describe_local(cfg: Config, items: list[dict], vocab: str, log: Logger | None = None) -> TagBatch:
    """Describe + tag locally-imported programs (input: name, file_type, help, files)."""
    lines = [json.dumps(p, ensure_ascii=False) for p in items]
    prompt = f"## Tag vocabulary\n{vocab}\n{_tag_rule(cfg)}\n## Programs (one JSON per line)\n" + "\n".join(lines)
    return run_claude(cfg, prompt=prompt, system=_prompt("importer"), schema=TagBatch,
                      model=cfg.model("tagger"), log=log)
