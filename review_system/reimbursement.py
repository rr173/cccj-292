"""Speaker travel reimbursement: allowances, receipts, claims, payment batches
and finance results.

Money is always integer **cents**.  Lifecycle
--------------------------------------------
1. The editor grants one allowance per (manuscript, decision) to the
   manuscript's author.  The manuscript must have a confirmed talk session and
   the decision must still be live.
2. The editor registers receipts (unique voucher number, owning author, face
   value).
3. The editor approves a *claim* of ``amount`` against one allowance and one
   receipt belonging to the same author.  Approval is checked against three
   independent caps: the fixed conference budget, the allowance, and the
   receipt face value.
4. Approved cents are handed to conference finance in *batches*.  A batch
   either reserves every listed claim portion or rejects the whole request.
   Batch creation is idempotent on ``(request number, content)``.
5. Finance reports results per payment number — possibly in several rounds,
   possibly zero paid.  The unpaid difference is released and never silently
   re-queued.  Until a result arrives the instruction keeps encumbering its
   full amount.

Withdrawal of the backing decision releases only the *not-yet-delivered*
approved cents.  Delivered instructions may still be settled, unknown amounts
stay encumbered, and paid cents remain spend.  A new decision never revives an
old allowance.

Concurrency: every mutation runs inside the shared serialised
``BEGIN IMMEDIATE`` transaction (see :mod:`review_system.db`), so claims,
batches, results and withdrawals cannot interleave into an over-committed
state.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable

from . import config, db as database
from .db import require_row, utcnow
from .errors import ApiError
from . import scheduling

ALLOWANCE_ACTIVE = "active"
ALLOWANCE_RELEASED = "released"


# ---------------------------------------------------------------------------
# Exposure / cap arithmetic
# ---------------------------------------------------------------------------

_EXPOSURE_SQL = """
SELECT c.id                AS claim_id,
       c.amount_cents      AS claim_amount,
       c.receipt_id        AS receipt_id,
       a.id                AS allowance_id,
       a.released_at       AS allowance_released_at,
       i.id                AS instruction_id,
       i.amount_cents      AS instruction_amount,
       r.id                AS result_id,
       r.paid_cents        AS paid_cents
FROM reimbursement_claims c
JOIN reimbursement_allowances a ON a.id = c.allowance_id
LEFT JOIN payment_instructions i ON i.claim_id = c.id
LEFT JOIN payment_results r ON r.instruction_id = i.id
"""


def _exposure(
    conn: sqlite3.Connection,
    *,
    allowance_id: int | None = None,
    receipt_id: int | None = None,
    claim_id: int | None = None,
) -> dict[str, int]:
    """Aggregate the three capped buckets over a scope.

    Per claim: ``amount - delivered`` is either still *unsubmitted*, or was
    released by an allowance withdrawal.  A delivered instruction is either
    fully *pending* (no finance result) or contributes ``paid`` plus a released
    difference.  Released differences are therefore excluded from all three
    buckets but reported separately.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if allowance_id is not None:
        clauses.append("a.id = ?")
        params.append(allowance_id)
    if receipt_id is not None:
        clauses.append("c.receipt_id = ?")
        params.append(receipt_id)
    if claim_id is not None:
        clauses.append("c.id = ?")
        params.append(claim_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(_EXPOSURE_SQL + where, params).fetchall()

    claims: dict[int, dict[str, Any]] = {}
    for row in rows:
        state = claims.setdefault(
            row["claim_id"],
            {
                "amount": row["claim_amount"],
                "released_allowance": row["allowance_released_at"] is not None,
                "instructions": [],
            },
        )
        if row["instruction_id"] is not None:
            state["instructions"].append(row)

    approved_unsubmitted = 0
    delivered_pending = 0
    paid = 0
    released_unsubmitted = 0
    released_payment_difference = 0
    for state in claims.values():
        delivered = sum(i["instruction_amount"] for i in state["instructions"])
        undelivered = state["amount"] - delivered
        if state["released_allowance"]:
            released_unsubmitted += undelivered
        else:
            approved_unsubmitted += undelivered
        for instruction in state["instructions"]:
            if instruction["result_id"] is None:
                delivered_pending += instruction["instruction_amount"]
            else:
                paid += instruction["paid_cents"]
                released_payment_difference += (
                    instruction["instruction_amount"] - instruction["paid_cents"]
                )

    committed = approved_unsubmitted + delivered_pending + paid
    return {
        "approved_unsubmitted_cents": approved_unsubmitted,
        "delivered_pending_cents": delivered_pending,
        "paid_cents": paid,
        "released_cents": released_unsubmitted + released_payment_difference,
        "released_unsubmitted_cents": released_unsubmitted,
        "released_payment_difference_cents": released_payment_difference,
        "committed_cents": committed,
        "encumbered_cents": approved_unsubmitted + delivered_pending,
    }


def _with_remaining(exposure: dict[str, int], cap_cents: int) -> dict[str, int]:
    return {
        **exposure,
        "cap_cents": cap_cents,
        "remaining_cents": max(0, cap_cents - exposure["committed_cents"]),
    }


def _conference_budget() -> int:
    return config.REIMBURSEMENT_BUDGET_CENTS


def _receipt_capacity_used(conn: sqlite3.Connection, receipt_id: int) -> int:
    """Face value consumed on a receipt: every cent ever approved against it.

    The face value is consumed permanently.  A withdrawal releases undelivered
    *allowance* money, but it does not turn the same physical bill into a
    fresh, reusable one for another allowance.  This is stricter than the
    three-bucket cap and therefore still satisfies it.
    """
    return conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM reimbursement_claims"
        " WHERE receipt_id = ?",
        (receipt_id,),
    ).fetchone()["s"]


