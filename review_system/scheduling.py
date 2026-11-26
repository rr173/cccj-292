"""Conference talk scheduling and linked group rescheduling.

Lifecycle overview
-------------------
* The editor submits a *group* of arrangements (one row per manuscript).
  Every proposed slot immediately becomes a **reservation** that other groups
  cannot overlap, while existing confirmed sessions stay valid and occupied.
* Each author answers their own manuscript's item separately.  Only when every
  item in a still-open group has been accepted before the deadline does the
  whole group finalise atomically: old sessions are superseded and the new
  sessions become confirmed.
* Decline, deadline expiry or conflict-withdrawal cancels the whole group,
  releases its reservations and leaves prior confirmed sessions untouched.

All mutating entry points run inside the same serialised
``BEGIN IMMEDIATE`` transaction discipline as the rest of the service, so the
last consent cannot race a conflict withdrawal into leaving a session based
on a withdrawn decision.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import config, db as database
from .db import require_row, utcnow
from .errors import ApiError

# Group / item / session status strings are centralised so payload aliases and
# the conflict hook cannot drift apart.
GROUP_PENDING = "pending"
GROUP_CONFIRMED = "confirmed"
GROUP_DECLINED = "declined"
GROUP_EXPIRED = "expired"
GROUP_CANCELLED = "cancelled"

RESP_WAITING = "waiting"
RESP_ACCEPTED = "accepted"
RESP_DECLINED = "declined"

SESSION_CONFIRMED = "confirmed"
SESSION_SUPERSEDED = "superseded"
SESSION_VOIDED = "voided"


# ---------------------------------------------------------------------------
# Time helpers — the API speaks UTC ISO-8601.
# ---------------------------------------------------------------------------

def parse_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        # Naive timestamps are interpreted as UTC rather than rejected: the
        # house convention is "all times are UTC".
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_utc(value: Any) -> str:
    return parse_utc(value).isoformat(timespec="seconds")


def _overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """Half-open interval overlap; back-to-back slots do not overlap."""
    return start_a < end_b and start_b < end_a


# ---------------------------------------------------------------------------
# Expiry sweep
# ---------------------------------------------------------------------------

def expire_due_groups(db_path: str) -> list[int]:
    """Persistently close every pending group whose deadline has passed."""
    now = utcnow()
    with database.write_tx(db_path) as conn:
        return _expire_due_groups(conn, now)


def _expire_due_groups(conn: sqlite3.Connection, now_iso: str) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM schedule_groups"
        " WHERE status = ? AND confirmation_deadline <= ?",
        (GROUP_PENDING, now_iso),
    ).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        conn.execute(
            "UPDATE schedule_groups SET status = ?, end_reason = ?,"
            " ended_at = ? WHERE status = ? AND confirmation_deadline <= ?",
            (GROUP_EXPIRED, "deadline_expired", now_iso, GROUP_PENDING, now_iso),
        )
    return ids


# ---------------------------------------------------------------------------
# Room / payload helpers
# ---------------------------------------------------------------------------

def _room_fields(stored_room: Any) -> dict[str, Any]:
    """Payload room fields.

    ``room`` echoes the exact token the editor supplied; ``room_code`` is the
    canonical local-configuration identity used for matching/overlap checks.
    """
    resolved = config.resolve_room(stored_room)
    if resolved is None:
        return {
            "room": stored_room,
            "room_code": None,
            "room_id": None,
            "room_name": str(stored_room),
        }
    return {
        "room": stored_room,
        "room_code": resolved["code"],
        "room_id": resolved["id"],
        "room_name": resolved["name"],
    }


def _author_of_manuscript(conn: sqlite3.Connection, manuscript: sqlite3.Row) -> sqlite3.Row:
    return require_row(conn, "users", manuscript["author_id"], "user")


def _author_brief(author: sqlite3.Row) -> dict[str, str]:
    return {"username": author["username"], "display_name": author["display_name"]}


def _item_editor_payload(conn: sqlite3.Connection, item: sqlite3.Row) -> dict[str, Any]:
    manuscript = require_row(conn, "manuscripts", item["manuscript_id"], "manuscript")
    author = _author_of_manuscript(conn, manuscript)
    return {
        "item_id": item["id"],
        "id": item["id"],
        "manuscript_id": item["manuscript_id"],
        "manuscript_title": manuscript["title"],
        "author": _author_brief(author),
        "decision_id": item["decision_id"],
        **_room_fields(item["room"]),
        "starts_at": item["starts_at"],
        "ends_at": item["ends_at"],
        "response": item["response"],
        "author_response": item["response"],
        "responded_at": item["responded_at"],
    }


def group_payload(conn: sqlite3.Connection, group: sqlite3.Row) -> dict[str, Any]:
    items = conn.execute(
        "SELECT * FROM schedule_items WHERE group_id = ? ORDER BY id", (group["id"],)
    ).fetchall()
    item_views = [_item_editor_payload(conn, it) for it in items]
    accepted = sum(1 for it in items if it["response"] == RESP_ACCEPTED)
    declined = sum(1 for it in items if it["response"] == RESP_DECLINED)
    waiting = len(items) - accepted - declined
    status = group["status"]
    return {
        "group_id": group["id"],
        "id": group["id"],
        "status": status,
        # Vocabulary aliases for clients that phrase the terminal state differently.
        "state": status,
        "confirmation_deadline": group["confirmation_deadline"],
        "deadline": group["confirmation_deadline"],
        "created_at": group["created_at"],
        "ended_at": group["ended_at"],
        "end_reason": group["end_reason"],
        "reason": group["end_reason"],
        "progress": {
            "total": len(items),
            "accepted": accepted,
            "declined": declined,
            "waiting": waiting,
            "all_accepted": len(items) > 0 and accepted == len(items),
        },
        "items": item_views,
    }


def _get_group_row(conn: sqlite3.Connection, group_id: int) -> sqlite3.Row:
    return require_row(conn, "schedule_groups", group_id, "schedule group")


def session_payload(conn: sqlite3.Connection, session: sqlite3.Row) -> dict[str, Any]:
    manuscript = require_row(conn, "manuscripts", session["manuscript_id"], "manuscript")
    author = _author_of_manuscript(conn, manuscript)
    return {
        "session_id": session["id"],
        "id": session["id"],
        "manuscript_id": session["manuscript_id"],
        "manuscript_title": manuscript["title"],
        "author": _author_brief(author),
        "decision_id": session["decision_id"],
        "group_id": session["group_id"],
        **_room_fields(session["room"]),
        "starts_at": session["starts_at"],
        "ends_at": session["ends_at"],
        "status": session["status"],
        "confirmed_at": session["confirmed_at"],
        "ended_at": session["ended_at"],
        "end_reason": session["end_reason"],
        "replaced_by_group_id": session["replaced_by_group_id"],
    }


# ---------------------------------------------------------------------------
# Occupancy / overlap checks
# ---------------------------------------------------------------------------

def _occupancy_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every slot currently occupying a room/author calendar.

    Occupying slots are confirmed sessions plus the items of pending groups
    (the reservations created the moment an editor submits a group).
    """
    candidates: list[dict[str, Any]] = []
    confirmed = conn.execute(
        "SELECT s.id AS ref_id, s.manuscript_id, s.room, s.starts_at, s.ends_at,"
        " m.author_id, 'session' AS kind"
        " FROM sessions s JOIN manuscripts m ON m.id = s.manuscript_id"
        " WHERE s.status = ?",
        (SESSION_CONFIRMED,),
    ).fetchall()
    reserved = conn.execute(
        "SELECT i.id AS ref_id, i.manuscript_id, i.room, i.starts_at, i.ends_at,"
        " m.author_id, i.group_id, 'reservation' AS kind"
        " FROM schedule_items i"
        " JOIN schedule_groups g ON g.id = i.group_id"
        " JOIN manuscripts m ON m.id = i.manuscript_id"
        " WHERE g.status = ?",
        (GROUP_PENDING,),
    ).fetchall()
    for row in (*confirmed, *reserved):
        resolved_room = config.resolve_room(row["room"])
        candidates.append(
            {
                "ref_id": row["ref_id"],
                "kind": row["kind"],
                "group_id": row["group_id"] if "group_id" in row.keys() else None,
                "manuscript_id": row["manuscript_id"],
                "author_id": row["author_id"],
                "room": resolved_room["code"] if resolved_room else str(row["room"]),
                "start": parse_utc(row["starts_at"]),
                "end": parse_utc(row["ends_at"]),
            }
        )
    return candidates


