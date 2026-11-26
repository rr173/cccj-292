"""Business rules for the anonymous review workflow.

Every mutating rule runs inside one serialized ``BEGIN IMMEDIATE``
transaction (see :mod:`review_system.db`), so read-check-write sequences such
as "no conflict at invite time" and "all selected reviews still conflict-free
at publication time" cannot race.
"""

from __future__ import annotations

import sqlite3

from . import db as database
from .db import require_row, utcnow
from .errors import ApiError
from .security import (
    author_decision_payload,
    author_withdrawn_payload,
    editor_decision_payload,
    manuscript_detail,
    reviewer_invitation_payload,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _get_manuscript_for_author(conn: sqlite3.Connection, manuscript_id: int, user: sqlite3.Row) -> sqlite3.Row:
    manuscript = require_row(conn, "manuscripts", manuscript_id, "manuscript")
    if manuscript["author_id"] != user["id"]:
        # Do not leak existence of other authors' manuscripts.
        raise ApiError(404, "not_found", "manuscript not found")
    return manuscript


def _has_conflict(conn: sqlite3.Connection, author_id: int, reviewer_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM conflicts WHERE author_id = ? AND reviewer_id = ?",
        (author_id, reviewer_id),
    ).fetchone()


def _current_conflict_for_manuscript(
    conn: sqlite3.Connection, manuscript_id: int, reviewer_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT c.* FROM conflicts c"
        " JOIN manuscripts m ON m.author_id = c.author_id"
        " WHERE m.id = ? AND c.reviewer_id = ?",
        (manuscript_id, reviewer_id),
    ).fetchone()


def _ensure_unique_review_ids(ids: list[int]) -> None:
    if len(set(ids)) != len(ids):
        raise ApiError(400, "duplicate_review", "each review may be selected at most once")


# ---------------------------------------------------------------------------
# Author services
# ---------------------------------------------------------------------------

def create_manuscript(db_path: str, user: sqlite3.Row, title: str, content: str) -> dict:
    now = utcnow()
    with database.write_tx(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO manuscripts (title, author_id, created_at) VALUES (?, ?, ?)",
            (title, user["id"], now),
        )
        manuscript_id = cur.lastrowid
        conn.execute(
            "INSERT INTO revisions (manuscript_id, revision_no, content, created_at)"
            " VALUES (?, 1, ?, ?)",
            (manuscript_id, content, now),
        )
        row = require_row(conn, "manuscripts", manuscript_id, "manuscript")
        return {
            "manuscript_id": manuscript_id,
            "title": row["title"],
            "created_at": row["created_at"],
            "first_revision": {"revision_id": 1, "revision_no": 1, "content": content},
        }


def add_revision(db_path: str, user: sqlite3.Row, manuscript_id: int, content: str) -> dict:
    with database.write_tx(db_path) as conn:
        manuscript = _get_manuscript_for_author(conn, manuscript_id, user)
        last = conn.execute(
            "SELECT COALESCE(MAX(revision_no), 0) AS n FROM revisions WHERE manuscript_id = ?",
            (manuscript_id,),
        ).fetchone()["n"]
        new_no = last + 1
        cur = conn.execute(
            "INSERT INTO revisions (manuscript_id, revision_no, content, created_at)"
            " VALUES (?, ?, ?, ?)",
            (manuscript_id, new_no, content, utcnow()),
        )
        # Old rows are never updated: revision content is append-only.
        return {
            "manuscript_id": manuscript_id,
            "title": manuscript["title"],
            "revision_id": cur.lastrowid,
            "revision_no": new_no,
            "content": content,
            "created_at": utcnow(),
        }


def list_my_manuscripts(db_path: str, user: sqlite3.Row) -> list[dict]:
    with database.read_tx(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM manuscripts WHERE author_id = ? ORDER BY id", (user["id"],)
        ).fetchall()
        result = []
        for m in rows:
            revisions = conn.execute(
                "SELECT id, revision_no, created_at FROM revisions"
                " WHERE manuscript_id = ? ORDER BY revision_no",
                (m["id"],),
            ).fetchall()
            result.append(
                {
                    "manuscript_id": m["id"],
                    "title": m["title"],
                    "created_at": m["created_at"],
                    "revisions": [dict(r) for r in revisions],
                }
            )
        return result


def get_author_manuscript(db_path: str, user: sqlite3.Row, manuscript_id: int) -> dict:
    with database.read_tx(db_path) as conn:
        _get_manuscript_for_author(conn, manuscript_id, user)
        manuscript = require_row(conn, "manuscripts", manuscript_id, "manuscript")
        return manuscript_detail(conn, manuscript, include_author=False)


def list_author_decisions(db_path: str, user: sqlite3.Row) -> list[dict]:
    with database.read_tx(db_path) as conn:
        rows = conn.execute(
            "SELECT d.* FROM decisions d"
            " JOIN manuscripts m ON m.id = d.manuscript_id"
            " WHERE m.author_id = ? ORDER BY d.id",
            (user["id"],),
        ).fetchall()
        result = []
        for d in rows:
            if d["status"] == "withdrawn":
                result.append(author_withdrawn_payload(d))
            else:
                snaps = conn.execute(
                    "SELECT anon_number, score, content FROM decision_reviews"
                    " WHERE decision_id = ? ORDER BY anon_number",
                    (d["id"],),
                ).fetchall()
                result.append(author_decision_payload(d, [dict(s) for s in snaps]))
        return result


def get_author_decision(db_path: str, user: sqlite3.Row, decision_id: int) -> dict:
    with database.read_tx(db_path) as conn:
        decision = require_row(conn, "decisions", decision_id, "decision")
        manuscript = require_row(conn, "manuscripts", decision["manuscript_id"], "manuscript")
        if manuscript["author_id"] != user["id"]:
            raise ApiError(404, "not_found", "decision not found")
        if decision["status"] == "withdrawn":
            raise ApiError(410, "withdrawn", "this published version has been withdrawn")
        snaps = conn.execute(
            "SELECT anon_number, score, content FROM decision_reviews"
            " WHERE decision_id = ? ORDER BY anon_number",
            (decision_id,),
        ).fetchall()
        return author_decision_payload(decision, [dict(s) for s in snaps])


# ---------------------------------------------------------------------------
# Reviewer services
# ---------------------------------------------------------------------------

def list_my_invitations(db_path: str, user: sqlite3.Row) -> list[dict]:
    with database.read_tx(db_path) as conn:
        rows = conn.execute(
            "SELECT i.*, m.title, r.revision_no, r.content, an.anon_number"
            " FROM invitations i"
            " JOIN manuscripts m ON m.id = i.manuscript_id"
            " JOIN revisions r ON r.id = i.revision_id"
            " JOIN anonymous_numbers an ON an.manuscript_id = m.id AND an.reviewer_id = i.reviewer_id"
            " WHERE i.reviewer_id = ? ORDER BY i.id",
            (user["id"],),
        ).fetchall()
        result = []
        for row in rows:
            review = conn.execute(
                "SELECT * FROM reviews WHERE invitation_id = ?", (row["id"],)
            ).fetchone()
            result.append(reviewer_invitation_payload(row, review))
        return result


def _load_invitation_for_reviewer(
    conn: sqlite3.Connection, invitation_id: int, user: sqlite3.Row
) -> sqlite3.Row:
    invitation = require_row(conn, "invitations", invitation_id, "invitation")
    if invitation["reviewer_id"] != user["id"]:
        # Another reviewer's invitation must not be readable by guessing ids.
        raise ApiError(404, "not_found", "invitation not found")
    return invitation


def respond_invitation(
    db_path: str, user: sqlite3.Row, invitation_id: int, accept: bool
) -> dict:
    with database.write_tx(db_path) as conn:
        invitation = _load_invitation_for_reviewer(conn, invitation_id, user)
        if invitation["status"] in ("accepted", "reviewed"):
            raise ApiError(409, "invalid_state", f"invitation already {invitation['status']}")
        if invitation["status"] == "declined":
            raise ApiError(409, "invalid_state", "invitation already declined")
        if invitation["status"] == "blocked":
            raise ApiError(
                409,
                "conflict_blocked",
                invitation["blocked_reason"]
                or "a conflict of interest blocks this invitation",
            )

        if not accept:
            conn.execute(
                "UPDATE invitations SET status = 'declined', responded_at = ? WHERE id = ?",
                (utcnow(), invitation_id),
            )
            return {"invitation_id": invitation_id, "status": "declined"}

        # Acceptance re-checks conflicts registered after the invitation was sent.
        conflict = _current_conflict_for_manuscript(
            conn, invitation["manuscript_id"], user["id"]
        )
        if conflict is not None:
            raise ApiError(
                409,
                "conflict_blocked",
                f"conflict of interest on record (reason: {conflict['reason']})",
            )
        conn.execute(
            "UPDATE invitations SET status = 'accepted', responded_at = ? WHERE id = ?",
            (utcnow(), invitation_id),
        )
        return {"invitation_id": invitation_id, "status": "accepted"}


def submit_review(
    db_path: str,
    user: sqlite3.Row,
    invitation_id: int,
    content: str,
    score: int,
) -> dict:
    with database.write_tx(db_path) as conn:
        invitation = _load_invitation_for_reviewer(conn, invitation_id, user)

        existing = conn.execute(
            "SELECT id FROM reviews WHERE invitation_id = ?", (invitation_id,)
        ).fetchone()
        if existing is not None:
            # Reviews are immutable: never replaceable.
            raise ApiError(409, "review_exists", "a review already exists for this invitation")

        # Conflict appearing between acceptance and submission freezes the
        # invitation; check it before status so callers get the precise reason.
        conflict = _current_conflict_for_manuscript(
            conn, invitation["manuscript_id"], user["id"]
        )
        if conflict is not None:
            raise ApiError(
                409,
                "conflict_blocked",
                f"conflict of interest on record (reason: {conflict['reason']})",
            )

        if invitation["status"] != "accepted":
            raise ApiError(
                409,
                "invalid_state",
                f"cannot submit a review for an invitation in status '{invitation['status']}'",
            )

        now = utcnow()
        cur = conn.execute(
            "INSERT INTO reviews (invitation_id, revision_id, reviewer_id, content, score, submitted_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                invitation_id,
                invitation["revision_id"],
                user["id"],
                content,
                score,
                now,
            ),
        )
        conn.execute(
            "UPDATE invitations SET status = 'reviewed' WHERE id = ?", (invitation_id,)
        )
        return {
            "review_id": cur.lastrowid,
            "revision_id": invitation["revision_id"],
            "score": score,
            "submitted_at": now,
        }