def _assert_caps(
    conn: sqlite3.Connection,
    allowance: sqlite3.Row,
    receipt: sqlite3.Row,
    amount_cents: int,
) -> None:
    """Reject an approval that would push any capped bucket over its limit.

    Each scope caps the same three buckets: approved-but-not-handed-to-finance,
    handed-to-finance-without-result, and actually paid.  Released money is
    excluded by ``_exposure``.  A receipt additionally never reuses face value
    already approved on it, released or not.
    """
    receipt_used = _receipt_capacity_used(conn, receipt["id"])
    checks = [
        (
            _exposure(conn, allowance_id=allowance["id"]),
            allowance["amount_cents"],
            "allowance_exceeded",
            f"the allowance would exceed its {allowance['amount_cents']} cent limit",
        ),
        (
            _exposure(conn, receipt_id=receipt["id"]),
            receipt["amount_cents"],
            "receipt_exceeded",
            f"the receipt would exceed its {receipt['amount_cents']} cent face value",
        ),
        (
            _exposure(conn),
            _conference_budget(),
            "budget_exceeded",
            f"the conference reimbursement budget of {_conference_budget()} cents"
            " would be exceeded",
        ),
    ]
    for exposure, cap, code, detail in checks:
        if exposure["committed_cents"] + amount_cents > cap:
            raise ApiError(409, code, detail)
    if receipt_used + amount_cents > receipt["amount_cents"]:
        raise ApiError(
            409,
            "receipt_exceeded",
            f"voucher {receipt['voucher_no']!r} has only"
            f" {max(0, receipt['amount_cents'] - receipt_used)} cents of face value"
            " not already approved",
        )


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

def _user_brief(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
    }


def _allowance_status(row: sqlite3.Row) -> str:
    return ALLOWANCE_RELEASED if row["released_at"] else ALLOWANCE_ACTIVE


def allowance_payload(conn: sqlite3.Connection, allowance: sqlite3.Row) -> dict[str, Any]:
    manuscript = require_row(conn, "manuscripts", allowance["manuscript_id"], "manuscript")
    beneficiary = require_row(conn, "users", allowance["beneficiary_id"], "user")
    exposure = _with_remaining(
        _exposure(conn, allowance_id=allowance["id"]), allowance["amount_cents"]
    )
    if allowance["released_at"] is not None:
        # A released allowance can no longer commit anything, whatever the
        # arithmetic of its historical paid/pending amounts says.
        exposure["remaining_cents"] = 0
    return {
        "allowance_id": allowance["id"],
        "manuscript_id": allowance["manuscript_id"],
        "manuscript_title": manuscript["title"],
        "decision_id": allowance["decision_id"],
        "beneficiary": _user_brief(beneficiary),
        "amount_cents": allowance["amount_cents"],
        "status": _allowance_status(allowance),
        "released_at": allowance["released_at"],
        "release_reason": allowance["release_reason"],
        "created_at": allowance["created_at"],
        **exposure,
    }