def _clashing_candidate(
    room_code: str,
    author_id: int,
    start: datetime,
    end: datetime,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for cand in candidates:
        # Different room and different author calendars -> no possible clash.
        if cand["room"] != room_code and cand["author_id"] != author_id:
            continue
        if _overlaps(start, end, cand["start"], cand["end"]):
            return cand
    return None


def _conflict_error(cand: dict[str, Any]) -> ApiError:
    if cand["kind"] == "session":
        target = f"confirmed session {cand['ref_id']}"
    else:
        target = f"reserved slot from pending group {cand['group_id']} (item {cand['ref_id']})"
    return ApiError(
        409,
        "schedule_conflict",
        f"time slot overlaps {target}; room and author schedules cannot overlap"
        " (back-to-back slots are allowed)",
    )


# ---------------------------------------------------------------------------
# Editor: submit a group of arrangements
# ---------------------------------------------------------------------------

def create_schedule_group(
    db_path: str,
    items: list[dict[str, Any]],
    confirmation_deadline: Any,
) -> dict[str, Any]:
    deadline = parse_utc(confirmation_deadline)

    with database.write_tx(db_path) as conn:
        now = utcnow()
        # Reservations of groups whose deadline already passed are released
        # before we validate new occupancy.
        _expire_due_groups(conn, now)

        manuscript_ids = [int(it["manuscript_id"]) for it in items]
        if len(set(manuscript_ids)) != len(manuscript_ids):
            raise ApiError(
                400,
                "duplicate_manuscript",
                "each manuscript may appear at most once in a schedule group",
            )

        planned: list[dict[str, Any]] = []
        old_sessions: dict[int, sqlite3.Row] = {}

        for raw in items:
            manuscript_id = int(raw["manuscript_id"])
            manuscript = require_row(conn, "manuscripts", manuscript_id, "manuscript")

            start = parse_utc(raw["starts_at"])
            end = parse_utc(raw["ends_at"])
            if end <= start:
                raise ApiError(
                    400,
                    "invalid_time_range",
                    f"manuscript {manuscript_id}: ends_at must be strictly after starts_at",
                )

            room = config.resolve_room(raw.get("room"))
            if room is None:
                valid = ", ".join(r["code"] for r in config.load_rooms())
                raise ApiError(
                    400,
                    "unknown_room",
                    f"room {raw.get('room')!r} is not in local room configuration"
                    f" (configured: {valid})",
                )

            current_session = conn.execute(
                "SELECT * FROM sessions WHERE manuscript_id = ? AND status = ?",
                (manuscript_id, SESSION_CONFIRMED),
            ).fetchone()

            supplied_decision = raw.get("decision_id")
            if current_session is not None:
                # An existing arrangement keeps its decision: only room/time move.
                locked_decision_id = current_session["decision_id"]
                if supplied_decision is not None and int(supplied_decision) != locked_decision_id:
                    raise ApiError(
                        409,
                        "decision_locked",
                        "manuscript already has a confirmed arrangement; the rescheduling"
                        f" must reuse its decision {locked_decision_id} (only room and time change)",
                    )
                decision_id = locked_decision_id
                old_sessions[manuscript_id] = current_session
            else:
                if supplied_decision is None:
                    raise ApiError(
                        400,
                        "decision_required",
                        f"manuscript {manuscript_id} has no confirmed arrangement yet,"
                        " so one of its published decisions must be designated",
                    )
                decision = require_row(
                    conn, "decisions", int(supplied_decision), "decision"
                )
                if decision["manuscript_id"] != manuscript_id:
                    raise ApiError(
                        400,
                        "decision_mismatch",
                        f"decision {decision['id']} does not belong to manuscript {manuscript_id}",
                    )
                if decision["status"] != "published":
                    raise ApiError(
                        409,
                        "decision_not_active",
                        f"decision {decision['id']} is {decision['status']};"
                        " an un-withdrawn published decision is required",
                    )
                decision_id = decision["id"]

            # A manuscript may belong to at most one still-pending group.
            pending_row = conn.execute(
                "SELECT g.id FROM schedule_items i"
                " JOIN schedule_groups g ON g.id = i.group_id"
                " WHERE i.manuscript_id = ? AND g.status = ?",
                (manuscript_id, GROUP_PENDING),
            ).fetchone()
            if pending_row is not None:
                raise ApiError(
                    409,
                    "manuscript_pending",
                    f"manuscript {manuscript_id} is already waiting for author confirmation"
                    f" in schedule group {pending_row['id']}",
                )

            author = _author_of_manuscript(conn, manuscript)
            planned.append(
                {
                    "manuscript_id": manuscript_id,
                    "author_id": author["id"],
                    "decision_id": decision_id,
                    "room_raw": raw["room"],
                    "room_code": room["code"],
                    "start": start,
                    "end": end,
                }
            )

        # The deadline must precede every involved *new* slot start.
        for slot in planned:
            if deadline >= slot["start"]:
                raise ApiError(
                    400,
                    "deadline_too_late",
                    "confirmation deadline must be strictly before every proposed slot start",
                )
        # ...and every involved *old* (currently confirmed) slot start.
        for session in old_sessions.values():
            old_start = parse_utc(session["starts_at"])
            if deadline >= old_start:
                raise ApiError(
                    400,
                    "deadline_too_late",
                    "confirmation deadline must be strictly before every current slot start"
                    " that this group would replace",
                )

        # New slots inside one group must not conflict with each other.
        for i in range(len(planned)):
            for j in range(i + 1, len(planned)):
                a, b = planned[i], planned[j]
                same_room = a["room_code"] == b["room_code"]
                same_author = a["author_id"] == b["author_id"]
                if (same_room or same_author) and _overlaps(
                    a["start"], a["end"], b["start"], b["end"]
                ):
                    dimension = "room" if same_room else "author"
                    raise ApiError(
                        409,
                        "schedule_conflict",
                        f"proposed arrangements for manuscripts {a['manuscript_id']} and"
                        f" {b['manuscript_id']} overlap on the same {dimension}",
                    )

        # Occupancy from confirmed sessions and *other* pending groups.
        # Confirmed sessions belonging to manuscripts in this group are the
        # old slots this group will replace, so its new slots are allowed to
        # overlap them (e.g. two talks swapping consecutive slots).
        replaced_manuscripts = set(manuscript_ids)
        candidates = [
            c
            for c in _occupancy_candidates(conn)
            if not (c["kind"] == "session" and c["manuscript_id"] in replaced_manuscripts)
        ]
        for slot in planned:
            clash = _clashing_candidate(
                slot["room_code"], slot["author_id"], slot["start"], slot["end"], candidates
            )
            if clash is not None:
                raise _conflict_error(clash)

        cur = conn.execute(
            "INSERT INTO schedule_groups (confirmation_deadline, status, created_at)"
            " VALUES (?, ?, ?)",
            (iso_utc(deadline), GROUP_PENDING, now),
        )
        group_id = cur.lastrowid
        for slot in planned:
            conn.execute(
                "INSERT INTO schedule_items"
                " (group_id, manuscript_id, decision_id, room, starts_at, ends_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    group_id,
                    slot["manuscript_id"],
                    slot["decision_id"],
                    slot["room_raw"],
                    iso_utc(slot["start"]),
                    iso_utc(slot["end"]),
                ),
            )

        # A deadline already in the past closes the group immediately; the
        # committed row is then terminal and never held a live reservation.
        _expire_due_groups(conn, now)
        group = _get_group_row(conn, group_id)
        return group_payload(conn, group)


