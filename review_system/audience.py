"""Audience seat reservations for confirmed conference talks.

Overview
--------
* The locally prepared accounts double as the audience: any authenticated
  account may register for talks and cancel its own registrations.  A talk is
  open while it has a confirmed session backed by a live published decision
  and has not started yet; pending (not yet confirmed) schedule changes never
  affect existing registrations.
* Editors give each configured room an audience capacity (default zero);
  every talk uses the capacity of the room its confirmed session is in.
* A registration attaches to the *talk* (manuscript + the decision era it was
  registered under).  A confirmed reschedule that reuses the original
  decision therefore carries the audience list over automatically — editors
  never copy registrations by hand — while a withdrawn decision terminates
  the registrations of not-yet-started talks and never transfers them to a
  new decision.
* A registration is ``waitlisted`` or ``seated`` (active), then ``cancelled``
  (by the audience member) or ``ended`` (decision withdrawn).  One audience
  member has at most one active application per talk; re-registering while
  active changes nothing, and a re-registration after cancelling queues
  behind everyone else.
* While seats remain, waitlisted applications are seated in registration
  order, skipping applicants whose other held seats overlap the talk
  (back-to-back slots do not overlap).  One person may waitlist overlapping
  talks but never holds two seats whose time slots overlap; the same global
  registration order decides when one person could be promoted in several
  talks at once.
* Confirmed seats are sticky: they are never bumped by earlier waitlisted
  applicants that later become eligible.  Only a confirmed reschedule or a
  capacity change re-evaluates seated lists — entries that still fit are kept
  preferentially in original registration order; seats that now clash or
  exceed the new capacity return to the waitlist keeping their original
  order.  Seats freed by cancellations, cancelled talks and reschedules are
  propagated to every affected talk by the same promotion pass.
* Once a talk starts, its admission list is frozen: no registration,
  cancellation or promotion, and later capacity or decision changes do not
  rewrite it.  Frozen seats still count for overlap checks by their original
  time slot.

Concurrency: every mutation runs inside the shared serialised
``BEGIN IMMEDIATE`` transaction (see :mod:`review_system.db`), and schedule
finalisation / decision withdrawal re-evaluate the affected lists inside
their own transaction, so no partially-rescheduled intermediate list is ever
visible.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import config, db as database
from .db import require_row, utcnow
from .errors import ApiError
from .scheduling import SESSION_CONFIRMED, parse_utc

REG_WAITLISTED = "waitlisted"
REG_SEATED = "seated"
REG_CANCELLED = "cancelled"
REG_ENDED = "ended"

_ACTIVE_STATUSES = (REG_WAITLISTED, REG_SEATED)


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Half-open interval overlap; back-to-back slots do not overlap."""
    return a_start < b_end and b_start < a_end


# ---------------------------------------------------------------------------
# Room audience capacity (editor-managed, default zero)
# ---------------------------------------------------------------------------