def receipt_payload(conn: sqlite3.Connection, receipt: sqlite3.Row) -> dict[str, Any]:
    author = require_row(conn, "users", receipt["author_id"], "user")
    exposure = _with_remaining(
        _exposure(conn, receipt_id=receipt["id"]), receipt["amount_cents"]
    )
    capacity_used = _receipt_capacity_used(conn, receipt["id"])
    exposure["approved_total_cents"] = capacity_used
    exposure["remaining_cents"] = max(0, receipt["amount_cents"] - capacity_used)
    return {
        "receipt_id": receipt["id"],
        "voucher_no": receipt["voucher_no"],
        "author": _user_brief(author),
        "amount_cents": receipt["amount_cents"],
        "registered_at": receipt["created_at"],
        **exposure,
    }


def _claim_instruction_state(conn: sqlite3.Connection, claim_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT i.id, i.amount_cents, r.id AS result_id, r.paid_cents"
        " FROM payment_instructions i"
        " LEFT JOIN payment_results r ON r.instruction_id = i.id"
        " WHERE i.claim_id = ? ORDER BY i.id",
        (claim_id,),
    ).fetchall()
    delivered = sum(r["amount_cents"] for r in rows)
    pending = sum(r["amount_cents"] for r in rows if r["result_id"] is None)
    paid = sum((r["paid_cents"] or 0) for r in rows if r["result_id"] is not None)
    released = sum(
        (r["amount_cents"] - r["paid_cents"])
        for r in rows
        if r["result_id"] is not None
    )
    return {
        "delivered_cents": delivered,
        "delivered_pending_cents": pending,
        "paid_cents": paid,
        "released_payment_difference_cents": released,
        "instruction_count": len(rows),
    }


def claim_payload(conn: sqlite3.Connection, claim: sqlite3.Row) -> dict[str, Any]:
    allowance = require_row(conn, "reimbursement_allowances", claim["allowance_id"], "allowance")
    receipt = require_row(conn, "reimbursement_receipts", claim["receipt_id"], "receipt")
    state = _claim_instruction_state(conn, claim["id"])
    undelivered = claim["amount_cents"] - state["delivered_cents"]
    if allowance["released_at"]:
        released_by_allowance = undelivered
        remaining_unsubmitted = 0
    else:
        released_by_allowance = 0
        remaining_unsubmitted = undelivered
    return {
        "claim_id": claim["id"],
        "allowance_id": claim["allowance_id"],
        "receipt_id": claim["receipt_id"],
        "voucher_no": receipt["voucher_no"],
        "author": _user_brief(require_row(conn, "users", receipt["author_id"], "user")),
        "amount_cents": claim["amount_cents"],
        "approved_at": claim["created_at"],
        "allowance_status": _allowance_status(allowance),
        "remaining_unsubmitted_cents": remaining_unsubmitted,
        "delivered_cents": state["delivered_cents"],
        "delivered_pending_cents": state["delivered_pending_cents"],
        "paid_cents": state["paid_cents"],
        "released_payment_difference_cents": state[
            "released_payment_difference_cents"
        ],
        "released_by_allowance_cents": released_by_allowance,
        "released_cents": (
            state["released_payment_difference_cents"] + released_by_allowance
        ),
        "instruction_count": state["instruction_count"],
    }


