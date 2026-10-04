"""SQLite + FTS5 search index. Rebuildable from manifests at any time (`tm reindex`)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from tm.manifest import Manifest

SCHEMA = """
CREATE TABLE IF NOT EXISTS tools (
    name TEXT PRIMARY KEY,
    source TEXT, method TEXT, language TEXT, platform TEXT,
    description TEXT, tags TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS tool_tags (name TEXT, tag TEXT, PRIMARY KEY (name, tag));
CREATE VIRTUAL TABLE IF NOT EXISTS tools_fts USING fts5(name, description, tags);
"""


class Registry:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def upsert(self, m: Manifest) -> None:
        tags = m.tags
        with self.db:
            self._delete(m.name)
            self.db.execute(
                "INSERT INTO tools VALUES (?,?,?,?,?,?,?,?)",
                (m.name, m.source, m.method, m.language, m.platform, m.description, " ".join(tags), m.updated_at),
            )
            self.db.executemany("INSERT INTO tool_tags VALUES (?,?)", [(m.name, t) for t in tags])
            self.db.execute(
                "INSERT INTO tools_fts (name, description, tags) VALUES (?,?,?)",
                (m.name, m.description, " ".join(tags)),
            )

    def _delete(self, name: str) -> None:
        self.db.execute("DELETE FROM tools WHERE name=?", (name,))
        self.db.execute("DELETE FROM tool_tags WHERE name=?", (name,))
        self.db.execute("DELETE FROM tools_fts WHERE name=?", (name,))

    def delete(self, name: str) -> None:
        with self.db:
            self._delete(name)

    def clear(self) -> None:
        with self.db:
            for t in ("tools", "tool_tags", "tools_fts"):
                self.db.execute(f"DELETE FROM {t}")

    def search(self, query: str = "", tags: list[str] | None = None,
               method: str | None = None, platform: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT t.* FROM tools t"
        where, args = [], []
        if query:
            sql += " JOIN tools_fts f ON f.name = t.name"
            where.append("tools_fts MATCH ?")
            args.append(_fts_query(query))
        for tag in tags or []:
            where.append("EXISTS (SELECT 1 FROM tool_tags x WHERE x.name=t.name AND x.tag=?)")
            args.append(tag)
        if method:
            where.append("t.method=?")
            args.append(method)
        if platform:
            where.append("(t.platform=? OR t.platform='both')")
            args.append(platform)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY bm25(tools_fts)" if query else " ORDER BY t.name"
        return list(self.db.execute(sql, args))

    def tag_counts(self) -> list[tuple[str, int]]:
        return [(r[0], r[1]) for r in self.db.execute(
            "SELECT tag, COUNT(*) FROM tool_tags GROUP BY tag ORDER BY COUNT(*) DESC, tag")]


def _fts_query(q: str) -> str:
    # prefix-match every word; quote to neutralize FTS syntax characters
    words = [w.replace('"', "") for w in q.split() if w.strip('"')]
    return " ".join(f'"{w}"*' for w in words)
