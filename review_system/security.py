"""API-key authentication and response serialization.

There is deliberately no generic user-management surface: accounts are
prepared locally (see ``db.SEED_USERS``) and every request authenticates with
a static ``X-API-Key`` header.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from fastapi import Depends, Header

from . import config
from . import db as database
from .errors import ApiError


def current_user(x_api_key: str | None = Header(default=None)) -> sqlite3.Row:
    if not x_api_key:
        raise ApiError(401, "unauthorized", "missing X-API-Key header")
    with database.read_tx(config.DB_PATH) as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE api_key = ?", (x_api_key,)
        ).fetchone()
    if user is None:
        raise ApiError(401, "unauthorized", "invalid API key")
    return user


def require_role(*roles: str) -> Callable[..., sqlite3.Row]:
    def checker(user: sqlite3.Row = Depends(current_user)) -> sqlite3.Row:
        if user["role"] not in roles:
            raise ApiError(
                403, "forbidden", f"role '{user['role']}' may not call this endpoint"
            )
        return user

    return checker


# ---------------------------------------------------------------------------
# Serialization. Public payloads only ever expose the fields each role may see.
# ---------------------------------------------------------------------------

def revision_brief(row: sqlite3.Row) -> dict:
    return {
        "revision_id": row["id"],
        "revision_no": row["revision_no"],
        "created_at": row["created_at"],
    }


def manuscript_brief(row: sqlite3.Row, *, include_author: bool, users: dict[int, sqlite3.Row]) -> dict:
    payload = {
        "manuscript_id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
    }
    if include_author:
        author = users[row["author_id"]]
        payload["author"] = {"username": author["username"], "display_name": author["display_name"]}
    return payload


def manuscript_detail(
    conn: sqlite3.Connection,
    manuscript: sqlite3.Row,
    *,
    include_author: bool,
) -> dict:
    users = {u["id"]: u for u in conn.execute("SELECT * FROM users").fetchall()}
    revisions = conn.execute(
        "SELECT * FROM revisions WHERE manuscript_id = ? ORDER BY revision_no",
        (manuscript["id"],),
    ).fetchall()
    payload = manuscript_brief(manuscript, include_author=include_author, users=users)
    payload["revisions"] = [
        {**revision_brief(r), "content": r["content"]} for r in revisions
    ]
    if include_author:
        invitations = conn.execute(
            "SELECT i.*, u.username AS reviewer_username, u.display_name AS reviewer_name,"
            " an.anon_number, rv.id AS review_id, rv.content AS review_content,"
            " rv.score AS review_score, rv.submitted_at AS review_submitted_at"
            " FROM invitations i"
            " JOIN users u ON u.id = i.reviewer_id"
            " JOIN anonymous_numbers an ON an.manuscript_id = i.manuscript_id"
            "  AND an.reviewer_id = i.reviewer_id"
            " LEFT JOIN reviews rv ON rv.invitation_id = i.id"
            " WHERE i.manuscript_id = ? ORDER BY i.id",
            (manuscript["id"],),
        ).fetchall()
        payload["invitations"] = [_editor_invitation(r) for r in invitations]
        decisions = conn.execute(
            "SELECT * FROM decisions WHERE manuscript_id = ? ORDER BY id",
            (manuscript["id"],),
        ).fetchall()
        payload["decisions"] = [
            editor_decision_payload(conn, d) for d in decisions
        ]
    return payload


def _editor_invitation(row: sqlite3.Row) -> dict:
    out = {
        "invitation_id": row["id"],
        "revision_id": row["revision_id"],
        "reviewer": {
            "username": row["reviewer_username"],
            "display_name": row["reviewer_name"],
        },
        "anon_number": row["anon_number"],
        "status": row["status"],
        "invited_at": row["invited_at"],
        "responded_at": row["responded_at"],
        "blocked_reason": row["blocked_reason"],
    }
    if row["review_id"] is not None:
        out["review"] = {
            "review_id": row["review_id"],
            "content": row["review_content"],
            "score": row["review_score"],
            "submitted_at": row["review_submitted_at"],
        }
    return out


def reviewer_invitation_payload(row: sqlite3.Row, review: sqlite3.Row | None) -> dict:
    """Reviewer view: no invitation id, no author identity, own review only."""
    out = {
        "manuscript_id": row["manuscript_id"],
        "manuscript_title": row["title"],
        "revision_id": row["revision_id"],
        "revision_no": row["revision_no"],
        "revision_content": row["content"],
        "anon_number": row["anon_number"],
        "status": row["status"],
        "invited_at": row["invited_at"],
        "responded_at": row["responded_at"],
        "blocked_reason": row["blocked_reason"],
    }
    if review is not None:
        out["my_review"] = {
            "content": review["content"],
            "score": review["score"],
            "submitted_at": review["submitted_at"],
        }
    return out


def _snapshots(conn: sqlite3.Connection, decision_id: int) -> list[dict]:
    return [
        {
            "anon_number": r["anon_number"],
            "score": r["score"],
            "content": r["content"],
        }
        for r in conn.execute(
            "SELECT * FROM decision_reviews WHERE decision_id = ? ORDER BY anon_number",
            (decision_id,),
        ).fetchall()
    ]


def author_decision_payload(row: sqlite3.Row, snapshots: list[dict]) -> dict:
    """Fixed published version: only stable anonymous numbers, no account/invitation ids."""
    return {
        "decision_id": row["id"],
        "manuscript_id": row["manuscript_id"],
        "revision_id": row["revision_id"],
        "conclusion": row["conclusion"],
        "published_at": row["published_at"],
        "reviews": snapshots,
    }


def author_withdrawn_payload(row: sqlite3.Row) -> dict:
    """Withdrawn: the body is gone from author-facing links."""
    return {
        "decision_id": row["id"],
        "manuscript_id": row["manuscript_id"],
        "revision_id": row["revision_id"],
        "status": "withdrawn",
        "withdrawn_at": row["withdrawn_at"],
        "note": "this published version has been withdrawn because of a conflict of interest",
    }


def editor_decision_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """Editor view: original published content plus withdrawal metadata."""
    payload = {
        "decision_id": row["id"],
        "manuscript_id": row["manuscript_id"],
        "revision_id": row["revision_id"],
        "status": row["status"],
        "conclusion": row["conclusion"],
        "published_at": row["published_at"],
        "withdrawn_at": row["withdrawn_at"],
        "withdrawal_reason": row["withdrawal_reason"],
        "reviews": _snapshots(conn, row["id"]),
    }
    return payload