def batch_payload(conn: sqlite3.Connection, batch: sqlite3.Row) -> dict[str, Any]:
    """The immutable delivery list: payment numbers, payees, vouchers, cents.

    Deliberately contains no finance result data, so an idempotent replay is
    byte-for-byte the original answer even after results arrive.
    """
    rows = conn.execute(
        "SELECT * FROM payment_instructions WHERE batch_id = ? ORDER BY id",
        (batch["id"],),
    ).fetchall()
    payments = []
    for row in rows:
        payee = require_row(conn, "users", row["payee_id"], "user")
        # Only what finance needs to pay and to report back on: the payment
        # number, payee, voucher and cents.  No claim/allowance/decision ids.
        payments.append(
            {
                "payment_no": row["payment_no"],
                "voucher_no": row["voucher_no"],
                "payee": _user_brief(payee),
                "amount_cents": row["amount_cents"],
            }
        )
    return {
        "batch_id": batch["id"],
        "idempotency_key": batch["idempotency_key"],
        "created_at": batch["created_at"],
        "total_cents": sum(p["amount_cents"] for p in payments),
        "payment_count": len(payments),
        "payments": payments,
    }


def _batch_progress(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT i.*, r.id AS result_id, r.paid_cents, r.released_cents,"
        " r.note AS result_note, r.created_at AS result_at"
        " FROM payment_instructions i"
        " LEFT JOIN payment_results r ON r.instruction_id = i.id"
        " WHERE i.batch_id = ? ORDER BY i.id",
        (batch_id,),
    ).fetchall()
    payments = []
    pending = settled = 0
    paid = released = 0
    for row in rows:
        payee = require_row(conn, "users", row["payee_id"], "user")
        is_settled = row["result_id"] is not None
        if is_settled:
            settled += 1
            paid += row["paid_cents"]
            released += row["released_cents"]
        else:
            pending += 1
        payments.append(
            {
                "payment_no": row["payment_no"],
                "instruction_id": row["id"],
                "claim_id": row["claim_id"],
                "allowance_id": row["allowance_id"],
                "voucher_no": row["voucher_no"],
                "payee": _user_brief(payee),
                "amount_cents": row["amount_cents"],
                "status": "settled" if is_settled else "pending",
                "paid_cents": row["paid_cents"],
                "released_cents": row["released_cents"],
                "result_note": row["result_note"],
                "result_at": row["result_at"],
            }
        )
    return {
        "batch_id": batch_id,
        "payment_count": len(rows),
        "progress": {
            "total": len(rows),
            "settled": settled,
            "pending": pending,
            "all_settled": len(rows) > 0 and pending == 0,
        },
        "totals": {
            "delivered_cents": sum(r["amount_cents"] for r in rows),
            "delivered_pending_cents": sum(
                r["amount_cents"] for r in rows if r["result_id"] is None
            ),
            "paid_cents": paid,
            "released_cents": released,
        },
        "payments": payments,
    }


def batch_detail(conn: sqlite3.Connection, batch: sqlite3.Row) -> dict[str, Any]:
    return {**batch_payload(conn, batch), **_batch_progress(conn, batch["id"])}


# ---------------------------------------------------------------------------
# Allowances
# ---------------------------------------------------------------------------

def create_allowance(
    db_path: str,
    manuscript_id: int,
    amount_cents: int,
    decision_id: int | None = None,
) -> dict[str, Any]:
    with database.write_tx(db_path) as conn:
        manuscript = require_row(conn, "manuscripts", manuscript_id, "manuscript")
        session = conn.execute(
            "SELECT * FROM sessions WHERE manuscript_id = ? AND status = ?",
            (manuscript_id, scheduling.SESSION_CONFIRMED),
        ).fetchone()
        if session is None:
            raise ApiError(
                409,
                "no_confirmed_session",
                "an allowance can only be granted to a manuscript with a confirmed talk",
            )
        effective_decision_id = session["decision_id"]
        if decision_id is not None and int(decision_id) != effective_decision_id:
            raise ApiError(
                409,
                "decision_mismatch",
                "the allowance must use the decision of the confirmed talk"
                f" ({effective_decision_id}); a confirmed reschedule reuses it",
            )
        decision = require_row(conn, "decisions", effective_decision_id, "decision")
        if decision["status"] != "published":
            raise ApiError(
                409,
                "decision_not_active",
                f"decision {decision['id']} is {decision['status']};"
                " an un-withdrawn published decision is required",
            )

        existing = conn.execute(
            "SELECT id FROM reimbursement_allowances"
            " WHERE manuscript_id = ? AND decision_id = ?",
            (manuscript_id, effective_decision_id),
        ).fetchone()
        if existing is not None:
            raise ApiError(
                409,
                "allowance_exists",
                "an allowance already exists for this manuscript and decision",
            )

        cur = conn.execute(
            "INSERT INTO reimbursement_allowances"
            " (manuscript_id, decision_id, beneficiary_id, amount_cents, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                manuscript_id,
                effective_decision_id,
                manuscript["author_id"],
                amount_cents,
                utcnow(),
            ),
        )
        allowance = require_row(
            conn, "reimbursement_allowances", cur.lastrowid, "allowance"
        )
        return allowance_payload(conn, allowance)


