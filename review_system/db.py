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

-- -------------------------------------------------------------------------
-- Conference talk scheduling
--
-- A schedule_groups row is one editor submission: either a first-time group
-- of talk arrangements or a linked rescheduling that several authors confirm
-- together.  schedule_items are the per-manuscript proposed slots and each
-- author's separate response.  sessions are the effective, confirmed talks.
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schedule_groups (
    id                       INTEGER PRIMARY KEY,
    confirmation_deadline    TEXT NOT NULL,  -- UTC ISO-8601
    status                   TEXT NOT NULL CHECK (status IN
                                 ('pending', 'confirmed', 'declined',
                                  'expired', 'cancelled')),
    end_reason               TEXT,
    created_at               TEXT NOT NULL,
    ended_at                 TEXT
);

CREATE TABLE IF NOT EXISTS schedule_items (
    id            INTEGER PRIMARY KEY,
    group_id      INTEGER NOT NULL REFERENCES schedule_groups(id),
    manuscript_id INTEGER NOT NULL REFERENCES manuscripts(id),
    -- The specific live published decision this arrangement is based on.
    decision_id   INTEGER NOT NULL REFERENCES decisions(id),
    room          TEXT NOT NULL,             -- canonical config room code
    starts_at     TEXT NOT NULL,             -- UTC ISO-8601
    ends_at       TEXT NOT NULL,
    response      TEXT NOT NULL DEFAULT 'waiting'
                  CHECK (response IN ('waiting', 'accepted', 'declined')),
    responded_at  TEXT,
    UNIQUE (group_id, manuscript_id)
);

CREATE INDEX IF NOT EXISTS ix_schedule_items_group
    ON schedule_items (group_id);
CREATE INDEX IF NOT EXISTS ix_schedule_items_manuscript
    ON schedule_items (manuscript_id);
CREATE INDEX IF NOT EXISTS ix_schedule_items_decision
    ON schedule_items (decision_id);

CREATE TABLE IF NOT EXISTS sessions (
    id                       INTEGER PRIMARY KEY,
    manuscript_id            INTEGER NOT NULL REFERENCES manuscripts(id),
    decision_id              INTEGER NOT NULL REFERENCES decisions(id),
    group_id                 INTEGER REFERENCES schedule_groups(id),
    room                     TEXT NOT NULL,
    starts_at                TEXT NOT NULL,
    ends_at                  TEXT NOT NULL,
    status                   TEXT NOT NULL CHECK (status IN
                                 ('confirmed', 'superseded', 'voided')),
    created_at               TEXT NOT NULL,
    confirmed_at             TEXT,
    ended_at                 TEXT,
    end_reason               TEXT,
    replaced_by_group_id     INTEGER REFERENCES schedule_groups(id)
);

-- At most one confirmed (occupying) talk per manuscript.
CREATE UNIQUE INDEX IF NOT EXISTS ux_confirmed_session
    ON sessions (manuscript_id) WHERE status = 'confirmed';

CREATE INDEX IF NOT EXISTS ix_sessions_decision
    ON sessions (decision_id);
CREATE INDEX IF NOT EXISTS ix_sessions_time
    ON sessions (starts_at, ends_at);

-- -------------------------------------------------------------------------
-- Speaker travel reimbursement (all money is integer cents)
--
-- One allowance per (manuscript, decision).  A claim is the editor's approval
-- of a receipt against one allowance; approved cents become "to be handed to
-- finance" and are placed into payment batches.  Each payment instruction
-- eventually receives exactly one finance result; unpaid remainder is
-- released and never silently re-queued.
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS reimbursement_allowances (
    id            INTEGER PRIMARY KEY,
    manuscript_id INTEGER NOT NULL REFERENCES manuscripts(id),
    decision_id   INTEGER NOT NULL REFERENCES decisions(id),
    beneficiary_id INTEGER NOT NULL REFERENCES users(id),  -- the manuscript's author
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    created_at    TEXT NOT NULL,
    released_at   TEXT,          -- set when the backing decision is withdrawn
    release_reason TEXT,
    UNIQUE (manuscript_id, decision_id)
);