# ---------------------------------------------------------------------------
# Editor services
# ---------------------------------------------------------------------------

def list_accounts(db_path: str) -> list[dict]:
    with database.read_tx(db_path) as conn:
        rows = conn.execute(
            "SELECT id, username, display_name, role FROM users ORDER BY role, id"
        ).fetchall()
        return [dict(r) for r in rows]


def list_manuscripts_overview(db_path: str) -> list[dict]:
    with database.read_tx(db_path) as conn:
        users = {u["id"]: u for u in conn.execute("SELECT * FROM users").fetchall()}
        rows = conn.execute("SELECT * FROM manuscripts ORDER BY id").fetchall()
        out = []
        for m in rows:
            author = users[m["author_id"]]
            revision_count = conn.execute(
                "SELECT COUNT(*) AS c FROM revisions WHERE manuscript_id = ?", (m["id"],)
            ).fetchone()["c"]
            out.append(
                {
                    "manuscript_id": m["id"],
                    "title": m["title"],
                    "author": {
                        "username": author["username"],
                        "display_name": author["display_name"],
                    },
                    "revision_count": revision_count,
                    "created_at": m["created_at"],
                }
            )
        return out


def editor_get_manuscript(db_path: str, manuscript_id: int) -> dict:
    with database.read_tx(db_path) as conn:
        manuscript = require_row(conn, "manuscripts", manuscript_id, "manuscript")
        return manuscript_detail(conn, manuscript, include_author=True)