def list_allowances(db_path: str) -> list[dict[str, Any]]:
    with database.read_tx(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM reimbursement_allowances ORDER BY id"
        ).fetchall()
        return [allowance_payload(conn, row) for row in rows]


def get_allowance(db_path: str, allowance_id: int) -> dict[str, Any]:
    with database.read_tx(db_path) as conn:
        allowance = require_row(
            conn, "reimbursement_allowances", allowance_id, "allowance"
        )
        return allowance_payload(conn, allowance)


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

def create_receipt(
    db_path: str, voucher_no: str, author_id: int, amount_cents: int
) -> dict[str, Any]:
    voucher_no = voucher_no.strip()
    with database.write_tx(db_path) as conn:
        author = require_row(conn, "users", author_id, "user")
        if author["role"] != "author":
            raise ApiError(400, "not_author", "the receipt owner must be an author")
        existing = conn.execute(
            "SELECT id FROM reimbursement_receipts WHERE voucher_no = ?",
            (voucher_no,),
        ).fetchone()
        if existing is not None:
            raise ApiError(
                409,
                "receipt_exists",
                f"voucher number {voucher_no!r} is already registered",
            )
        cur = conn.execute(
            "INSERT INTO reimbursement_receipts (voucher_no, author_id, amount_cents, created_at)"
            " VALUES (?, ?, ?, ?)",
            (voucher_no, author_id, amount_cents, utcnow()),
        )
        receipt = require_row(conn, "reimbursement_receipts", cur.lastrowid, "receipt")
        return receipt_payload(conn, receipt)


def list_receipts(db_path: str) -> list[dict[str, Any]]:
    with database.read_tx(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM reimbursement_receipts ORDER BY id"
        ).fetchall()
        return [receipt_payload(conn, row) for row in rows]


def get_receipt(db_path: str, receipt_id: int) -> dict[str, Any]:
    with database.read_tx(db_path) as conn:
        receipt = require_row(conn, "reimbursement_receipts", receipt_id, "receipt")
        return receipt_payload(conn, receipt)


# ---------------------------------------------------------------------------
# Claims (approvals)
# ---------------------------------------------------------------------------

def create_claim(
    db_path: str, allowance_id: int, receipt_id: int, amount_cents: int
) -> dict[str, Any]:
    with database.write_tx(db_path) as conn:
        allowance = require_row(
            conn, "reimbursement_allowances", allowance_id, "allowance"
        )
        receipt = require_row(conn, "reimbursement_receipts", receipt_id, "receipt")

        if allowance["released_at"] is not None:
            raise ApiError(
                409,
                "allowance_released",
                "the backing decision was withdrawn; this allowance accepts no new claims",
            )
        if receipt["author_id"] != allowance["beneficiary_id"]:
            raise ApiError(
                409,
                "receipt_owner_mismatch",
                "the receipt must be owned by the allowance beneficiary",
            )

        _assert_caps(conn, allowance, receipt, amount_cents)

        cur = conn.execute(
            "INSERT INTO reimbursement_claims (allowance_id, receipt_id, amount_cents, created_at)"
            " VALUES (?, ?, ?, ?)",
            (allowance_id, receipt_id, amount_cents, utcnow()),
        )
        claim = require_row(conn, "reimbursement_claims", cur.lastrowid, "claim")
        return claim_payload(conn, claim)


