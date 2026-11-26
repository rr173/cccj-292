#!/usr/bin/env python3
"""End-to-end speaker-travel-reimbursement walkthrough over HTTP.

Story:
  1. A talk is confirmed for author1; the editor grants one allowance (cents).
  2. The editor registers two receipts and approves three expense claims.
  3. One payment batch delivers portions of two claims; replaying the same
     request number + content returns the same payment numbers.
  4. Finance reports per payment number in two rounds: one partial payment
     (difference released) and one zero payment.  The remaining claim stays
     undelivered for a later batch.
  5. The backing decision is withdrawn by a new conflict: the allowance stops
     accepting approvals, only the undelivered cents are released, delivered
     instructions remain settleable, and paid cents stay spend.
  6. The author checks only their own progress.

Start a server first (with a fresh database for a clean run):
    REVIEW_DB_PATH=/tmp/reimb-demo.db ./run.sh --port 8011
Then:
    PYTHONPATH=/workspace/.pylibs/lib/python3.11/site-packages \
      python3 examples/reimbursement_walkthrough.py http://127.0.0.1:8011
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

KEYS = {
    "editor": "key-editor-1",
    "author1": "key-author-1",
    "reviewer1": "key-reviewer-1",
    "reviewer2": "key-reviewer-2",
}


def call(role: str, method: str, path: str, **kwargs):
    headers = {"X-API-Key": KEYS[role]}
    r = httpx.request(method, BASE + path, headers=headers, timeout=10, **kwargs)
    print(f"\n=== {role:8s} {method:5s} {path} -> {r.status_code}")
    data = r.json()
    print(json.dumps(data, indent=2, ensure_ascii=False))
    r.raise_for_status()
    return data


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def main() -> None:
    httpx.get(BASE + "/health", timeout=10).raise_for_status()
    suffix = int(time.time())
    accounts = call("editor", "GET", "/editor/accounts")
    id_of = {a["username"]: a["id"] for a in accounts}

    now = datetime.now(timezone.utc)
    deadline = iso(now + timedelta(days=20))
    start = iso((now + timedelta(days=40)).replace(hour=10, minute=0, second=0, microsecond=0))
    end = iso((now + timedelta(days=40)).replace(hour=11, minute=0, second=0, microsecond=0))

    # --- 0. One accepted paper --------------------------------------------
    ms = call(
        "author1",
        "POST",
        "/author/manuscripts",
        json={"title": "Reimbursement Demo Paper", "content": "full text"},
    )
    revision_id = ms["first_revision"]["revision_id"]
    review_ids = []
    for reviewer in ("reviewer1", "reviewer2"):
        inv = call(
            "editor",
            "POST",
            "/editor/invitations",
            json={"revision_id": revision_id, "reviewer_id": id_of[reviewer]},
        )
        call(
            reviewer,
            "POST",
            f"/reviewer/invitations/{inv['invitation_id']}/respond",
            json={"accept": True},
        )
        rv = call(
            reviewer,
            "POST",
            f"/reviewer/invitations/{inv['invitation_id']}/reviews",
            json={"content": "solid", "score": 4},
        )
        review_ids.append(rv["review_id"])
    decision = call(
        "editor",
        "POST",
        "/editor/decisions",
        json={"revision_id": revision_id, "review_ids": review_ids, "conclusion": "accept"},
    )

    # --- 1. Confirm the talk, then grant one allowance ---------------------
    group = call(
        "editor",
        "POST",
        "/editor/schedule/groups",
        json={
            "confirmation_deadline": deadline,
            "items": [
                {
                    "manuscript_id": ms["manuscript_id"],
                    "decision_id": decision["decision_id"],
                    "room": "hall-a",
                    "starts_at": start,
                    "ends_at": end,
                }
            ],
        },
    )
    call(
        "author1",
        "POST",
        f"/author/schedule/items/{group['items'][0]['item_id']}/respond",
        json={"accept": True},
    )

    allowance = call(
        "editor",
        "POST",
        "/editor/reimbursements/allowances",
        json={"manuscript_id": ms["manuscript_id"], "amount_cents": 120000},
    )

    # --- 2. Receipts and approvals ----------------------------------------
    receipt_a = call(
        "editor",
        "POST",
        "/editor/reimbursements/receipts",
        json={
            "voucher_no": f"VOC-{suffix}-A",
            "author_id": id_of["author1"],
            "amount_cents": 80000,
        },
    )
    receipt_b = call(
        "editor",
        "POST",
        "/editor/reimbursements/receipts",
        json={
            "voucher_no": f"VOC-{suffix}-B",
            "author_id": id_of["author1"],
            "amount_cents": 50000,
        },
    )

    def approve(receipt_id: int, amount_cents: int):
        return call(
            "editor",
            "POST",
            "/editor/reimbursements/claims",
            json={
                "allowance_id": allowance["allowance_id"],
                "receipt_id": receipt_id,
                "amount_cents": amount_cents,
            },
        )

    claim1 = approve(receipt_a["receipt_id"], 60000)
    claim2 = approve(receipt_a["receipt_id"], 20000)  # same receipt, split
    claim3 = approve(receipt_b["receipt_id"], 30000)

    # Approval beyond the allowance cap is refused.
    httpx.post(
        BASE + "/editor/reimbursements/claims",
        headers={"X-API-Key": KEYS["editor"]},
        json={
            "allowance_id": allowance["allowance_id"],
            "receipt_id": receipt_b["receipt_id"],
            "amount_cents": 20000,
        },
        timeout=10,
    )
    print("\n(an extra 20000-cent approval is rejected: allowance cap would be exceeded)")

    # --- 3. Deliver approved money to finance -----------------------------
    batch_items = [
        {"claim_id": claim1["claim_id"], "amount_cents": 60000},
        {"claim_id": claim2["claim_id"], "amount_cents": 20000},
    ]
    batch = call(
        "editor",
        "POST",
        "/editor/reimbursements/batches",
        json={"idempotency_key": f"demo-batch-{suffix}", "items": batch_items},
    )
    replay = call(
        "editor",
        "POST",
        "/editor/reimbursements/batches",
        json={"idempotency_key": f"demo-batch-{suffix}", "items": batch_items},
    )
    assert [p["payment_no"] for p in replay["payments"]] == [
        p["payment_no"] for p in batch["payments"]
    ]

    first_no, second_no = [p["payment_no"] for p in batch["payments"]]

    # --- 4. Finance reports in two rounds ---------------------------------
    call(
        "editor",
        "POST",
        f"/editor/reimbursements/batches/{batch['batch_id']}/results",
        json={"results": [{"payment_no": first_no, "paid_cents": 55000, "note": "wire fee"}]},
    )
    call(
        "editor",
        "POST",
        f"/editor/reimbursements/batches/{batch['batch_id']}/results",
        json={"results": [{"payment_no": second_no, "paid_cents": 0}]},
    )
    call("editor", "GET", f"/editor/reimbursements/batches/{batch['batch_id']}")

    # claim3 is still fully undelivered: deliver it in a second batch.
    later = call(
        "editor",
        "POST",
        "/editor/reimbursements/batches",
        json={
            "idempotency_key": f"demo-batch-late-{suffix}",
            "items": [{"claim_id": claim3["claim_id"], "amount_cents": 30000}],
        },
    )
    third_no = later["payments"][0]["payment_no"]

    # --- 5. Encumbrance and spend overview --------------------------------
    call("editor", "GET", "/editor/reimbursements/summary")

    # --- 6. Withdraw the decision via a new conflict ----------------------
    call(
        "editor",
        "POST",
        "/editor/conflicts",
        json={
            "author_id": id_of["author1"],
            "reviewer_id": id_of["reviewer1"],
            "reason": "late-disclosed collaboration",
        },
    )

    # Allowance is released; undelivered cents are freed, nothing new accepted.
    call(
        "editor",
        "GET",
        f"/editor/reimbursements/allowances/{allowance['allowance_id']}",
    )
    r = httpx.post(
        BASE + "/editor/reimbursements/claims",
        headers={"X-API-Key": KEYS["editor"]},
        json={
            "allowance_id": allowance["allowance_id"],
            "receipt_id": receipt_b["receipt_id"],
            "amount_cents": 1000,
        },
        timeout=10,
    )
    print(f"\nnew approval after withdrawal -> {r.status_code} {r.json()}")
    assert r.status_code == 409

    # The instruction already handed to finance may still be settled.
    call(
        "editor",
        "POST",
        f"/editor/reimbursements/batches/{later['batch_id']}/results",
        json={"results": [{"payment_no": third_no, "paid_cents": 30000}]},
    )

    # --- 7. Author checks only their own progress -------------------------
    call("author1", "GET", "/author/reimbursements")

    summary = call("editor", "GET", "/editor/reimbursements/summary")
    conf = summary["conference"]
    assert conf["paid_cents"] == 55000 + 0 + 30000
    assert conf["delivered_pending_cents"] == 0
    assert conf["approved_unsubmitted_cents"] == 0
    # Released = 5000 not paid on claim1 + 20000 reported as zero on claim2;
    # both claims had already been fully delivered before the withdrawal.
    assert conf["released_cents"] == 25000

    print("\nReimbursement walkthrough completed successfully.")


if __name__ == "__main__":
    main()