def create_invitation(
    db_path: str, revision_id: int, reviewer_id: int
) -> dict:
    with database.write_tx(db_path) as conn:
        revision = require_row(conn, "revisions", revision_id, "revision")
        reviewer = require_row(conn, "users", reviewer_id, "user")
        if reviewer["role"] != "reviewer":
            raise ApiError(400, "not_reviewer", "the target user is not a reviewer")

        manuscript = require_row(conn, "manuscripts", revision["manuscript_id"], "manuscript")

        # Existing conflict blocks the invitation outright.
        conflict = _has_conflict(conn, manuscript["author_id"], reviewer_id)
        if conflict is not None:
            raise ApiError(
                409,
                "conflict_exists",
                f"conflict of interest already registered (reason: {conflict['reason']})",
            )

        dup = conn.execute(
            "SELECT id, status FROM invitations WHERE revision_id = ? AND reviewer_id = ?",
            (revision_id, reviewer_id),
        ).fetchone()
        if dup is not None:
            raise ApiError(
                409,
                "invitation_exists",
                f"reviewer already invited to this revision (status: {dup['status']})",
            )

        # Allocate the stable per-manuscript anonymous number on first contact.
        anon_row = conn.execute(
            "SELECT anon_number FROM anonymous_numbers"
            " WHERE manuscript_id = ? AND reviewer_id = ?",
            (manuscript["id"], reviewer_id),
        ).fetchone()
        if anon_row is None:
            max_no = conn.execute(
                "SELECT COALESCE(MAX(anon_number), 0) AS n FROM anonymous_numbers"
                " WHERE manuscript_id = ?",
                (manuscript["id"],),
            ).fetchone()["n"]
            anon_number = max_no + 1
            conn.execute(
                "INSERT INTO anonymous_numbers (manuscript_id, reviewer_id, anon_number)"
                " VALUES (?, ?, ?)",
                (manuscript["id"], reviewer_id, anon_number),
            )
        else:
            anon_number = anon_row["anon_number"]

        now = utcnow()
        cur = conn.execute(
            "INSERT INTO invitations (manuscript_id, revision_id, reviewer_id, status, invited_at)"
            " VALUES (?, ?, ?, 'invited', ?)",
            (manuscript["id"], revision_id, reviewer_id, now),
        )
        return {
            "invitation_id": cur.lastrowid,
            "manuscript_id": manuscript["id"],
            "revision_id": revision_id,
            "reviewer_id": reviewer_id,
            "anon_number": anon_number,
            "status": "invited",
            "invited_at": now,
        }