def list_claims(db_path: str) -> list[dict[str, Any]]:
    with database.read_tx(db_path) as conn:
        rows = conn.execute("SELECT * FROM reimbursement_claims ORDER BY id").fetchall()
        return [claim_payload(conn, row) for row in rows]


def get_claim(db_path: str, claim_id: int) -> dict[str, Any]:
    with database.read_tx(db_path) as conn:
        claim = require_row(conn, "reimbursement_claims", claim_id, "claim")
        return claim_payload(conn, claim)


# ---------------------------------------------------------------------------
# Payment batches
# ---------------------------------------------------------------------------

def _batch_fingerprint(items: Iterable[dict[str, Any]]) -> str:
    canonical = sorted([[int(i["claim_id"]), int(i["amount_cents"])] for i in items])
    return hashlib.sha256(
        json.dumps(canonical, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def create_batch(
    db_path: str, idempotency_key: str, items: list[dict[str, Any]]
) -> dict[str, Any]:
    idempotency_key = idempotency_key.strip()
    fingerprint = _batch_fingerprint(items)

    # Structural validation of the *content* happens before idempotency so a
    # malformed request can never be mistaken for a replay.
    seen: set[int] = set()
    for item in items:
        claim_id = int(item["claim_id"])
        if claim_id in seen:
            raise ApiError(
                400, "duplicate_claim", "each claim may appear at most once per batch"
            )
        seen.add(claim_id)

    with database.write_tx(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM payment_batches WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] == fingerprint:
                # Replay: return the original delivery list and payment
                # numbers even if eligibility or finance results have changed.
                return batch_payload(conn, existing)
            raise ApiError(
                409,
                "idempotency_conflict",
                f"request number {idempotency_key!r} was already used for different content",
            )

        prepared: list[dict[str, Any]] = []
        for item in items:
            claim_id = int(item["claim_id"])
            amount_cents = int(item["amount_cents"])
            claim = require_row(conn, "reimbursement_claims", claim_id, "claim")
            allowance = require_row(
                conn, "reimbursement_allowances", claim["allowance_id"], "allowance"
            )
            if allowance["released_at"] is not None:
                raise ApiError(
                    409,
                    "allowance_released",
                    f"claim {claim_id}: the backing decision was withdrawn and the"
                    " undelivered amount was released",
                )
            delivered = conn.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM payment_instructions"
                " WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()["s"]
            available = claim["amount_cents"] - delivered
            if amount_cents > available:
                raise ApiError(
                    409,
                    "claim_amount_exceeded",
                    f"claim {claim_id} has only {available} undelivered cents available",
                )
            receipt = require_row(
                conn, "reimbursement_receipts", claim["receipt_id"], "receipt"
            )
            prepared.append(
                {
                    "claim_id": claim_id,
                    "allowance_id": allowance["id"],
                    "amount_cents": amount_cents,
                    "payee_id": allowance["beneficiary_id"],
                    "voucher_no": receipt["voucher_no"],
                }
            )

        cur = conn.execute(
            "INSERT INTO payment_batches (idempotency_key, request_hash, created_at)"
            " VALUES (?, ?, ?)",
            (idempotency_key, fingerprint, utcnow()),
        )
        batch_id = cur.lastrowid
        for seq, item in enumerate(prepared, start=1):
            # Deterministic, globally unique per-payment number.
            payment_no = f"PMT-{batch_id:06d}-{seq:04d}"
            conn.execute(
                "INSERT INTO payment_instructions"
                " (batch_id, claim_id, allowance_id, amount_cents, payee_id,"
                "  voucher_no, payment_no, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    item["claim_id"],
                    item["allowance_id"],
                    item["amount_cents"],
                    item["payee_id"],
                    item["voucher_no"],
                    payment_no,
                    utcnow(),
                ),
            )
        batch = require_row(conn, "payment_batches", batch_id, "payment batch")
        return batch_payload(conn, batch)


def list_batches(db_path: str) -> list[dict[str, Any]]:
    with database.read_tx(db_path) as conn:
        rows = conn.execute("SELECT * FROM payment_batches ORDER BY id").fetchall()
        return [batch_detail(conn, row) for row in rows]


def get_batch(db_path: str, batch_id: int) -> dict[str, Any]:
    with database.read_tx(db_path) as conn:
        batch = require_row(conn, "payment_batches", batch_id, "payment batch")
        return batch_detail(conn, batch)


# ---------------------------------------------------------------------------
# Finance results
# ---------------------------------------------------------------------------

def record_results(
    db_path: str, batch_id: int, results: list[dict[str, Any]]
) -> dict[str, Any]:
    with database.write_tx(db_path) as conn:
        batch = require_row(conn, "payment_batches", batch_id, "payment batch")

        seen: set[str] = set()
        for result in results:
            payment_no = str(result["payment_no"])
            if payment_no in seen:
                raise ApiError(
                    400,
                    "duplicate_payment",
                    f"payment {payment_no} appears more than once in this report",
                )
            seen.add(payment_no)

        applied: list[dict[str, Any]] = []
        for result in results:
            payment_no = str(result["payment_no"])
            instruction = conn.execute(
                "SELECT * FROM payment_instructions WHERE payment_no = ?",
                (payment_no,),
            ).fetchone()
            if instruction is None or instruction["batch_id"] != batch["id"]:
                raise ApiError(
                    404,
                    "payment_not_found",
                    f"payment {payment_no} does not belong to batch {batch['id']}",
                )
            paid_cents = int(result["paid_cents"])
            if paid_cents > instruction["amount_cents"]:
                raise ApiError(
                    400,
                    "paid_exceeds_instruction",
                    f"payment {payment_no} instructed {instruction['amount_cents']} cents;"
                    f" {paid_cents} cannot be reported as paid",
                )
            existing = conn.execute(
                "SELECT * FROM payment_results WHERE instruction_id = ?",
                (instruction["id"],),
            ).fetchone()
            if existing is not None:
                if existing["paid_cents"] != paid_cents:
                    raise ApiError(
                        409,
                        "result_conflict",
                        f"payment {payment_no} already has a final result of"
                        f" {existing['paid_cents']} cents; it cannot be overwritten",
                    )
                applied.append(
                    {
                        "payment_no": payment_no,
                        "paid_cents": existing["paid_cents"],
                        "released_cents": existing["released_cents"],
                        "outcome": "unchanged",
                    }
                )
                continue
            released_cents = instruction["amount_cents"] - paid_cents
            conn.execute(
                "INSERT INTO payment_results"
                " (instruction_id, paid_cents, released_cents, note, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    instruction["id"],
                    paid_cents,
                    released_cents,
                    result.get("note"),
                    utcnow(),
                ),
            )
            applied.append(
                {
                    "payment_no": payment_no,
                    "paid_cents": paid_cents,
                    "released_cents": released_cents,
                    "outcome": "recorded",
                }
            )

        batch = require_row(conn, "payment_batches", batch_id, "payment batch")
        return {"batch": batch_detail(conn, batch), "applied": applied}


