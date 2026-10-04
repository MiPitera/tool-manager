"""Deterministic repo context gathering via the GitHub REST API."""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

import httpx

API = "https://api.github.com"

MARKER_FILES = [
    "Dockerfile", "docker-compose.yml", "compose.yml", "pyproject.toml", "setup.py",
    "setup.cfg", "requirements.txt", "Pipfile", "go.mod", "Cargo.toml", "Makefile",
    "CMakeLists.txt", "configure", "meson.build", "package.json", "build.sh", "install.sh",
]
INSTALL_DOC_RE = re.compile(r"^(install|installation|setup|building|build)(\.(md|rst|txt))?$", re.I)
SECTION_RE = re.compile(r"(install|setup|usage|docker|build|requirement|getting started|quick ?start)", re.I)

README_MAX = 6000
SECTIONS_MAX = 12000
DOC_MAX = 6000


@dataclass
class RepoContext:
    owner: str
    repo: str
    url: str
    description: str = ""
    topics: list[str] = field(default_factory=list)
    languages: dict[str, int] = field(default_factory=dict)
    default_branch: str = "main"
    root_files: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    release_tag: str = ""
    release_assets: list[dict] = field(default_factory=list)  # {name, url, size}
    readme_install_sections: str = ""
    readme_head: str = ""
    install_docs: dict[str, str] = field(default_factory=dict)

    def as_prompt(self) -> str:
        parts = [
            f"Repository: {self.url}",
            f"Description: {self.description}",
            f"Topics: {', '.join(self.topics) or '-'}",
            f"Languages (bytes): {self.languages}",
            f"Root files: {', '.join(self.root_files)}",
            f"Build/packaging markers present: {', '.join(self.markers) or '-'}",
            f"Latest release: {self.release_tag or 'none'}",
        ]
        if self.release_assets:
            parts.append("Release assets:\n" + "\n".join(f"  - {a['name']} ({a['size']} B)" for a in self.release_assets))
        if self.readme_install_sections:
            parts.append("README install/usage sections (verbatim):\n" + self.readme_install_sections)
        if self.readme_head:
            parts.append("README beginning:\n" + self.readme_head)
        for name, text in self.install_docs.items():
            parts.append(f"File {name}:\n{text}")
        return "\n\n".join(parts)


def parse_url(url: str) -> tuple[str, str]:
    m = re.search(r"(?:https?://)?(?:[\w.-]+@)?(?:www\.)?github\.com[/:]([^/]+)/([^/#?]+)", url.strip())
    if not m:
        m = re.match(r"^([\w.-]+)/([\w.-]+)$", url.strip())
    if not m:
        raise ValueError(f"not a GitHub repo URL: {url}")
    return m.group(1), m.group(2).removesuffix(".git")


def _token() -> str | None:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    if shutil.which("gh"):
        p = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    return None


def client() -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "tm-tool-manager"}
    if tok := _token():
        headers["Authorization"] = f"Bearer {tok}"
    return httpx.Client(base_url=API, headers=headers, timeout=30, follow_redirects=True)


def extract_sections(markdown: str, limit: int = SECTIONS_MAX) -> str:
    """Return README sections whose heading mentions install/setup/usage/docker/build, verbatim."""
    out, keep, level = [], False, 0
    for line in markdown.splitlines():
        h = re.match(r"^(#{1,6})\s+(.*)", line)
        if h:
            lvl = len(h.group(1))
            if keep and lvl <= level:
                keep = False
            if not keep and SECTION_RE.search(h.group(2)):
                keep, level = True, lvl
        if keep:
            out.append(line)
    return "\n".join(out)[:limit]


def _file(c: httpx.Client, owner: str, repo: str, path: str) -> str:
    r = c.get(f"/repos/{owner}/{repo}/contents/{path}")
    if r.status_code != 200:
        return ""
    data = r.json()
    if isinstance(data, dict) and data.get("encoding") == "base64":
        return base64.b64decode(data["content"]).decode("utf-8", "replace")
    return ""


def gather(url: str) -> RepoContext:
    owner, repo = parse_url(url)
    with client() as c:
        r = c.get(f"/repos/{owner}/{repo}")
        if r.status_code == 404:
            raise ValueError(f"repo not found: {owner}/{repo}")
        r.raise_for_status()
        info = r.json()
        ctx = RepoContext(
            owner=owner, repo=info["name"], url=info["html_url"],
            description=info.get("description") or "",
            topics=info.get("topics") or [],
            default_branch=info.get("default_branch") or "main",
        )
        lr = c.get(f"/repos/{owner}/{repo}/languages")
        if lr.status_code == 200:
            ctx.languages = lr.json()

        cr = c.get(f"/repos/{owner}/{repo}/contents/")
        entries = cr.json() if cr.status_code == 200 else []
        ctx.root_files = [e["name"] + ("/" if e["type"] == "dir" else "") for e in entries]
        names = {e["name"] for e in entries}
        ctx.markers = [m for m in MARKER_FILES if m in names]
        ctx.markers += [n for n in names if n.endswith((".sln", ".csproj"))]

        rel = c.get(f"/repos/{owner}/{repo}/releases/latest")
        if rel.status_code == 200:
            rj = rel.json()
            ctx.release_tag = rj.get("tag_name", "")
            ctx.release_assets = [
                {"name": a["name"], "url": a["browser_download_url"], "size": a["size"]}
                for a in rj.get("assets", [])
            ]

        rd = c.get(f"/repos/{owner}/{repo}/readme")
        if rd.status_code == 200:
            readme = base64.b64decode(rd.json()["content"]).decode("utf-8", "replace")
            ctx.readme_install_sections = extract_sections(readme)
            ctx.readme_head = readme[:README_MAX] if not ctx.readme_install_sections else readme[:2000]

        doc_paths = [e["name"] for e in entries if e["type"] == "file" and INSTALL_DOC_RE.match(e["name"])]
        if "docs" in names:
            dr = c.get(f"/repos/{owner}/{repo}/contents/docs")
            if dr.status_code == 200:
                doc_paths += [f"docs/{e['name']}" for e in dr.json()
                              if e["type"] == "file" and INSTALL_DOC_RE.match(e["name"])]
        for p in doc_paths[:4]:
            if text := _file(c, owner, repo, p):
                ctx.install_docs[p] = text[:DOC_MAX]
        for script in ("install.sh",):
            if script in names and (text := _file(c, owner, repo, script)):
                ctx.install_docs[script] = text[:DOC_MAX]
    return ctx