CREATE TABLE IF NOT EXISTS reimbursement_receipts (
    id            INTEGER PRIMARY KEY,
    voucher_no    TEXT NOT NULL UNIQUE,      -- external, editor-supplied
    author_id     INTEGER NOT NULL REFERENCES users(id),
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reimbursement_claims (
    id            INTEGER PRIMARY KEY,
    allowance_id  INTEGER NOT NULL REFERENCES reimbursement_allowances(id),
    receipt_id    INTEGER NOT NULL REFERENCES reimbursement_receipts(id),
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_claims_allowance ON reimbursement_claims (allowance_id);
CREATE INDEX IF NOT EXISTS ix_claims_receipt ON reimbursement_claims (receipt_id);

CREATE TABLE IF NOT EXISTS payment_batches (
    id                INTEGER PRIMARY KEY,
    idempotency_key   TEXT NOT NULL UNIQUE, -- editor-supplied request number
    request_hash      TEXT NOT NULL,        -- canonical content fingerprint
    created_at        TEXT NOT NULL
);

-- One row per approved claim placed into a batch.  The cents are reserved for
-- this delivery instruction until finance reports on it.
CREATE TABLE IF NOT EXISTS payment_instructions (
    id              INTEGER PRIMARY KEY,
    batch_id        INTEGER NOT NULL REFERENCES payment_batches(id),
    claim_id        INTEGER NOT NULL REFERENCES reimbursement_claims(id),
    allowance_id    INTEGER NOT NULL REFERENCES reimbursement_allowances(id),
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    payee_id        INTEGER NOT NULL REFERENCES users(id),
    voucher_no      TEXT NOT NULL,
    payment_no      TEXT NOT NULL UNIQUE,   -- per-instruction payment number
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_instructions_batch ON payment_instructions (batch_id);
CREATE INDEX IF NOT EXISTS ix_instructions_claim ON payment_instructions (claim_id);

-- Exactly one final finance result per payment instruction.
CREATE TABLE IF NOT EXISTS payment_results (
    id                  INTEGER PRIMARY KEY,
    instruction_id      INTEGER NOT NULL UNIQUE REFERENCES payment_instructions(id),
    paid_cents          INTEGER NOT NULL CHECK (paid_cents >= 0),
    released_cents      INTEGER NOT NULL CHECK (released_cents >= 0),
    note                TEXT,
    created_at          TEXT NOT NULL
);

-- -------------------------------------------------------------------------
-- Audience seat reservations
--
-- Editors give each configured room an audience capacity (default zero);
-- every talk uses the capacity of the room its confirmed session is in.
-- A registration attaches to the *talk* (manuscript + the decision era it
-- was registered under), so a confirmed reschedule that reuses the original
-- decision carries the audience list over automatically, while a withdrawn
-- decision terminates the registrations of not-yet-started talks without
-- transferring them to any new decision.  Status lifecycle:
-- waitlisted/seated (active) -> cancelled (by the audience member) or
-- ended (decision withdrawn).  Once a talk starts, its admission list is
-- frozen and never rewritten.
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS room_audience_capacities (
    room_code   TEXT PRIMARY KEY,        -- canonical config room code
    capacity    INTEGER NOT NULL CHECK (capacity >= 0),
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audience_registrations (
    id              INTEGER PRIMARY KEY,
    manuscript_id   INTEGER NOT NULL REFERENCES manuscripts(id),
    -- The decision era of the talk at registration time; registrations never
    -- migrate across decisions.
    decision_id     INTEGER NOT NULL REFERENCES decisions(id),
    audience_id     INTEGER NOT NULL REFERENCES users(id),
    status          TEXT NOT NULL CHECK (status IN
                        ('waitlisted', 'seated', 'cancelled', 'ended')),
    registered_at   TEXT NOT NULL,       -- queue order of this application
    seated_at       TEXT,
    cancelled_at    TEXT,
    ended_at        TEXT,
    end_reason      TEXT,
    created_at      TEXT NOT NULL
);

-- At most one active application per audience member per talk (decision era).
-- id order is the global registration order used for all queue decisions.
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_registration
    ON audience_registrations (decision_id, audience_id)
    WHERE status IN ('waitlisted', 'seated');
CREATE INDEX IF NOT EXISTS ix_registrations_manuscript
    ON audience_registrations (manuscript_id);
CREATE INDEX IF NOT EXISTS ix_registrations_audience
    ON audience_registrations (audience_id);
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


def _migrate_legacy_schema(conn: sqlite3.Connection) -> None:
    """Additive migrations for databases created by an earlier revision.

    ``CREATE TABLE IF NOT EXISTS`` handles new tables; this hook exists so a
    future additive column does not require rebuilding a live database.
    """
    # No column additions yet.
    return



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
        _migrate_legacy_schema(conn)
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
