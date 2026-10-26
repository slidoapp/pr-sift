"""SQLite storage for analyses, rankings, review state, comments and
questions."""

import json
import os
import sqlite3
import threading
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
    description TEXT,                -- what the symbol does, when its docstring doesn't say
    attention TEXT,                  -- read | skim; NULL means read
    highlights TEXT,                 -- JSON list of {line, note}
    sections TEXT,                   -- JSON list of {line, title}
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
    ANCHOR,
    body TEXT NOT NULL,
    state TEXT NOT NULL,             -- draft | posted
    github_id INTEGER,               -- the inline comment's id on GitHub
    github_url TEXT,                 -- the comment, or the review whose body holds it
    submission_id INTEGER,           -- the review it was posted in
    posted_at TEXT,
    question_id INTEGER,             -- the question it was drafted from
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY,
    pr_id INTEGER NOT NULL REFERENCES prs (id) ON DELETE CASCADE,
    github_id INTEGER NOT NULL,
    event TEXT NOT NULL,             -- COMMENT | APPROVE | REQUEST_CHANGES
    body TEXT NOT NULL,
    url TEXT,
    commit_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY,
    pr_id INTEGER NOT NULL REFERENCES prs (id) ON DELETE CASCADE,
    ANCHOR,
    status TEXT NOT NULL,            -- queued | running | done | failed | cancelled
    resolved INTEGER NOT NULL DEFAULT 0,
    session_id TEXT,                 -- the agent session follow-ups resume
    workdir TEXT,                    -- where that session ran
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions (id) ON DELETE CASCADE,
    role TEXT NOT NULL,              -- you | agent
    body TEXT NOT NULL,
    sources TEXT NOT NULL,           -- JSON list of {kind, ...}: files read, searches, URLs fetched
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pr_summary (
    pr_id INTEGER PRIMARY KEY REFERENCES prs (id) ON DELETE CASCADE,
    head_sha TEXT,
    overview TEXT NOT NULL,
    architecture_summary TEXT,
    components TEXT NOT NULL,        -- JSON list
    walkthrough TEXT                 -- JSON list of blocks, see ranking.py
);
""".replace(
    "ANCHOR",
    # Where a comment or question attaches, see anchors.py.
    """anchor_kind TEXT NOT NULL,      -- line | range | unit | walkthrough | pr
    qualname TEXT,
    fingerprint TEXT,                -- of the unit, or of the walkthrough block
    side TEXT,                       -- old | new
    start_offset INTEGER,            -- index into the unit's lines on that side
    end_side TEXT,
    end_offset INTEGER,
    block INTEGER,                   -- walkthrough block index
    quote TEXT""",
)
ANCHOR_FIELDS = ("anchor_kind", "qualname", "fingerprint", "side", "start_offset", "end_side", "end_offset", "block", "quote")


def data_dir() -> Path:
    return Path(os.environ.get("REVIEWER_DATA_DIR", "~/.local/share/reviewer")).expanduser()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | None = None):
        if path is None:
            path = data_dir() / "reviewer.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # The report server's request threads and agent runs share one
        # store: the lock keeps their queries and transactions apart.
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._drop_old_comments()
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _drop_old_comments(self) -> None:
        """Earlier versions created a comments table without anchors and
        never wrote to it: replace it."""
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(comments)")}
        if "line_offset" in columns and not self.conn.execute("SELECT 1 FROM comments LIMIT 1").fetchone():
            with self.conn:
                self.conn.execute("DROP TABLE comments")

    def _migrate(self) -> None:
        """Add columns that databases created by earlier versions lack."""
        added = {
            "rankings": ("description", "attention", "highlights", "sections"),
            "pr_summary": ("walkthrough",),
            "comments": ("github_url", "submission_id", "posted_at"),
        }
        with self.conn:
            for table, wanted in added.items():
                columns = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for column in wanted:
                    if column in columns:
                        continue
                    try:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
                    except sqlite3.OperationalError as e:
                        # Another process opening the store added it first.
                        if "duplicate column" not in str(e):
                            raise

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
                INSERT OR REPLACE INTO rankings
                    (fingerprint, tier, reason, summary, description, attention, highlights, sections, model, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r["fingerprint"], r["tier"], r["reason"], r["summary"], r.get("description"),
                        r.get("attention") or "read", json.dumps(r.get("highlights") or []),
                        json.dumps(r.get("sections") or []), model, now(),
                    )
                    for r in rankings
                ],
            )

    def save_summary(
        self,
        pr_id: int,
        head_sha: str,
        overview: str,
        architecture_summary: str | None,
        components: list,
        walkthrough: list | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO pr_summary (pr_id, head_sha, overview, architecture_summary, components, walkthrough)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (pr_id, head_sha, overview, architecture_summary, json.dumps(components), json.dumps(walkthrough or [])),
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

    # Comments and questions. These run in the report server's request
    # threads and in agent runs, so they hold the lock.

    def comments(self, pr_id: int) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute("SELECT * FROM comments WHERE pr_id = ? ORDER BY id", (pr_id,)).fetchall()

    def comment(self, comment_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()

    def add_comment(self, pr_id: int, anchor: dict, body: str, question_id: int | None = None) -> int:
        with self.lock, self.conn:
            return self.conn.execute(
                f"""
                INSERT INTO comments (pr_id, {", ".join(ANCHOR_FIELDS)}, body, state, question_id, created_at, updated_at)
                VALUES (?, {", ".join("?" * len(ANCHOR_FIELDS))}, ?, 'draft', ?, ?, ?)
                """,
                (pr_id, *(anchor[f] for f in ANCHOR_FIELDS), body, question_id, now(), now()),
            ).lastrowid

    def update_comment(self, comment_id: int, body: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE comments SET body = ?, updated_at = ? WHERE id = ?", (body, now(), comment_id))

    def delete_comment(self, comment_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))

    def questions(self, pr_id: int) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute("SELECT * FROM questions WHERE pr_id = ? ORDER BY id", (pr_id,)).fetchall()

    def question(self, question_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()

    def add_question(self, pr_id: int, anchor: dict) -> int:
        with self.lock, self.conn:
            return self.conn.execute(
                f"""
                INSERT INTO questions (pr_id, {", ".join(ANCHOR_FIELDS)}, status, created_at, updated_at)
                VALUES (?, {", ".join("?" * len(ANCHOR_FIELDS))}, 'queued', ?, ?)
                """,
                (pr_id, *(anchor[f] for f in ANCHOR_FIELDS), now(), now()),
            ).lastrowid

    def update_question(self, question_id: int, **fields) -> None:
        allowed = {"status", "resolved", "session_id", "workdir", "error"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"cannot update {sorted(set(fields) - allowed)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self.lock, self.conn:
            self.conn.execute(
                f"UPDATE questions SET {assignments}, updated_at = ? WHERE id = ?",
                (*fields.values(), now(), question_id),
            )

    def delete_question(self, question_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM messages WHERE question_id = ?", (question_id,))
            self.conn.execute("DELETE FROM questions WHERE id = ?", (question_id,))

    def interrupt_questions(self, pr_id: int) -> None:
        """Fail runs left queued or running by a server that stopped."""
        with self.lock, self.conn:
            self.conn.execute(
                """
                UPDATE questions SET status = 'failed', error = ?, updated_at = ?
                WHERE pr_id = ? AND status IN ('queued', 'running')
                """,
                ("The report server stopped while this was running.", now(), pr_id),
            )

    def messages(self, question_ids: Iterable[int]) -> dict[int, list[sqlite3.Row]]:
        ids = list(question_ids)
        found: dict[int, list[sqlite3.Row]] = {i: [] for i in ids}
        with self.lock:
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                query = f"SELECT * FROM messages WHERE question_id IN ({','.join('?' * len(chunk))}) ORDER BY id"
                for row in self.conn.execute(query, chunk):
                    found[row["question_id"]].append(row)
        return found

    def add_message(self, question_id: int, role: str, body: str, sources: list | None = None) -> int:
        with self.lock, self.conn:
            return self.conn.execute(
                "INSERT INTO messages (question_id, role, body, sources, created_at) VALUES (?, ?, ?, ?, ?)",
                (question_id, role, body, json.dumps(sources or []), now()),
            ).lastrowid

    def add_submission(self, pr_id: int, github_id: int, event: str, body: str, url: str | None, commit_id: str) -> int:
        with self.lock, self.conn:
            return self.conn.execute(
                """
                INSERT INTO submissions (pr_id, github_id, event, body, url, commit_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (pr_id, github_id, event, body, url, commit_id, now()),
            ).lastrowid

    def submissions(self, pr_id: int) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute("SELECT * FROM submissions WHERE pr_id = ? ORDER BY id", (pr_id,)).fetchall()

    def mark_posted(self, comment_id: int, submission_id: int, github_id: int | None, url: str | None) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                UPDATE comments SET state = 'posted', submission_id = ?, github_id = ?, github_url = ?,
                    posted_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (submission_id, github_id, url, now(), now(), comment_id),
            )
