"""SQLite storage for analyses, rankings, review state and comments."""

import json
import os
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from reviewer.units import Unit

SCHEMA = """
CREATE TABLE IF NOT EXISTS prs (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,              -- host/owner/name
    number INTEGER NOT NULL,
    title TEXT,
    url TEXT,
    base_sha TEXT,
    head_sha TEXT,
    merge_base TEXT,
    analyzed_at TEXT,
    analysis TEXT,                   -- JSON: architecture, interfaces, counts
    UNIQUE (repo, number)
);
CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY,
    pr_id INTEGER NOT NULL REFERENCES prs (id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    old_path TEXT,
    qualname TEXT NOT NULL,
    old_qualname TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    old_body TEXT NOT NULL,
    new_body TEXT NOT NULL,
    old_lines TEXT NOT NULL,         -- JSON list of line numbers
    new_lines TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    noise TEXT,
    signals TEXT NOT NULL            -- JSON list
);
CREATE INDEX IF NOT EXISTS units_pr ON units (pr_id);
CREATE TABLE IF NOT EXISTS rankings (
    fingerprint TEXT PRIMARY KEY,
    tier TEXT NOT NULL,
    reason TEXT NOT NULL,
    summary TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    pr_id INTEGER NOT NULL REFERENCES prs (id) ON DELETE CASCADE,
    qualname TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    reviewed_body TEXT NOT NULL,
    status TEXT NOT NULL,            -- reviewed | flagged
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS reviews_unit ON reviews (pr_id, qualname, status);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY,
    pr_id INTEGER NOT NULL REFERENCES prs (id) ON DELETE CASCADE,
    qualname TEXT NOT NULL,
    fingerprint TEXT,
    line_offset INTEGER,
    side TEXT,
    body TEXT NOT NULL,
    state TEXT NOT NULL,             -- draft | posted | orphaned
    github_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pr_summary (
    pr_id INTEGER PRIMARY KEY REFERENCES prs (id) ON DELETE CASCADE,
    head_sha TEXT,
    overview TEXT NOT NULL,
    architecture_summary TEXT,
    components TEXT NOT NULL         -- JSON list
);
"""


def data_dir() -> Path:
    return Path(os.environ.get("REVIEWER_DATA_DIR", "~/.local/share/reviewer")).expanduser()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | None = None):
        if path is None:
            path = data_dir() / "reviewer.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        # The report server may run in another thread than the one that
        # opened the store; it handles one request at a time.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def save_analysis(self, repo: str, number: int, pr: dict, units: list[Unit], analysis: dict) -> int:
        """Store a PR's latest analysis, replacing its previous units."""
        with self.conn:
            pr_id = self.conn.execute(
                """
                INSERT INTO prs (repo, number, title, url, base_sha, head_sha, merge_base, analyzed_at, analysis)
                VALUES (:repo, :number, :title, :url, :base_sha, :head_sha, :merge_base, :analyzed_at, :analysis)
                ON CONFLICT (repo, number) DO UPDATE SET
                    title = excluded.title, url = excluded.url, base_sha = excluded.base_sha,
                    head_sha = excluded.head_sha, merge_base = excluded.merge_base,
                    analyzed_at = excluded.analyzed_at, analysis = excluded.analysis
                RETURNING id
                """,
                {
                    "repo": repo,
                    "number": number,
                    "title": pr.get("title"),
                    "url": pr.get("url"),
                    "base_sha": pr.get("base_sha"),
                    "head_sha": pr.get("head_sha"),
                    "merge_base": pr.get("merge_base"),
                    "analyzed_at": now(),
                    "analysis": json.dumps(analysis),
                },
            ).fetchone()[0]
            self.conn.execute("DELETE FROM units WHERE pr_id = ?", (pr_id,))
            self.conn.executemany(
                """
                INSERT INTO units (pr_id, path, old_path, qualname, old_qualname, kind, status, old_body,
                                   new_body, old_lines, new_lines, fingerprint, noise, signals)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        pr_id, u.path, u.old_path, u.qualname, u.old_qualname, u.kind, u.status, u.old_body,
                        u.new_body, json.dumps(u.old_lines), json.dumps(u.new_lines), u.fingerprint, u.noise,
                        json.dumps(u.signals),
                    )
                    for u in units
                ],
            )
        return pr_id

    def pr(self, repo: str, number: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM prs WHERE repo = ? AND number = ?", (repo, number)).fetchone()

    def prs(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM prs ORDER BY analyzed_at DESC").fetchall()

    def units(self, pr_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM units WHERE pr_id = ? ORDER BY id", (pr_id,)).fetchall()

    def rankings(self, fingerprints: Iterable[str]) -> dict[str, sqlite3.Row]:
        fingerprints = list(set(fingerprints))
        rows = {}
        # Stay below SQLite's limit on query parameters.
        for i in range(0, len(fingerprints), 500):
            chunk = fingerprints[i : i + 500]
            query = f"SELECT * FROM rankings WHERE fingerprint IN ({','.join('?' * len(chunk))})"
            rows |= {r["fingerprint"]: r for r in self.conn.execute(query, chunk)}
        return rows

    def save_rankings(self, rankings: list[dict], model: str | None) -> None:
        with self.conn:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO rankings (fingerprint, tier, reason, summary, model, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [(r["fingerprint"], r["tier"], r["reason"], r["summary"], model, now()) for r in rankings],
            )

    def save_summary(self, pr_id: int, head_sha: str, overview: str, architecture_summary: str | None, components: list) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO pr_summary (pr_id, head_sha, overview, architecture_summary, components)
                VALUES (?, ?, ?, ?, ?)
                """,
                (pr_id, head_sha, overview, architecture_summary, json.dumps(components)),
            )

    def summary(self, pr_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM pr_summary WHERE pr_id = ?", (pr_id,)).fetchone()

    def pr_by_id(self, pr_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM prs WHERE id = ?", (pr_id,)).fetchone()

    def reviews(self, pr_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM reviews WHERE pr_id = ?", (pr_id,)).fetchall()

    def set_review(self, pr_id: int, qualname: str, fingerprint: str, body: str, status: str) -> None:
        """Record that a unit was reviewed or flagged, as it is now."""
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO reviews (pr_id, qualname, fingerprint, reviewed_body, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (pr_id, qualname, status) DO UPDATE SET
                    fingerprint = excluded.fingerprint, reviewed_body = excluded.reviewed_body,
                    created_at = excluded.created_at
                """,
                (pr_id, qualname, fingerprint, body, status, now()),
            )

    def clear_review(self, pr_id: int, qualname: str, status: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM reviews WHERE pr_id = ? AND qualname = ? AND status = ?", (pr_id, qualname, status)
            )