# ---------------------------------------------------------------------------
# Author response + group finalisation
# ---------------------------------------------------------------------------

_CLOSED_CODE = {
    GROUP_EXPIRED: ("confirmation_expired", "the confirmation deadline has passed"),
    GROUP_DECLINED: ("group_declined", "the group was rejected by an author"),
    GROUP_CANCELLED: ("group_cancelled", "the group was cancelled"),
    GROUP_CONFIRMED: ("group_confirmed", "the group has already been confirmed"),
}


def resolve_own_group_item(
    db_path: str, user: sqlite3.Row, group_id: int, item_id: int | None = None
) -> int:
    """Resolve ``POST /author/schedule/groups/{id}/respond`` to one own item.

    Authors normally have one manuscript per group; if they have several they
    must disambiguate with ``item_id`` because every manuscript is confirmed
    separately.  Non-ownership always surfaces as 404.
    """
    with database.read_tx(db_path) as conn:
        _get_group_row(conn, group_id)
        rows = conn.execute(
            "SELECT i.* FROM schedule_items i"
            " JOIN manuscripts m ON m.id = i.manuscript_id"
            " WHERE i.group_id = ? AND m.author_id = ? ORDER BY i.id",
            (group_id, user["id"]),
        ).fetchall()
        if not rows:
            raise ApiError(404, "not_found", "schedule group not found")
        if item_id is not None:
            if not any(r["id"] == item_id for r in rows):
                raise ApiError(404, "not_found", "schedule item not found")
            return item_id
        if len(rows) == 1:
            return rows[0]["id"]
        waiting = [r["id"] for r in rows if r["response"] == RESP_WAITING]
        if len(waiting) == 1:
            return waiting[0]
        raise ApiError(
            400,
            "item_id_required",
            "your account has multiple arrangements in this group;"
            " pass the specific item_id to respond to",
        )


