"""SQLite connection management, schema and seed accounts.

Concurrency model
------------------
All write transactions take a process-wide lock *and* open with
``BEGIN IMMEDIATE``, so they are serialised and acquire the SQLite reserved
lock before reading.  Conflict-registration and decision-publication therefore
can never interleave: one completes fully before the other starts, and the
latter always observes the former's committed conflicts.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from .errors import ApiError

# One writer at a time inside this process (uvicorn worker / test client).
_WRITE_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('author', 'reviewer', 'editor')),
    api_key     TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS manuscripts (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    author_id   INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    id              INTEGER PRIMARY KEY,
    manuscript_id   INTEGER NOT NULL REFERENCES manuscripts(id),
    revision_no     INTEGER NOT NULL,
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (manuscript_id, revision_no)
);

-- Stable per-manuscript anonymous number for each reviewer.
CREATE TABLE IF NOT EXISTS anonymous_numbers (
    id              INTEGER PRIMARY KEY,
    manuscript_id   INTEGER NOT NULL REFERENCES manuscripts(id),
    reviewer_id     INTEGER NOT NULL REFERENCES users(id),
    anon_number     INTEGER NOT NULL,
    UNIQUE (manuscript_id, reviewer_id),
    UNIQUE (manuscript_id, anon_number)
);

CREATE TABLE IF NOT EXISTS invitations (
    id              INTEGER PRIMARY KEY,
    manuscript_id   INTEGER NOT NULL REFERENCES manuscripts(id),
    revision_id     INTEGER NOT NULL REFERENCES revisions(id),
    reviewer_id     INTEGER NOT NULL REFERENCES users(id),
    status          TEXT NOT NULL CHECK (status IN
                        ('invited', 'accepted', 'reviewed', 'declined', 'blocked')),
    invited_at      TEXT NOT NULL,
    responded_at    TEXT,
    blocked_reason  TEXT,
    UNIQUE (revision_id, reviewer_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id              INTEGER PRIMARY KEY,
    invitation_id   INTEGER NOT NULL UNIQUE REFERENCES invitations(id),
    revision_id     INTEGER NOT NULL REFERENCES revisions(id),
    reviewer_id     INTEGER NOT NULL REFERENCES users(id),
    content         TEXT NOT NULL,
    score           INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
    submitted_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conflicts (
    id          INTEGER PRIMARY KEY,
    author_id   INTEGER NOT NULL REFERENCES users(id),
    reviewer_id INTEGER NOT NULL REFERENCES users(id),
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    -- conflict direction is symmetric, store one canonical row
    UNIQUE (author_id, reviewer_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY,
    manuscript_id   INTEGER NOT NULL REFERENCES manuscripts(id),
    revision_id     INTEGER NOT NULL REFERENCES revisions(id),
    conclusion      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('published', 'withdrawn')),
    published_at    TEXT NOT NULL,
    withdrawn_at    TEXT,
    withdrawal_reason TEXT
);

-- At most one *active* (published, non-withdrawn) decision per revision.
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_decision
    ON decisions (revision_id) WHERE status = 'published';

CREATE TABLE IF NOT EXISTS decision_reviews (
    id              INTEGER PRIMARY KEY,
    decision_id     INTEGER NOT NULL REFERENCES decisions(id),
    review_id       INTEGER NOT NULL REFERENCES reviews(id),
    -- snapshot of the frozen, published content
    anon_number     INTEGER NOT NULL,
    content         TEXT NOT NULL,
    score           INTEGER NOT NULL,
    UNIQUE (decision_id, review_id)
);
"""

# Locally prepared accounts. Keys are static so the role examples are
# directly runnable; rotate them for any real deployment.
SEED_USERS = [
    ("editor1", "Editor Lin", "editor", "key-editor-1"),
    ("author1", "Author Wang", "author", "key-author-1"),
    ("author2", "Author Zhao", "author", "key-author-2"),
    ("reviewer1", "Reviewer Chen", "reviewer", "key-reviewer-1"),
    ("reviewer2", "Reviewer Liu", "reviewer", "key-reviewer-2"),
    ("reviewer3", "Reviewer Sun", "reviewer", "key-reviewer-3"),
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO users (username, display_name, role, api_key)"
            " VALUES (?, ?, ?, ?)",
            SEED_USERS,
        )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def read_tx(db_path: str) -> Iterator[sqlite3.Connection]:
    """Read-only transaction; deferred (shared) lock is sufficient."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.rollback()
    finally:
        conn.close()


@contextmanager
def write_tx(db_path: str) -> Iterator[sqlite3.Connection]:
    """Serialized immediate-write transaction.

    The process lock orders writers; BEGIN IMMEDIATE makes the whole
    read-check-write sequence atomic against other connections.
    """
    with _WRITE_LOCK:
        conn = connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def row_by_id(conn: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row | None:
    return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()


def require_row(conn: sqlite3.Connection, table: str, row_id: int, label: str) -> sqlite3.Row:
    row = row_by_id(conn, table, row_id)
    if row is None:
        raise ApiError(404, "not_found", f"{label} {row_id} does not exist")
    return row