def _capacity_map(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT room_code, capacity FROM room_audience_capacities").fetchall()
    return {row["room_code"]: row["capacity"] for row in rows}


def _room_audience_capacity(capacities: dict[str, int], room_ref: Any) -> int:
    resolved = config.resolve_room(room_ref)
    code = resolved["code"] if resolved else str(room_ref)
    return capacities.get(code, 0)


def room_capacities(db_path: str) -> dict[str, int]:
    with database.read_tx(db_path) as conn:
        return _capacity_map(conn)


# ---------------------------------------------------------------------------
# Talk/session resolution.  Registrations key on (manuscript_id, decision_id)
# — the talk's decision era — and always evaluate against the era's *current*
# session (confirmed if any, otherwise the most recent one, e.g. a frozen
# session that was voided after it started).
# ---------------------------------------------------------------------------

def _era_sessions(conn: sqlite3.Connection) -> dict[tuple[int, int], sqlite3.Row]:
    by_era: dict[tuple[int, int], sqlite3.Row] = {}
    rows = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    for row in rows:
        key = (row["manuscript_id"], row["decision_id"])
        existing = by_era.get(key)
        if existing is not None and existing["status"] == SESSION_CONFIRMED:
            continue  # the live session of an era always wins
        by_era[key] = row
    return by_era


def _current_confirmed_session(conn: sqlite3.Connection, manuscript_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sessions WHERE manuscript_id = ? AND status = ?",
        (manuscript_id, SESSION_CONFIRMED),
    ).fetchone()


def _active_registration(
    conn: sqlite3.Connection, manuscript_id: int, decision_id: int, audience_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM audience_registrations"
        " WHERE manuscript_id = ? AND decision_id = ? AND audience_id = ?"
        " AND status IN ('waitlisted', 'seated')",
        (manuscript_id, decision_id, audience_id),
    ).fetchone()


# ---------------------------------------------------------------------------
# Seat evaluation
# ---------------------------------------------------------------------------

def _build_held_state(
    conn: sqlite3.Connection, now_dt: datetime
) -> tuple[dict[tuple[int, int], sqlite3.Row], dict[int, list], dict[int, int]]:
    """Currently held seats: per-session counts and per-audience intervals.

    A seat is held when its registration is ``seated`` and the talk's session
    is either live (confirmed, not yet started) or frozen (already started —
    frozen seats keep blocking overlapping talks by their original slot even
    if the session was voided afterwards).
    """
    sessions_by_era = _era_sessions(conn)
    held: dict[int, list[tuple[datetime, datetime]]] = {}
    seated_count: dict[int, int] = {}
    rows = conn.execute(
        "SELECT * FROM audience_registrations WHERE status = ? ORDER BY id",
        (REG_SEATED,),
    ).fetchall()
    for reg in rows:
        sess = sessions_by_era.get((reg["manuscript_id"], reg["decision_id"]))
        if sess is None:
            continue
        start = parse_utc(sess["starts_at"])
        end = parse_utc(sess["ends_at"])
        started = start <= now_dt
        if sess["status"] != SESSION_CONFIRMED and not started:
            # Dead era (voided before start): the withdrawal hook terminates
            # these registrations; nothing valid should reach here.
            continue
        seated_count[sess["id"]] = seated_count.get(sess["id"], 0) + 1
        held.setdefault(reg["audience_id"], []).append((start, end))
    return sessions_by_era, held, seated_count


def _clashes(held_intervals: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    return any(_overlaps(start, end, h_start, h_end) for h_start, h_end in held_intervals)


def _promote_pass(conn: sqlite3.Connection, now_iso: str) -> list[int]:
    """Seat waitlisted applications in registration order while seats remain.

    One global pass over every open talk: applicants whose held seats overlap
    the talk are skipped (they keep their waitlist position), and the same
    order governs one person's simultaneous chances across several talks.
    Seating someone never frees a seat, so a single ordered pass converges.
    """
    now_dt = parse_utc(now_iso)
    sessions_by_era, held, seated_count = _build_held_state(conn, now_dt)
    capacities = _capacity_map(conn)
    promoted: list[int] = []
    rows = conn.execute(
        "SELECT * FROM audience_registrations WHERE status = ? ORDER BY id",
        (REG_WAITLISTED,),
    ).fetchall()
    for reg in rows:
        sess = sessions_by_era.get((reg["manuscript_id"], reg["decision_id"]))
        if sess is None or sess["status"] != SESSION_CONFIRMED:
            continue
        start = parse_utc(sess["starts_at"])
        end = parse_utc(sess["ends_at"])
        if start <= now_dt:
            continue  # already started: frozen, no more promotions
        capacity = _room_audience_capacity(capacities, sess["room"])
        if seated_count.get(sess["id"], 0) >= capacity:
            continue
        if _clashes(held.get(reg["audience_id"], []), start, end):
            continue
        conn.execute(
            "UPDATE audience_registrations SET status = ?, seated_at = ? WHERE id = ?",
            (REG_SEATED, now_iso, reg["id"]),
        )
        seated_count[sess["id"]] = seated_count.get(sess["id"], 0) + 1
        held.setdefault(reg["audience_id"], []).append((start, end))
        promoted.append(reg["id"])
    return promoted


def _reevaluate(conn: sqlite3.Connection, now_iso: str) -> dict[str, list[int]]:
    """Re-check seated lists after a confirmed reschedule or capacity change.

    Frozen (started) talks are untouched.  Every other seated entry is kept
    preferentially in original registration order as long as it fits the
    current capacity and does not overlap the keeper's other seats; entries
    that clash or exceed the capacity return to the waitlist with their
    original order, then the normal promotion pass refills remaining seats.
    """
    now_dt = parse_utc(now_iso)
    sessions_by_era = _era_sessions(conn)
    capacities = _capacity_map(conn)
    held: dict[int, list[tuple[datetime, datetime]]] = {}
    seated_count: dict[int, int] = {}
    live_seated: list[tuple[sqlite3.Row, sqlite3.Row, datetime, datetime]] = []

    rows = conn.execute(
        "SELECT * FROM audience_registrations WHERE status = ? ORDER BY id",
        (REG_SEATED,),
    ).fetchall()
    for reg in rows:
        sess = sessions_by_era.get((reg["manuscript_id"], reg["decision_id"]))
        if sess is None:
            continue
        start = parse_utc(sess["starts_at"])
        end = parse_utc(sess["ends_at"])
        if start <= now_dt:
            # Frozen admission list: preserved as-is, still blocks overlaps.
            seated_count[sess["id"]] = seated_count.get(sess["id"], 0) + 1
            held.setdefault(reg["audience_id"], []).append((start, end))
            continue
        if sess["status"] != SESSION_CONFIRMED:
            continue
        live_seated.append((reg, sess, start, end))

    demoted: list[int] = []
    for reg, sess, start, end in live_seated:
        capacity = _room_audience_capacity(capacities, sess["room"])
        fits = seated_count.get(sess["id"], 0) < capacity
        if fits and not _clashes(held.get(reg["audience_id"], []), start, end):
            seated_count[sess["id"]] = seated_count.get(sess["id"], 0) + 1
            held.setdefault(reg["audience_id"], []).append((start, end))
        else:
            # Back to the waitlist; the original registered_at/id keep the
            # application's original queue order.
            conn.execute(
                "UPDATE audience_registrations SET status = ?, seated_at = NULL WHERE id = ?",
                (REG_WAITLISTED, reg["id"]),
            )
            demoted.append(reg["id"])

    promoted = _promote_pass(conn, now_iso)
    return {"demoted_registration_ids": demoted, "promoted_registration_ids": promoted}


# ---------------------------------------------------------------------------
# Hooks called inside other features' write transactions
# ---------------------------------------------------------------------------

def on_sessions_finalized(conn: sqlite3.Connection, now_iso: str) -> dict[str, list[int]]:
    """Re-evaluate audience lists after a schedule group is confirmed.

    Runs inside the finalisation transaction: registrations follow their talk
    to the new session automatically (they key on the manuscript + decision
    era), then seats are re-checked against the new times/rooms/capacities.
    """
    return _reevaluate(conn, now_iso)


def decisions_withdrawn_hook(
    conn: sqlite3.Connection, decision_ids: list[int], now_iso: str
) -> dict[str, list[int]]:
    """Terminate registrations of not-yet-started talks whose decision died.

    Started talks keep their frozen admission lists.  Seats and overlap
    blocks released here are propagated to other affected talks by the
    promotion pass, all inside the caller's transaction.
    """
    if not decision_ids:
        return {"ended_registration_ids": [], "promoted_registration_ids": []}
    now_dt = parse_utc(now_iso)
    sessions_by_era = _era_sessions(conn)
    placeholders = ",".join("?" for _ in decision_ids)
    rows = conn.execute(
        "SELECT * FROM audience_registrations"
        " WHERE status IN ('waitlisted', 'seated')"
        f" AND decision_id IN ({placeholders}) ORDER BY id",
        decision_ids,
    ).fetchall()
    ended: list[int] = []
    for reg in rows:
        sess = sessions_by_era.get((reg["manuscript_id"], reg["decision_id"]))
        started = sess is not None and parse_utc(sess["starts_at"]) <= now_dt
        if started:
            continue  # the frozen admission list of a started talk is preserved
        conn.execute(
            "UPDATE audience_registrations SET status = ?, ended_at = ?, end_reason = ?"
            " WHERE id = ?",
            (REG_ENDED, now_iso, "decision_withdrawn", reg["id"]),
        )
        ended.append(reg["id"])
    promoted = _promote_pass(conn, now_iso)
    return {"ended_registration_ids": ended, "promoted_registration_ids": promoted}


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

def _session_brief(sess: sqlite3.Row, now_dt: datetime) -> dict[str, Any]:
    resolved = config.resolve_room(sess["room"])
    return {
        "session_id": sess["id"],
        "room": sess["room"],
        "room_code": resolved["code"] if resolved else None,
        "room_id": resolved["id"] if resolved else None,
        "room_name": resolved["name"] if resolved else str(sess["room"]),
        "starts_at": sess["starts_at"],
        "ends_at": sess["ends_at"],
        "session_status": sess["status"],
        "started": parse_utc(sess["starts_at"]) <= now_dt,
    }


def _waitlist_position(conn: sqlite3.Connection, reg: sqlite3.Row) -> int | None:
    if reg["status"] != REG_WAITLISTED:
        return None
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM audience_registrations"
        " WHERE manuscript_id = ? AND decision_id = ? AND status = ? AND id < ?",
        (reg["manuscript_id"], reg["decision_id"], REG_WAITLISTED, reg["id"]),
    ).fetchone()
    return row["c"] + 1


def _registration_payload(
    conn: sqlite3.Connection,
    reg: sqlite3.Row,
    now_dt: datetime,
    sessions_by_era: dict[tuple[int, int], sqlite3.Row] | None = None,
) -> dict[str, Any]:
    if sessions_by_era is None:
        sessions_by_era = _era_sessions(conn)
    manuscript = require_row(conn, "manuscripts", reg["manuscript_id"], "manuscript")
    sess = sessions_by_era.get((reg["manuscript_id"], reg["decision_id"]))
    return {
        "registration_id": reg["id"],
        "manuscript_id": reg["manuscript_id"],
        "manuscript_title": manuscript["title"],
        "decision_id": reg["decision_id"],
        "status": reg["status"],
        "registered_at": reg["registered_at"],
        "seated_at": reg["seated_at"],
        "cancelled_at": reg["cancelled_at"],
        "ended_at": reg["ended_at"],
        "end_reason": reg["end_reason"],
        "waitlist_position": _waitlist_position(conn, reg),
        "session": _session_brief(sess, now_dt) if sess is not None else None,
    }


# ---------------------------------------------------------------------------
# Audience operations
# ---------------------------------------------------------------------------

def register(db_path: str, user: sqlite3.Row, manuscript_id: int) -> dict[str, Any]:
    """Register the audience member for a talk (idempotent while active).

    A repeated registration keeps the existing application and its queue
    position; only a fresh application after a cancellation queues last.
    """
    with database.write_tx(db_path) as conn:
        now_iso = utcnow()
        now_dt = parse_utc(now_iso)
        require_row(conn, "manuscripts", manuscript_id, "manuscript")
        session = _current_confirmed_session(conn, manuscript_id)
        if session is None:
            raise ApiError(
                409,
                "not_open",
                "this talk has no confirmed session; registration is not open",
            )
        decision = require_row(conn, "decisions", session["decision_id"], "decision")
        if decision["status"] != "published":
            raise ApiError(
                409,
                "not_open",
                "the decision backing this talk is no longer active",
            )
        if parse_utc(session["starts_at"]) <= now_dt:
            raise ApiError(
                409,
                "session_started",
                "the talk has already started; registration is closed",
            )

        existing = _active_registration(
            conn, manuscript_id, session["decision_id"], user["id"]
        )
        if existing is not None:
            # Re-registering while active changes nothing, order included.
            payload = _registration_payload(conn, existing, now_dt)
            payload["already_registered"] = True
            return {"created": False, "registration": payload}

        cur = conn.execute(
            "INSERT INTO audience_registrations"
            " (manuscript_id, decision_id, audience_id, status, registered_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (manuscript_id, session["decision_id"], user["id"], REG_WAITLISTED, now_iso, now_iso),
        )
        registration_id = cur.lastrowid
        # The new application (and any previously blocked applicant) takes a
        # free seat in registration order.
        _promote_pass(conn, now_iso)
        reg = require_row(conn, "audience_registrations", registration_id, "registration")
        payload = _registration_payload(conn, reg, now_dt)
        payload["already_registered"] = False
        return {"created": True, "registration": payload}


def cancel(db_path: str, user: sqlite3.Row, manuscript_id: int) -> dict[str, Any]:
    """Cancel the caller's active registration for a talk."""
    with database.write_tx(db_path) as conn:
        now_iso = utcnow()
        now_dt = parse_utc(now_iso)
        require_row(conn, "manuscripts", manuscript_id, "manuscript")
        session = _current_confirmed_session(conn, manuscript_id)
        reg = None
        if session is not None:
            reg = _active_registration(conn, manuscript_id, session["decision_id"], user["id"])
        if reg is None:
            raise ApiError(404, "not_found", "no active registration for this talk")
        if parse_utc(session["starts_at"]) <= now_dt:
            raise ApiError(
                409,
                "session_started",
                "the talk has already started; its admission list is frozen",
            )
        conn.execute(
            "UPDATE audience_registrations SET status = ?, cancelled_at = ? WHERE id = ?",
            (REG_CANCELLED, now_iso, reg["id"]),
        )
        # A freed seat (or a removed overlap block) propagates to every
        # affected talk in registration order.
        _promote_pass(conn, now_iso)
        reg = require_row(conn, "audience_registrations", reg["id"], "registration")
        return _registration_payload(conn, reg, now_dt)


def list_talks(db_path: str, user: sqlite3.Row) -> list[dict[str, Any]]:
    """Confirmed talks with seat availability and the caller's own status.

    Only counts are exposed — never other audience members' registrations.
    """
    with database.read_tx(db_path) as conn:
        now_dt = _now_dt()
        capacities = _capacity_map(conn)
        sessions = conn.execute(
            "SELECT * FROM sessions WHERE status = ? ORDER BY starts_at, id",
            (SESSION_CONFIRMED,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for sess in sessions:
            manuscript = require_row(conn, "manuscripts", sess["manuscript_id"], "manuscript")
            counts = conn.execute(
                "SELECT"
                " COALESCE(SUM(CASE WHEN status = 'seated' THEN 1 ELSE 0 END), 0) AS seated,"
                " COALESCE(SUM(CASE WHEN status = 'waitlisted' THEN 1 ELSE 0 END), 0) AS waiting"
                " FROM audience_registrations"
                " WHERE manuscript_id = ? AND decision_id = ?"
                " AND status IN ('waitlisted', 'seated')",
                (sess["manuscript_id"], sess["decision_id"]),
            ).fetchone()
            capacity = _room_audience_capacity(capacities, sess["room"])
            started = parse_utc(sess["starts_at"]) <= now_dt
            mine = _active_registration(
                conn, sess["manuscript_id"], sess["decision_id"], user["id"]
            )
            my_registration = None
            if mine is not None:
                my_registration = {
                    "registration_id": mine["id"],
                    "status": mine["status"],
                    "registered_at": mine["registered_at"],
                    "seated_at": mine["seated_at"],
                    "waitlist_position": _waitlist_position(conn, mine),
                }
            out.append(
                {
                    "manuscript_id": sess["manuscript_id"],
                    "manuscript_title": manuscript["title"],
                    **_session_brief(sess, now_dt),
                    "audience_capacity": capacity,
                    "seated_count": counts["seated"],
                    "seats_available": max(0, capacity - counts["seated"]),
                    "waitlist_count": counts["waiting"],
                    "open_for_registration": not started,
                    "my_registration": my_registration,
                }
            )
        return out


def my_registrations(db_path: str, user: sqlite3.Row) -> list[dict[str, Any]]:
    """Every registration of the caller, active and historical."""
    with database.read_tx(db_path) as conn:
        now_dt = _now_dt()
        sessions_by_era = _era_sessions(conn)
        rows = conn.execute(
            "SELECT * FROM audience_registrations WHERE audience_id = ? ORDER BY id DESC",
            (user["id"],),
        ).fetchall()
        return [_registration_payload(conn, row, now_dt, sessions_by_era) for row in rows]


# ---------------------------------------------------------------------------
# Editor operations
# ---------------------------------------------------------------------------

def set_room_capacity(db_path: str, room_ref: Any, capacity: int) -> dict[str, Any]:
    """Set a room's audience capacity and re-evaluate affected talks."""
    room = config.resolve_room(room_ref)
    if room is None:
        raise ApiError(
            404, "unknown_room", f"room {room_ref!r} is not in local room configuration"
        )
    with database.write_tx(db_path) as conn:
        now_iso = utcnow()
        conn.execute(
            "INSERT INTO room_audience_capacities (room_code, capacity, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(room_code) DO UPDATE SET capacity = excluded.capacity,"
            " updated_at = excluded.updated_at",
            (room["code"], int(capacity), now_iso),
        )
        effects = _reevaluate(conn, now_iso)
        return {
            "room": {
                "id": room["id"],
                "code": room["code"],
                "name": room["name"],
                "capacity": room["capacity"],
                "audience_capacity": int(capacity),
            },
            "effects": effects,
        }


def _admissions_payload(
    conn: sqlite3.Connection, manuscript: sqlite3.Row, sess: sqlite3.Row, now_dt: datetime
) -> dict[str, Any]:
    capacities = _capacity_map(conn)
    capacity = _room_audience_capacity(capacities, sess["room"])
    rows = conn.execute(
        "SELECT r.*, u.username, u.display_name FROM audience_registrations r"
        " JOIN users u ON u.id = r.audience_id"
        " WHERE r.manuscript_id = ? AND r.decision_id = ?"
        " AND r.status IN ('waitlisted', 'seated') ORDER BY r.id",
        (sess["manuscript_id"], sess["decision_id"]),
    ).fetchall()
    seated = [row for row in rows if row["status"] == REG_SEATED]
    waitlisted = [row for row in rows if row["status"] == REG_WAITLISTED]
    started = parse_utc(sess["starts_at"]) <= now_dt
    return {
        "manuscript_id": manuscript["id"],
        "manuscript_title": manuscript["title"],
        "decision_id": sess["decision_id"],
        "session": _session_brief(sess, now_dt),
        "frozen": started,
        "audience_capacity": capacity,
        "seated_count": len(seated),
        "seats_available": 0 if started else max(0, capacity - len(seated)),
        "waitlist_count": len(waitlisted),
        "seated": [
            {
                "registration_id": row["id"],
                "audience": {
                    "username": row["username"],
                    "display_name": row["display_name"],
                },
                "registered_at": row["registered_at"],
                "seated_at": row["seated_at"],
            }
            for row in seated
        ],
        "waitlist": [
            {
                "position": position,
                "registration_id": row["id"],
                "audience": {
                    "username": row["username"],
                    "display_name": row["display_name"],
                },
                "registered_at": row["registered_at"],
            }
            for position, row in enumerate(waitlisted, start=1)
        ],
    }


def editor_admissions_for_manuscript(db_path: str, manuscript_id: int) -> dict[str, Any]:
    """The talk's current admission list (seated + waitlist), editor-only."""
    with database.read_tx(db_path) as conn:
        now_dt = _now_dt()
        manuscript = require_row(conn, "manuscripts", manuscript_id, "manuscript")
        sess = _current_confirmed_session(conn, manuscript_id)
        if sess is None:
            # Fall back to the most recent session so a frozen (started) list
            # stays readable even after the session was voided.
            sess = conn.execute(
                "SELECT * FROM sessions WHERE manuscript_id = ? ORDER BY id DESC LIMIT 1",
                (manuscript_id,),
            ).fetchone()
        if sess is None:
            raise ApiError(
                404, "not_scheduled", f"manuscript {manuscript_id} has no scheduled talk"
            )
        return _admissions_payload(conn, manuscript, sess, now_dt)


def editor_admissions_for_session(db_path: str, session_id: int) -> dict[str, Any]:
    """Admission list of the decision era the given session belongs to."""
    with database.read_tx(db_path) as conn:
        now_dt = _now_dt()
        sess = require_row(conn, "sessions", session_id, "session")
        manuscript = require_row(conn, "manuscripts", sess["manuscript_id"], "manuscript")
        era_current = _era_sessions(conn).get(
            (sess["manuscript_id"], sess["decision_id"]), sess
        )
        return _admissions_payload(conn, manuscript, era_current, now_dt)