def respond_to_item(
    db_path: str, user: sqlite3.Row, item_id: int, accept: bool
) -> dict[str, Any]:
    with database.write_tx(db_path) as conn:
        now_dt = datetime.now(timezone.utc)
        now = iso_utc(now_dt)
        _expire_due_groups(conn, now)

        item = require_row(conn, "schedule_items", item_id, "schedule item")
        group = _get_group_row(conn, item["group_id"])
        manuscript = require_row(conn, "manuscripts", item["manuscript_id"], "manuscript")

        # Never reveal another author's arrangement/item id.
        if manuscript["author_id"] != user["id"]:
            raise ApiError(404, "not_found", "schedule item not found")

        if group["status"] != GROUP_PENDING:
            code, detail = _CLOSED_CODE.get(
                group["status"], ("group_closed", "this confirmation group is closed")
            )
            raise ApiError(409, code, detail)

        if parse_utc(group["confirmation_deadline"]) <= now_dt:
            _expire_due_groups(conn, now)
            raise ApiError(
                409, "confirmation_expired", "the confirmation deadline has passed"
            )

        if item["response"] != RESP_WAITING:
            raise ApiError(
                409,
                "already_responded",
                f"this arrangement was already answered as '{item['response']}'",
            )

        if not accept:
            conn.execute(
                "UPDATE schedule_items SET response = ?, responded_at = ? WHERE id = ?",
                (RESP_DECLINED, now, item_id),
            )
            conn.execute(
                "UPDATE schedule_groups SET status = ?, end_reason = ?,"
                " ended_at = ? WHERE id = ?",
                (GROUP_DECLINED, "author_declined", now, group["id"]),
            )
        else:
            conn.execute(
                "UPDATE schedule_items SET response = ?, responded_at = ? WHERE id = ?",
                (RESP_ACCEPTED, now, item_id),
            )
            items = conn.execute(
                "SELECT response FROM schedule_items WHERE group_id = ?", (group["id"],)
            ).fetchall()
            if all(row["response"] == RESP_ACCEPTED for row in items):
                _finalize_group(conn, group["id"], now)

        group = _get_group_row(conn, group["id"])
        item = require_row(conn, "schedule_items", item_id, "schedule item")
        return {
            "item_id": item_id,
            "id": item_id,
            "response": item["response"],
            # Convenience aliases for clients that expect a status/boolean.
            "status": item["response"],
            "accepted": item["response"] == RESP_ACCEPTED,
            "responded_at": item["responded_at"],
            "group": group_payload(conn, group),
        }