def list_conflicts(db_path: str) -> list[dict]:
    with database.read_tx(db_path) as conn:
        rows = conn.execute(
            "SELECT c.*, au.username AS author_username, ru.username AS reviewer_username"
            " FROM conflicts c"
            " JOIN users au ON au.id = c.author_id"
            " JOIN users ru ON ru.id = c.reviewer_id"
            " ORDER BY c.id"
        ).fetchall()
        return [
            {
                "conflict_id": r["id"],
                "author": {"username": r["author_username"]},
                "reviewer": {"username": r["reviewer_username"]},
                "reason": r["reason"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]


def _block_open_invitations(
    conn: sqlite3.Connection, manuscript_id: int, reviewer_id: int, reason: str
) -> int:
    """Block invitations that have not yet produced a review.

    Already-submitted reviews stay attached to their original reviewer: they
    remain editor-visible but become ineligible for decisions.
    """
    cur = conn.execute(
        "UPDATE invitations SET status = 'blocked', blocked_reason = ?,"
        " responded_at = COALESCE(responded_at, ?)"
        " WHERE manuscript_id = ? AND reviewer_id = ?"
        " AND status IN ('invited', 'accepted')",
        (reason, utcnow(), manuscript_id, reviewer_id),
    )
    return cur.rowcount


def _withdraw_decisions_for_reviewer(
    conn: sqlite3.Connection, manuscript_id: int, reviewer_id: int, reason: str
) -> list[int]:
    """Withdraw any active published version whose frozen content used this reviewer."""
    decision_rows = conn.execute(
        "SELECT DISTINCT d.id FROM decisions d"
        " JOIN decision_reviews dr ON dr.decision_id = d.id"
        " JOIN reviews rv ON rv.id = dr.review_id"
        " WHERE d.manuscript_id = ? AND d.status = 'published' AND rv.reviewer_id = ?",
        (manuscript_id, reviewer_id),
    ).fetchall()
    withdrawn = []
    for d in decision_rows:
        conn.execute(
            "UPDATE decisions SET status = 'withdrawn', withdrawn_at = ?,"
            " withdrawal_reason = ? WHERE id = ?",
            (utcnow(), reason, d["id"]),
        )
        withdrawn.append(d["id"])
    return withdrawn


def register_conflict(
    db_path: str, author_id: int, reviewer_id: int, reason: str
) -> dict:
    with database.write_tx(db_path) as conn:
        author = require_row(conn, "users", author_id, "user")
        if author["role"] != "author":
            raise ApiError(400, "not_author", "the first user id must belong to an author")
        reviewer = require_row(conn, "users", reviewer_id, "user")
        if reviewer["role"] != "reviewer":
            raise ApiError(400, "not_reviewer", "the second user id must belong to a reviewer")
        if _has_conflict(conn, author_id, reviewer_id) is not None:
            raise ApiError(409, "conflict_exists", "this conflict is already registered")

        now = utcnow()
        cur = conn.execute(
            "INSERT INTO conflicts (author_id, reviewer_id, reason, created_at)"
            " VALUES (?, ?, ?, ?)",
            (author_id, reviewer_id, reason, now),
        )
        conflict_id = cur.lastrowid

        blocked = 0
        withdrawn: list[int] = []
        manuscripts = conn.execute(
            "SELECT id FROM manuscripts WHERE author_id = ?", (author_id,)
        ).fetchall()
        for m in manuscripts:
            blocked += _block_open_invitations(conn, m["id"], reviewer_id, reason)
            withdrawn += _withdraw_decisions_for_reviewer(conn, m["id"], reviewer_id, reason)

        return {
            "conflict_id": conflict_id,
            "author_id": author_id,
            "reviewer_id": reviewer_id,
            "blocked_invitations": blocked,
            "withdrawn_decision_ids": withdrawn,
            "created_at": now,
        }


def publish_decision(
    db_path: str,
    revision_id: int,
    review_ids: list[int],
    conclusion: str,
) -> dict:
    _ensure_unique_review_ids(review_ids)
    if len(review_ids) < 2:
        raise ApiError(400, "too_few_reviews", "at least two reviews must be selected")

    with database.write_tx(db_path) as conn:
        revision = require_row(conn, "revisions", revision_id, "revision")

        active = conn.execute(
            "SELECT id FROM decisions WHERE revision_id = ? AND status = 'published'",
            (revision_id,),
        ).fetchone()
        if active is not None:
            raise ApiError(
                409, "decision_exists", "an active decision already exists for this revision"
            )

        manuscript = require_row(conn, "manuscripts", revision["manuscript_id"], "manuscript")
        selected: list[sqlite3.Row] = []
        for rid in review_ids:
            review = require_row(conn, "reviews", rid, "review")
            # Reviews belong to one specific revision; never mix revisions.
            if review["revision_id"] != revision_id:
                raise ApiError(
                    400,
                    "revision_mismatch",
                    f"review {rid} was not submitted against revision {revision_id}",
                )
            # Current conflict makes the review ineligible even if it was valid at submission.
            conflict = _has_conflict(conn, manuscript["author_id"], review["reviewer_id"])
            if conflict is not None:
                raise ApiError(
                    409,
                    "conflict_blocks_review",
                    f"review {rid} is excluded by a current conflict (reason: {conflict['reason']})",
                )
            selected.append(review)

        now = utcnow()
        cur = conn.execute(
            "INSERT INTO decisions (manuscript_id, revision_id, conclusion, status, published_at)"
            " VALUES (?, ?, ?, 'published', ?)",
            (manuscript["id"], revision_id, conclusion, now),
        )
        decision_id = cur.lastrowid
        for review in selected:
            anon_row = conn.execute(
                "SELECT anon_number FROM anonymous_numbers"
                " WHERE manuscript_id = ? AND reviewer_id = ?",
                (manuscript["id"], review["reviewer_id"]),
            ).fetchone()
            conn.execute(
                "INSERT INTO decision_reviews (decision_id, review_id, anon_number, content, score)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    decision_id,
                    review["id"],
                    anon_row["anon_number"],
                    review["content"],
                    review["score"],
                ),
            )
        decision = require_row(conn, "decisions", decision_id, "decision")
        return editor_decision_payload(conn, decision)


def editor_list_decisions(db_path: str) -> list[dict]:
    with database.read_tx(db_path) as conn:
        rows = conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
        return [editor_decision_payload(conn, d) for d in rows]


def editor_get_decision(db_path: str, decision_id: int) -> dict:
    with database.read_tx(db_path) as conn:
        decision = require_row(conn, "decisions", decision_id, "decision")
        return editor_decision_payload(conn, decision)