# ---------------------------------------------------------------------------
# Withdrawal hook (called inside register_conflict's transaction)
# ---------------------------------------------------------------------------

def decisions_withdrawn_hook(
    conn: sqlite3.Connection, decision_ids: list[int], now: str
) -> dict[str, Any]:
    """Release undelivered allowance money for withdrawn decisions.

    Delivered instructions are left untouched: they can still be settled by
    finance, unknown amounts keep encumbering the conference budget, and paid
    cents remain spend.
    """
    if not decision_ids:
        return {"released_allowance_ids": [], "released_cents": 0}

    placeholders = ",".join("?" for _ in decision_ids)
    allowances = conn.execute(
        f"SELECT * FROM reimbursement_allowances"
        f" WHERE decision_id IN ({placeholders}) AND released_at IS NULL",
        decision_ids,
    ).fetchall()

    released_ids: list[int] = []
    released_cents = 0
    for allowance in allowances:
        exposure = _exposure(conn, allowance_id=allowance["id"])
        released_cents += exposure["approved_unsubmitted_cents"]
        conn.execute(
            "UPDATE reimbursement_allowances SET released_at = ?, release_reason = ?"
            " WHERE id = ? AND released_at IS NULL",
            (now, "decision_withdrawn", allowance["id"]),
        )
        released_ids.append(allowance["id"])

    return {
        "released_allowance_ids": released_ids,
        "released_cents": released_cents,
    }