def _finalize_group(conn: sqlite3.Connection, group_id: int, now: str) -> None:
    group = _get_group_row(conn, group_id)
    items = conn.execute(
        "SELECT * FROM schedule_items WHERE group_id = ? ORDER BY id", (group_id,)
    ).fetchall()

    # Defensive invariant: a finalised session must be based on a live
    # published decision.  Under the serialised writer this branch is
    # unreachable (withdrawal would have cancelled the pending group first).
    for item in items:
        decision = require_row(conn, "decisions", item["decision_id"], "decision")
        if decision["status"] != "published":
            conn.execute(
                "UPDATE schedule_groups SET status = ?, end_reason = ?, ended_at = ?"
                " WHERE id = ?",
                (GROUP_CANCELLED, "decision_withdrawn", now, group_id),
            )
            raise ApiError(
                409,
                "group_cancelled",
                "a decision backing this arrangement was withdrawn",
            )

    for item in items:
        old = conn.execute(
            "SELECT * FROM sessions WHERE manuscript_id = ? AND status = ?",
            (item["manuscript_id"], SESSION_CONFIRMED),
        ).fetchone()
        if old is not None:
            conn.execute(
                "UPDATE sessions SET status = ?, ended_at = ?, end_reason = ?,"
                " replaced_by_group_id = ? WHERE id = ?",
                (
                    SESSION_SUPERSEDED,
                    now,
                    "superseded_by_reschedule",
                    group_id,
                    old["id"],
                ),
            )
        conn.execute(
            "INSERT INTO sessions"
            " (manuscript_id, decision_id, group_id, room, starts_at, ends_at,"
            "  status, created_at, confirmed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item["manuscript_id"],
                item["decision_id"],
                group_id,
                item["room"],
                item["starts_at"],
                item["ends_at"],
                SESSION_CONFIRMED,
                now,
                now,
            ),
        )

    conn.execute(
        "UPDATE schedule_groups SET status = ?, end_reason = ?, ended_at = ? WHERE id = ?",
        (GROUP_CONFIRMED, "all_accepted", now, group_id),
    )


# ---------------------------------------------------------------------------
# Conflict-withdrawal hook (called inside register_conflict's transaction)
# ---------------------------------------------------------------------------

def decisions_withdrawn_hook(
    conn: sqlite3.Connection, decision_ids: list[int], now: str
) -> dict[str, list[int]]:
    """Void sessions and cancel pending groups tied to withdrawn decisions.

    Returns the ids changed so the conflict response can report them.
    """
    if not decision_ids:
        return {"voided_session_ids": [], "cancelled_schedule_group_ids": []}

    placeholders = ",".join("?" for _ in decision_ids)

    session_rows = conn.execute(
        f"SELECT id, manuscript_id FROM sessions"
        f" WHERE status = ? AND decision_id IN ({placeholders})",
        (SESSION_CONFIRMED, *decision_ids),
    ).fetchall()
    voided_session_ids: list[int] = []
    affected_manuscripts: set[int] = set()
    for row in session_rows:
        conn.execute(
            "UPDATE sessions SET status = ?, ended_at = ?, end_reason = ? WHERE id = ?",
            (SESSION_VOIDED, now, "decision_withdrawn", row["id"]),
        )
        voided_session_ids.append(row["id"])
        affected_manuscripts.add(row["manuscript_id"])

    # Pending items name the decision they reserve against.
    item_rows = conn.execute(
        f"SELECT DISTINCT manuscript_id FROM schedule_items"
        f" WHERE decision_id IN ({placeholders})",
        decision_ids,
    ).fetchall()
    for row in item_rows:
        affected_manuscripts.add(row["manuscript_id"])

    cancelled_group_ids: list[int] = []
    if affected_manuscripts:
        ms_placeholders = ",".join("?" for _ in affected_manuscripts)
        group_rows = conn.execute(
            f"SELECT DISTINCT g.id FROM schedule_groups g"
            f" JOIN schedule_items i ON i.group_id = g.id"
            f" WHERE g.status = ? AND i.manuscript_id IN ({ms_placeholders})",
            (GROUP_PENDING, *affected_manuscripts),
        ).fetchall()
        for row in group_rows:
            conn.execute(
                "UPDATE schedule_groups SET status = ?, end_reason = ?, ended_at = ?"
                " WHERE id = ? AND status = ?",
                (
                    GROUP_CANCELLED,
                    "decision_withdrawn",
                    now,
                    row["id"],
                    GROUP_PENDING,
                ),
            )
            cancelled_group_ids.append(row["id"])

    return {
        "voided_session_ids": voided_session_ids,
        "cancelled_schedule_group_ids": cancelled_group_ids,
    }


# ---------------------------------------------------------------------------
# Editor queries
# ---------------------------------------------------------------------------

def editor_list_sessions(db_path: str, include_history: bool = False) -> list[dict[str, Any]]:
    with database.read_tx(db_path) as conn:
        if include_history:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY starts_at, id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE status = ? ORDER BY starts_at, id",
                (SESSION_CONFIRMED,),
            ).fetchall()
        return [session_payload(conn, row) for row in rows]