# ---------------------------------------------------------------------------
# Editor summary: conference, allowance and receipt encumbrance/spend
# ---------------------------------------------------------------------------

def editor_summary(db_path: str) -> dict[str, Any]:
    with database.read_tx(db_path) as conn:
        allowances = [
            allowance_payload(conn, row)
            for row in conn.execute(
                "SELECT * FROM reimbursement_allowances ORDER BY id"
            ).fetchall()
        ]
        receipts = [
            receipt_payload(conn, row)
            for row in conn.execute(
                "SELECT * FROM reimbursement_receipts ORDER BY id"
            ).fetchall()
        ]
        claims = [
            claim_payload(conn, row)
            for row in conn.execute(
                "SELECT * FROM reimbursement_claims ORDER BY id"
            ).fetchall()
        ]
        conference = _with_remaining(_exposure(conn), _conference_budget())
        return {
            "conference": {
                "budget_cents": _conference_budget(),
                **conference,
            },
            "allowance_count": len(allowances),
            "receipt_count": len(receipts),
            "claim_count": len(claims),
            "allowances": allowances,
            "receipts": receipts,
            "claims": claims,
        }


# ---------------------------------------------------------------------------
# Author queries — only the caller's own allowances/claims/payments
# ---------------------------------------------------------------------------

def author_reimbursements(db_path: str, user: sqlite3.Row) -> dict[str, Any]:
    with database.read_tx(db_path) as conn:
        allowances = conn.execute(
            "SELECT * FROM reimbursement_allowances WHERE beneficiary_id = ? ORDER BY id",
            (user["id"],),
        ).fetchall()
        allowance_views = []
        totals = {
            "amount_cents": 0,
            "approved_unsubmitted_cents": 0,
            "delivered_pending_cents": 0,
            "paid_cents": 0,
            "released_cents": 0,
        }
        for allowance in allowances:
            view = allowance_payload(conn, allowance)
            rows = conn.execute(
                "SELECT * FROM reimbursement_claims WHERE allowance_id = ? ORDER BY id",
                (allowance["id"],),
            ).fetchall()
            claim_views = [claim_payload(conn, row) for row in rows]
            payments = []
            for claim in rows:
                instr = conn.execute(
                    "SELECT i.*, r.id AS result_id, r.paid_cents, r.released_cents,"
                    " r.created_at AS result_at"
                    " FROM payment_instructions i"
                    " LEFT JOIN payment_results r ON r.instruction_id = i.id"
                    " WHERE i.claim_id = ? ORDER BY i.id",
                    (claim["id"],),
                ).fetchall()
                for p in instr:
                    payments.append(
                        {
                            "payment_no": p["payment_no"],
                            "claim_id": p["claim_id"],
                            "voucher_no": p["voucher_no"],
                            "amount_cents": p["amount_cents"],
                            "status": "settled" if p["result_id"] else "pending",
                            "paid_cents": p["paid_cents"],
                            "released_cents": p["released_cents"],
                            "settled_at": p["result_at"],
                        }
                    )
            allowance_views.append({**view, "claims": claim_views, "payments": payments})
            for key in totals:
                totals[key] += view[key]

        return {
            "author": _user_brief(user),
            "totals": totals,
            "allowances": allowance_views,
        }


def author_allowance(db_path: str, user: sqlite3.Row, allowance_id: int) -> dict[str, Any]:
    with database.read_tx(db_path) as conn:
        allowance = require_row(
            conn, "reimbursement_allowances", allowance_id, "allowance"
        )
        if allowance["beneficiary_id"] != user["id"]:
            raise ApiError(404, "not_found", "allowance not found")
        view = allowance_payload(conn, allowance)
        rows = conn.execute(
            "SELECT * FROM reimbursement_claims WHERE allowance_id = ? ORDER BY id",
            (allowance_id,),
        ).fetchall()
        return {**view, "claims": [claim_payload(conn, row) for row in rows]}