def editor_occupancy(db_path: str) -> list[dict[str, Any]]:
    """All currently occupying slots: confirmed sessions + pending reservations."""
    expire_due_groups(db_path)
    with database.read_tx(db_path) as conn:
        out: list[dict[str, Any]] = []
        for cand in _occupancy_candidates(conn):
            out.append(
                {
                    "kind": cand["kind"],
                    "ref_id": cand["ref_id"],
                    "group_id": cand["group_id"],
                    "manuscript_id": cand["manuscript_id"],
                    "author_id": cand["author_id"],
                    "room": cand["room"],
                    "starts_at": cand["start"].isoformat(timespec="seconds"),
                    "ends_at": cand["end"].isoformat(timespec="seconds"),
                }
            )
        out.sort(key=lambda x: (x["starts_at"], x["room"]))
        return out


def editor_list_groups(db_path: str) -> list[dict[str, Any]]:
    expire_due_groups(db_path)
    with database.read_tx(db_path) as conn:
        rows = conn.execute("SELECT * FROM schedule_groups ORDER BY id DESC").fetchall()
        return [group_payload(conn, row) for row in rows]


def editor_get_group(db_path: str, group_id: int) -> dict[str, Any]:
    expire_due_groups(db_path)
    with database.read_tx(db_path) as conn:
        group = _get_group_row(conn, group_id)
        return group_payload(conn, group)


# ---------------------------------------------------------------------------
# Author queries — own manuscripts only
# ---------------------------------------------------------------------------

def _author_item_payload(
    conn: sqlite3.Connection, item: sqlite3.Row, group: sqlite3.Row, now_dt: datetime
) -> dict[str, Any]:
    deadline = parse_utc(group["confirmation_deadline"])
    can_respond = (
        group["status"] == GROUP_PENDING
        and item["response"] == RESP_WAITING
        and deadline > now_dt
    )
    return {
        "kind": "confirmation_request",
        "item_id": item["id"],
        "id": item["id"],
        "group_id": item["group_id"],
        "manuscript_id": item["manuscript_id"],
        "manuscript_title": require_row(
            conn, "manuscripts", item["manuscript_id"], "manuscript"
        )["title"],
        "decision_id": item["decision_id"],
        **_room_fields(item["room"]),
        "starts_at": item["starts_at"],
        "ends_at": item["ends_at"],
        "my_response": item["response"],
        "response": item["response"],
        "responded_at": item["responded_at"],
        "confirmation_deadline": group["confirmation_deadline"],
        "deadline": group["confirmation_deadline"],
        "group_status": group["status"],
        "status": group["status"],
        "end_reason": group["end_reason"],
        "can_respond": can_respond,
    }


def author_schedule_grouped(db_path: str, user: sqlite3.Row) -> dict[str, Any]:
    expire_due_groups(db_path)
    now_dt = datetime.now(timezone.utc)
    with database.read_tx(db_path) as conn:
        sessions = conn.execute(
            "SELECT s.* FROM sessions s"
            " JOIN manuscripts m ON m.id = s.manuscript_id"
            " WHERE m.author_id = ? AND s.status = ?"
            " ORDER BY s.starts_at",
            (user["id"], SESSION_CONFIRMED),
        ).fetchall()
        confirmed = [session_payload(conn, s) for s in sessions]

        items = conn.execute(
            "SELECT i.* FROM schedule_items i"
            " JOIN manuscripts m ON m.id = i.manuscript_id"
            " WHERE m.author_id = ? ORDER BY i.id DESC",
            (user["id"],),
        ).fetchall()
        pending: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        for item in items:
            group = _get_group_row(conn, item["group_id"])
            view = _author_item_payload(conn, item, group, now_dt)
            if group["status"] == GROUP_PENDING:
                pending.append(view)
            else:
                history.append(view)
        return {
            "author": {"username": user["username"], "display_name": user["display_name"]},
            "confirmed_sessions": confirmed,
            "confirmed": confirmed,
            "pending_confirmations": pending,
            "pending": pending,
            "history": history,
            "past_arrangements": history,
        }


def author_schedule_flat(db_path: str, user: sqlite3.Row) -> list[dict[str, Any]]:
    grouped = author_schedule_grouped(db_path, user)
    entries: list[dict[str, Any]] = []
    for session in grouped["confirmed_sessions"]:
        entries.append({"kind": "confirmed_session", **session})
    entries.extend(grouped["pending_confirmations"])
    entries.extend(grouped["history"])
    entries.sort(key=lambda e: e.get("starts_at") or "")
    return entries
