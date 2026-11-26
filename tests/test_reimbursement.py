"""Speaker travel reimbursement: allowances, receipts, approval caps, payment
batches, idempotency, finance results and decision withdrawal.

All money is integer cents.  Helpers build one complete talk (confirmed
session + allowance) so each test starts from a realistic state.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from conftest import (
    accept_and_review,
    auth,
    author_user_id,
    create_manuscript,
    invite,
    publish_two_review_decision,
    reviewer_user_id,
)
from review_system import config as app_config
from review_system import db as database
from review_system.main import create_app

DEADLINE = "2030-05-01T00:00:00+00:00"
START = "2030-06-03T10:00:00+00:00"
END = "2030-06-03T11:00:00+00:00"
START_2 = "2030-06-03T14:00:00+00:00"
END_2 = "2030-06-03T15:00:00+00:00"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _paper(client, author="author1", reviewers=("reviewer1", "reviewer2"), title="P"):
    m = create_manuscript(client, author=author, title=title)
    revision_id = m["first_revision"]["revision_id"]
    review_ids = []
    for reviewer in reviewers:
        inv = invite(client, revision_id, reviewer)
        out = accept_and_review(client, inv["invitation_id"], reviewer, "review", 4)
        review_ids.append(out["review_id"])
    decision = publish_two_review_decision(client, revision_id, review_ids)
    return {
        "manuscript_id": m["manuscript_id"],
        "decision_id": decision["decision_id"],
    }


def _confirm_session(client, paper, author="author1", times=(START, END), room="hall-a"):
    start, end = times
    r = client.post(
        "/editor/schedule/groups",
        json={
            "confirmation_deadline": DEADLINE,
            "items": [
                {
                    "manuscript_id": paper["manuscript_id"],
                    "decision_id": paper["decision_id"],
                    "room": room,
                    "starts_at": start,
                    "ends_at": end,
                }
            ],
        },
        headers=auth("editor"),
    )
    assert r.status_code == 201, r.text
    item_id = r.json()["items"][0]["item_id"]
    r = client.post(
        f"/author/schedule/items/{item_id}/respond",
        json={"accept": True},
        headers=auth(author),
    )
    assert r.status_code == 200, r.text
    return r.json()


def _allowance(client, paper, amount_cents=10000, decision_id=None):
    body = {"manuscript_id": paper["manuscript_id"], "amount_cents": amount_cents}
    if decision_id is not None:
        body["decision_id"] = decision_id
    return client.post(
        "/editor/reimbursements/allowances", json=body, headers=auth("editor")
    )


def _receipt(client, voucher_no, author, amount_cents):
    return client.post(
        "/editor/reimbursements/receipts",
        json={
            "voucher_no": voucher_no,
            "author_id": author_user_id(client, author),
            "amount_cents": amount_cents,
        },
        headers=auth("editor"),
    )


def _claim(client, allowance_id, receipt_id, amount_cents):
    return client.post(
        "/editor/reimbursements/claims",
        json={
            "allowance_id": allowance_id,
            "receipt_id": receipt_id,
            "amount_cents": amount_cents,
        },
        headers=auth("editor"),
    )


def _batch(client, key, items):
    return client.post(
        "/editor/reimbursements/batches",
        json={"idempotency_key": key, "items": items},
        headers=auth("editor"),
    )


def _results(client, batch_id, results):
    return client.post(
        f"/editor/reimbursements/batches/{batch_id}/results",
        json={"results": results},
        headers=auth("editor"),
    )


def _setup_allowance(client, amount_cents=10000, receipt_cents=None, author="author1"):
    """One confirmed talk + allowance + one receipt for its author."""
    paper = _paper(client, author=author)
    _confirm_session(client, paper, author=author)
    allowance = _allowance(client, paper, amount_cents).json()
    receipt = _receipt(
        client, f"V-{paper['manuscript_id']}", author, receipt_cents or amount_cents
    ).json()
    return paper, allowance, receipt


# ---------------------------------------------------------------------------
# Allowance eligibility
# ---------------------------------------------------------------------------

def test_allowance_requires_confirmed_session(client):
    paper = _paper(client)  # published decision, but never scheduled
    r = _allowance(client, paper)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_confirmed_session"


def test_allowance_beneficiary_is_manuscript_author(client):
    _, allowance, _ = _setup_allowance(client, author="author2")
    assert allowance["beneficiary"]["id"] == author_user_id(client, "author2")
    assert allowance["amount_cents"] == 10000
    assert allowance["status"] == "active"
    assert allowance["remaining_cents"] == 10000


def test_one_allowance_per_manuscript_and_decision(client):
    _paper_, allowance, _ = _setup_allowance(client)
    paper = {"manuscript_id": allowance["manuscript_id"]}
    r = _allowance(client, paper)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "allowance_exists"


def test_confirmed_reschedule_keeps_allowance_usable(client):
    """A confirmed reschedule only moves time and reuses the same decision."""
    paper, allowance, receipt = _setup_allowance(client, amount_cents=5000)
    # Move the same manuscript; decision_id omitted -> inherited.
    r = client.post(
        "/editor/schedule/groups",
        json={
            "confirmation_deadline": DEADLINE,
            "items": [
                {
                    "manuscript_id": paper["manuscript_id"],
                    "room": "hall-b",
                    "starts_at": START_2,
                    "ends_at": END_2,
                }
            ],
        },
        headers=auth("editor"),
    )
    assert r.status_code == 201, r.text
    item = r.json()["items"][0]
    assert item["decision_id"] == paper["decision_id"]
    assert (
        client.post(
            f"/author/schedule/items/{item['item_id']}/respond",
            json={"accept": True},
            headers=auth("author1"),
        ).status_code
        == 200
    )

    # Allowance untouched and still spendable.
    view = client.get(
        f"/editor/reimbursements/allowances/{allowance['allowance_id']}",
        headers=auth("editor"),
    ).json()
    assert view["amount_cents"] == 5000
    assert view["committed_cents"] == 0
    assert view["remaining_cents"] == 5000
    claim = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 5000)
    assert claim.status_code == 201, claim.text


def test_allowance_must_use_session_decision(client):
    paper = _paper(client)
    _confirm_session(client, paper)
    other = _paper(client, title="other")
    r = _allowance(client, paper, decision_id=other["decision_id"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "decision_mismatch"


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

def test_receipt_voucher_unique_and_positive(client):
    _, allowance, receipt = _setup_allowance(client)
    duplicate = _receipt(client, receipt["voucher_no"], "author1", 1000)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "receipt_exists"

    zero = client.post(
        "/editor/reimbursements/receipts",
        json={
            "voucher_no": "ZERO",
            "author_id": author_user_id(client, "author1"),
            "amount_cents": 0,
        },
        headers=auth("editor"),
    )
    assert zero.status_code == 422


def test_claim_requires_same_author_receipt(client):
    paper, allowance, _ = _setup_allowance(client, author="author1")
    other_receipt = _receipt(client, "V-OTHER", "author2", 5000).json()
    r = _claim(client, allowance["allowance_id"], other_receipt["receipt_id"], 1000)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "receipt_owner_mismatch"


# ---------------------------------------------------------------------------
# The three independent caps
# ---------------------------------------------------------------------------

def test_allowance_cap(client):
    _, allowance, receipt = _setup_allowance(client, amount_cents=3000, receipt_cents=3000)
    assert _claim(client, allowance["allowance_id"], receipt["receipt_id"], 2000).status_code == 201
    r = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 1001)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "allowance_exceeded"
    assert _claim(client, allowance["allowance_id"], receipt["receipt_id"], 1000).status_code == 201


def test_receipt_cap_across_multiple_allowances(client):
    """One receipt may be split across several allowances of the same author."""
    paper = _paper(client)
    _confirm_session(client, paper)
    a1 = _allowance(client, paper, 4000).json()
    paper2 = _paper(client, title="second")
    _confirm_session(client, paper2, times=(START_2, END_2))
    a2 = _allowance(client, paper2, 4000).json()

    receipt = _receipt(client, "V-SPLIT", "author1", 6000).json()
    assert _claim(client, a1["allowance_id"], receipt["receipt_id"], 3000).status_code == 201
    assert _claim(client, a2["allowance_id"], receipt["receipt_id"], 3000).status_code == 201
    r = _claim(client, a2["allowance_id"], receipt["receipt_id"], 1)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "receipt_exceeded"


def test_receipt_can_be_split_across_several_claims_same_allowance(client):
    _, allowance, receipt = _setup_allowance(client, amount_cents=5000, receipt_cents=5000)
    assert _claim(client, allowance["allowance_id"], receipt["receipt_id"], 1000).status_code == 201
    assert _claim(client, allowance["allowance_id"], receipt["receipt_id"], 4000).status_code == 201


def test_withdrawal_does_not_recycle_receipt_face_value(client):
    """Released allowance money must not re-spend the same physical receipt."""
    paper, allowance, receipt = _setup_allowance(client, amount_cents=5000, receipt_cents=5000)
    assert _claim(client, allowance["allowance_id"], receipt["receipt_id"], 5000).status_code == 201
    _withdraw(client, paper, "author1", "reviewer1")

    # New talk/decision, new allowance, same author — but the bill is used up.
    # (Different reviewers avoid the conflict registered for reviewer1.)
    paper2 = _paper(client, title="follow-up", reviewers=("reviewer2", "reviewer3"))
    _confirm_session(client, paper2, times=(START_2, END_2))
    a2 = _allowance(client, paper2, 5000).json()
    r = _claim(client, a2["allowance_id"], receipt["receipt_id"], 1000)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "receipt_exceeded"

    view = client.get(
        f"/editor/reimbursements/receipts/{receipt['receipt_id']}", headers=auth("editor")
    ).json()
    assert view["approved_total_cents"] == 5000
    assert view["remaining_cents"] == 0


def test_conference_budget_cap(client, monkeypatch):
    monkeypatch.setattr(app_config, "REIMBURSEMENT_BUDGET_CENTS", 5000)
    _, allowance, receipt = _setup_allowance(client, amount_cents=4000, receipt_cents=8000)
    paper2 = _paper(client, title="p2")
    _confirm_session(client, paper2, times=(START_2, END_2))
    a2 = _allowance(client, paper2, 4000).json()
    receipt2 = _receipt(client, "V-BUDGET-2", "author1", 4000).json()

    assert _claim(client, allowance["allowance_id"], receipt["receipt_id"], 3000).status_code == 201
    r = _claim(client, a2["allowance_id"], receipt2["receipt_id"], 2500)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "budget_exceeded"
    assert _claim(client, a2["allowance_id"], receipt2["receipt_id"], 2000).status_code == 201


# ---------------------------------------------------------------------------
# Batches and idempotency
# ---------------------------------------------------------------------------

def test_partial_claim_delivery_and_payment_numbers(client):
    _, allowance, receipt = _setup_allowance(client, amount_cents=10000, receipt_cents=10000)
    claim = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 8000).json()

    b1 = _batch(client, "req-1", [{"claim_id": claim["claim_id"], "amount_cents": 3000}])
    assert b1.status_code == 201, b1.text
    body = b1.json()
    assert body["total_cents"] == 3000
    assert len(body["payments"]) == 1
    payment = body["payments"][0]
    assert payment["payee"]["username"] == "author1"
    assert payment["voucher_no"] == receipt["voucher_no"]
    assert payment["payment_no"].startswith("PMT-")

    # Only payment info is delivered — no decision/review/large payload.
    assert "decision_id" not in body["payments"][0]

    # The claim still has 5000 undelivered.
    view = client.get(
        f"/editor/reimbursements/claims/{claim['claim_id']}", headers=auth("editor")
    ).json()
    assert view["delivered_cents"] == 3000
    assert view["remaining_unsubmitted_cents"] == 5000
    assert view["delivered_pending_cents"] == 3000

    # A second, different batch can deliver the rest.
    b2 = _batch(client, "req-2", [{"claim_id": claim["claim_id"], "amount_cents": 5000}])
    assert b2.status_code == 201
    assert b2.json()["payments"][0]["payment_no"] != payment["payment_no"]


def test_claim_over_delivery_rejected(client):
    _, allowance, receipt = _setup_allowance(client, amount_cents=5000, receipt_cents=5000)
    claim = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 5000).json()
    assert _batch(client, "x1", [{"claim_id": claim["claim_id"], "amount_cents": 3000}]).status_code == 201
    r = _batch(client, "x2", [{"claim_id": claim["claim_id"], "amount_cents": 3000}])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "claim_amount_exceeded"


def test_batch_is_all_or_nothing(client):
    _, allowance, receipt = _setup_allowance(client, amount_cents=5000, receipt_cents=5000)
    good = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 1000).json()
    bad = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 4000).json()
    # Ask for more than the second claim holds -> nothing is delivered.
    r = _batch(
        client,
        "all-or-nothing",
        [
            {"claim_id": good["claim_id"], "amount_cents": 1000},
            {"claim_id": bad["claim_id"], "amount_cents": 4001},
        ],
    )
    assert r.status_code == 409
    assert client.get("/editor/reimbursements/batches", headers=auth("editor")).json() == []
    view = client.get(
        f"/editor/reimbursements/claims/{good['claim_id']}", headers=auth("editor")
    ).json()
    assert view["delivered_cents"] == 0


def test_batch_idempotent_replay_returns_original_even_after_changes(client):
    _, allowance, receipt = _setup_allowance(client, amount_cents=5000, receipt_cents=5000)
    claim = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 5000).json()
    items = [{"claim_id": claim["claim_id"], "amount_cents": 5000}]
    first = _batch(client, "same-key", items).json()

    # Deliver + settle the payment, so "eligibility/results" have changed.
    results = _results(
        client, first["batch_id"], [{"payment_no": first["payments"][0]["payment_no"], "paid_cents": 5000}]
    )
    assert results.status_code == 200

    replay = _batch(client, "same-key", items)
    assert replay.status_code == 201
    assert replay.json() == first  # identical delivery list + payment numbers


def test_batch_same_key_different_content_rejected(client):
    _, allowance, receipt = _setup_allowance(client, amount_cents=5000, receipt_cents=5000)
    claim = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 5000).json()
    assert _batch(client, "k", [{"claim_id": claim["claim_id"], "amount_cents": 1000}]).status_code == 201
    r = _batch(client, "k", [{"claim_id": claim["claim_id"], "amount_cents": 2000}])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"
    # Still exactly one batch delivered.
    assert len(client.get("/editor/reimbursements/batches", headers=auth("editor")).json()) == 1


def test_replay_survives_decision_withdrawal(client):
    """A retried batch request returns the original list even if the backing
    decision was withdrawn in the meantime and eligibility has changed."""
    paper, allowance, receipt = _setup_allowance(client, amount_cents=5000, receipt_cents=5000)
    claim = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 5000).json()
    items = [{"claim_id": claim["claim_id"], "amount_cents": 2000}]
    first = _batch(client, "withdraw-replay", items).json()

    _withdraw(client, paper, "author1", "reviewer1")

    replay = _batch(client, "withdraw-replay", items)
    assert replay.status_code == 201
    assert replay.json() == first


def test_batch_does_not_allow_released_claim(client):
    paper, allowance, receipt = _setup_allowance(client, amount_cents=5000, receipt_cents=5000)
    claim = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 5000).json()
    # Withdraw the decision via a conflict.
    _withdraw(client, paper, "author1", "reviewer1")
    r = _batch(client, "after-withdraw", [{"claim_id": claim["claim_id"], "amount_cents": 1000}])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "allowance_released"


# ---------------------------------------------------------------------------
# Finance results
# ---------------------------------------------------------------------------

def test_partial_and_zero_results_release_difference(client):
    _, allowance, receipt = _setup_allowance(client, amount_cents=9000, receipt_cents=9000)
    claim = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 9000).json()
    batch = _batch(client, "pay", [{"claim_id": claim["claim_id"], "amount_cents": 7000}]).json()
    payment_no = batch["payments"][0]["payment_no"]

    # No result yet: fully pending.
    summary = client.get("/editor/reimbursements/summary", headers=auth("editor")).json()
    assert summary["conference"]["delivered_pending_cents"] == 7000
    assert summary["conference"]["paid_cents"] == 0

    r = _results(client, batch["batch_id"], [{"payment_no": payment_no, "paid_cents": 4000}])
    assert r.status_code == 200
    assert r.json()["applied"][0] == {
        "payment_no": payment_no,
        "paid_cents": 4000,
        "released_cents": 3000,
        "outcome": "recorded",
    }

    claim_view = client.get(
        f"/editor/reimbursements/claims/{claim['claim_id']}", headers=auth("editor")
    ).json()
    assert claim_view["paid_cents"] == 4000
    assert claim_view["released_payment_difference_cents"] == 3000
    # The unpaid difference does NOT return to the undelivered amount.
    assert claim_view["remaining_unsubmitted_cents"] == 2000

    summary = client.get("/editor/reimbursements/summary", headers=auth("editor")).json()
    assert summary["conference"]["paid_cents"] == 4000
    assert summary["conference"]["delivered_pending_cents"] == 0
    assert summary["conference"]["approved_unsubmitted_cents"] == 2000
    # Only the 2000 still undelivered is spendable (cap accounting excludes released difference).
    assert summary["allowances"][0]["remaining_cents"] == 9000 - 4000 - 2000


def test_zero_result_and_resend_is_noop(client):
    _, allowance, receipt = _setup_allowance(client, amount_cents=5000, receipt_cents=5000)
    claim = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 5000).json()
    batch = _batch(client, "z", [{"claim_id": claim["claim_id"], "amount_cents": 5000}]).json()
    pn = batch["payments"][0]["payment_no"]

    assert _results(client, batch["batch_id"], [{"payment_no": pn, "paid_cents": 0}]).status_code == 200
    again = _results(client, batch["batch_id"], [{"payment_no": pn, "paid_cents": 0}])
    assert again.status_code == 200
    assert again.json()["applied"][0]["outcome"] == "unchanged"
    # Different amount cannot overwrite a confirmed result.
    bad = _results(client, batch["batch_id"], [{"payment_no": pn, "paid_cents": 1000}])
    assert bad.status_code == 409
    assert bad.json()["error"]["code"] == "result_conflict"


def test_result_batching_and_validation(client):
    _, allowance, receipt = _setup_allowance(client, amount_cents=8000, receipt_cents=8000)
    c1 = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 4000).json()
    c2 = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 4000).json()
    batch = _batch(
        client,
        "multi",
        [
            {"claim_id": c1["claim_id"], "amount_cents": 4000},
            {"claim_id": c2["claim_id"], "amount_cents": 4000},
        ],
    ).json()
    pns = [p["payment_no"] for p in batch["payments"]]

    # Over-instruction amount rejected.
    over = _results(client, batch["batch_id"], [{"payment_no": pns[0], "paid_cents": 4001}])
    assert over.status_code == 400
    assert over.json()["error"]["code"] == "paid_exceeds_instruction"

    # First finance round reports only one payment; the other stays pending.
    r1 = _results(client, batch["batch_id"], [{"payment_no": pns[0], "paid_cents": 4000}])
    assert r1.status_code == 200
    detail = client.get(
        f"/editor/reimbursements/batches/{batch['batch_id']}", headers=auth("editor")
    ).json()
    assert detail["progress"] == {
        "total": 2,
        "settled": 1,
        "pending": 1,
        "all_settled": False,
    }
    assert detail["totals"]["delivered_pending_cents"] == 4000

    # Second round settles the rest.
    r2 = _results(client, batch["batch_id"], [{"payment_no": pns[1], "paid_cents": 3500}])
    assert r2.status_code == 200
    detail = client.get(
        f"/editor/reimbursements/batches/{batch['batch_id']}", headers=auth("editor")
    ).json()
    assert detail["progress"]["all_settled"] is True
    assert detail["totals"]["paid_cents"] == 7500
    assert detail["totals"]["released_cents"] == 500


def test_result_unknown_payment_rejected(client):
    _, allowance, receipt = _setup_allowance(client, amount_cents=1000, receipt_cents=1000)
    claim = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 1000).json()
    batch = _batch(client, "u", [{"claim_id": claim["claim_id"], "amount_cents": 1000}]).json()
    r = _results(client, batch["batch_id"], [{"payment_no": "PMT-NOPE", "paid_cents": 1000}])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "payment_not_found"


# ---------------------------------------------------------------------------
# Withdrawal semantics
# ---------------------------------------------------------------------------

def _withdraw(client, paper, author, reviewer, reason="late conflict"):
    return client.post(
        "/editor/conflicts",
        json={
            "author_id": author_user_id(client, author),
            "reviewer_id": reviewer_user_id(client, reviewer),
            "reason": reason,
        },
        headers=auth("editor"),
    )


def test_withdrawal_releases_only_undelivered_and_blocks_new_claims(client):
    paper, allowance, receipt = _setup_allowance(client, amount_cents=10000, receipt_cents=10000)
    # Three claims: 2000 undelivered, 3000 delivered+pending, 3000 delivered+paid.
    c_undelivered = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 2000).json()
    c_pending = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 3000).json()
    c_paid = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 3000).json()

    pending_batch = _batch(
        client, "pending", [{"claim_id": c_pending["claim_id"], "amount_cents": 3000}]
    ).json()
    paid_batch = _batch(
        client, "paid", [{"claim_id": c_paid["claim_id"], "amount_cents": 3000}]
    ).json()
    _results(
        client,
        paid_batch["batch_id"],
        [{"payment_no": paid_batch["payments"][0]["payment_no"], "paid_cents": 3000}],
    )

    r = _withdraw(client, paper, "author1", "reviewer1")
    assert r.status_code == 201, r.text
    effect = r.json()
    assert allowance["allowance_id"] in effect["released_allowance_ids"]
    # The 2000 never-delivered approved cents are released.
    assert effect["released_cents"] == 2000

    # The allowance is now released and rejects new claims.
    view = client.get(
        f"/editor/reimbursements/allowances/{allowance['allowance_id']}",
        headers=auth("editor"),
    ).json()
    assert view["status"] == "released"
    assert view["approved_unsubmitted_cents"] == 0
    assert view["released_unsubmitted_cents"] == 2000
    # Pending instruction still fully encumbers; paid remains spend.
    assert view["delivered_pending_cents"] == 3000
    assert view["paid_cents"] == 3000

    blocked = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 100)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "allowance_released"

    # The withheld pending instruction can still be settled by finance.
    rr = _results(
        client,
        pending_batch["batch_id"],
        [{"payment_no": pending_batch["payments"][0]["payment_no"], "paid_cents": 2500}],
    )
    assert rr.status_code == 200
    assert rr.json()["applied"][0]["paid_cents"] == 2500

    summary = client.get("/editor/reimbursements/summary", headers=auth("editor")).json()
    # Paid total unchanged by withdrawal; pending was 3000 then settled to 2500.
    assert summary["conference"]["paid_cents"] == 3000 + 2500
    assert summary["conference"]["delivered_pending_cents"] == 0
    assert summary["conference"]["approved_unsubmitted_cents"] == 0
    # Released = 2000 undelivered + 500 unpaid difference.
    assert summary["conference"]["released_cents"] == 2500


def test_withdrawal_does_not_leak_budget_into_new_directive(client, monkeypatch):
    """A new decision cannot restore the old allowance; budget accounting stays sane."""
    monkeypatch.setattr(app_config, "REIMBURSEMENT_BUDGET_CENTS", 5000)
    paper, allowance, receipt = _setup_allowance(client, amount_cents=3000, receipt_cents=3000)
    claim = _claim(client, allowance["allowance_id"], receipt["receipt_id"], 3000).json()
    batch = _batch(client, "old", [{"claim_id": claim["claim_id"], "amount_cents": 3000}]).json()
    _results(
        client,
        batch["batch_id"],
        [{"payment_no": batch["payments"][0]["payment_no"], "paid_cents": 1000}],
    )

    _withdraw(client, paper, "author1", "reviewer1")

    # A fresh allowance can be granted if a new talk/decision appears, but the
    # old decision's allowance never revives.
    old = client.get(
        f"/editor/reimbursements/allowances/{allowance['allowance_id']}",
        headers=auth("editor"),
    ).json()
    assert old["status"] == "released"
    assert old["paid_cents"] == 1000
    # 1000 paid + 0 pending + 0 undelivered = 1000 of the 5000 budget committed.
    summary = client.get("/editor/reimbursements/summary", headers=auth("editor")).json()
    assert summary["conference"]["committed_cents"] == 1000
    assert summary["conference"]["remaining_cents"] == 4000


# ---------------------------------------------------------------------------
# Visibility / roles
# ---------------------------------------------------------------------------

def test_author_only_sees_own_progress(client):
    _, a1, receipt = _setup_allowance(client, amount_cents=4000, receipt_cents=4000, author="author1")
    claim = _claim(client, a1["allowance_id"], receipt["receipt_id"], 4000).json()
    batch = _batch(client, "auth", [{"claim_id": claim["claim_id"], "amount_cents": 2000}]).json()

    mine = client.get("/author/reimbursements", headers=auth("author1"))
    assert mine.status_code == 200
    body = mine.json()
    assert body["totals"]["amount_cents"] == 4000
    assert body["totals"]["delivered_pending_cents"] == 2000
    assert len(body["allowances"]) == 1
    assert body["allowances"][0]["payments"][0]["payment_no"] == batch["payments"][0]["payment_no"]

    # Another author sees nothing and cannot read by id.
    other = client.get("/author/reimbursements", headers=auth("author2")).json()
    assert other["allowances"] == []
    denied = client.get(
        f"/author/reimbursements/allowances/{a1['allowance_id']}", headers=auth("author2")
    )
    assert denied.status_code == 404

    # Author cannot call editor reimbursement endpoints.
    assert (
        client.get("/editor/reimbursements/summary", headers=auth("author1")).status_code
        == 403
    )


def test_reviewer_cannot_access_reimbursements(client):
    assert (
        client.get("/author/reimbursements", headers=auth("reviewer1")).status_code == 403
    )
    assert (
        client.get("/editor/reimbursements/summary", headers=auth("reviewer1")).status_code
        == 403
    )


def test_author_progress_survives_withdrawal(client):
    paper, a1, receipt = _setup_allowance(client, amount_cents=3000, receipt_cents=3000)
    claim = _claim(client, a1["allowance_id"], receipt["receipt_id"], 3000).json()
    batch = _batch(client, "aw", [{"claim_id": claim["claim_id"], "amount_cents": 2000}]).json()
    _results(
        client,
        batch["batch_id"],
        [{"payment_no": batch["payments"][0]["payment_no"], "paid_cents": 2000}],
    )
    _withdraw(client, paper, "author1", "reviewer1")
    body = client.get("/author/reimbursements", headers=auth("author1")).json()
    allowance_view = body["allowances"][0]
    assert allowance_view["status"] == "released"
    assert allowance_view["paid_cents"] == 2000
    assert allowance_view["payments"][0]["status"] == "settled"


# ---------------------------------------------------------------------------
# Concurrency invariants
# ---------------------------------------------------------------------------

def _fresh_client():
    return TestClient(create_app(app_config.DB_PATH))


def _concurrent(fn_a, fn_b):
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def wrapped(name, fn):
        c = _fresh_client()
        barrier.wait()
        outcomes[name] = fn(c)

    t1 = threading.Thread(target=wrapped, args=("a", fn_a))
    t2 = threading.Thread(target=wrapped, args=("b", fn_b))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    return outcomes


def test_concurrent_claims_never_exceed_allowance(tmp_path):
    """20 rounds of two approvals against one 5000-cent allowance: exactly one
    of two 3000-cent claims can win; never both."""
    for i in range(20):
        db = str(tmp_path / f"claim_race_{i}.db")
        app_config.DB_PATH = db
        database.init_db(db)
        setup = _fresh_client()
        _, allowance, receipt = _setup_allowance(
            setup, amount_cents=5000, receipt_cents=5000
        )
        aid, rid = allowance["allowance_id"], receipt["receipt_id"]

        def claim_a(c, aid=aid, rid=rid):
            return c.post(
                "/editor/reimbursements/claims",
                json={"allowance_id": aid, "receipt_id": rid, "amount_cents": 3000},
                headers=auth("editor"),
            ).status_code

        def claim_b(c, aid=aid, rid=rid):
            return c.post(
                "/editor/reimbursements/claims",
                json={"allowance_id": aid, "receipt_id": rid, "amount_cents": 3000},
                headers=auth("editor"),
            ).status_code

        out = _concurrent(claim_a, claim_b)
        assert sorted([out["a"], out["b"]]) == [201, 409], out

        check = _fresh_client()
        view = check.get(
            f"/editor/reimbursements/allowances/{aid}", headers=auth("editor")
        ).json()
        assert view["committed_cents"] == 3000


def test_concurrent_batches_and_results_keep_caps(tmp_path):
    """Two concurrent delivery batches that together exceed the claim may not
    both succeed, and a concurrent settlement cannot make the result wrong."""
    for i in range(12):
        db = str(tmp_path / f"batch_race_{i}.db")
        app_config.DB_PATH = db
        database.init_db(db)
        setup = _fresh_client()
        _, allowance, receipt = _setup_allowance(
            setup, amount_cents=4000, receipt_cents=4000
        )
        claim = _claim(
            setup, allowance["allowance_id"], receipt["receipt_id"], 4000
        ).json()
        cid = claim["claim_id"]

        def batch_a(c, cid=cid):
            r = c.post(
                "/editor/reimbursements/batches",
                json={
                    "idempotency_key": "a",
                    "items": [{"claim_id": cid, "amount_cents": 3000}],
                },
                headers=auth("editor"),
            )
            return (r.status_code, r.json())

        def batch_b(c, cid=cid):
            r = c.post(
                "/editor/reimbursements/batches",
                json={
                    "idempotency_key": "b",
                    "items": [{"claim_id": cid, "amount_cents": 3000}],
                },
                headers=auth("editor"),
            )
            return (r.status_code, r.json())

        out = _concurrent(batch_a, batch_b)
        statuses = sorted([out["a"][0], out["b"][0]])
        assert statuses == [201, 409], out

        check = _fresh_client()
        batches = check.get("/editor/reimbursements/batches", headers=auth("editor")).json()
        assert len(batches) == 1
        assert batches[0]["total_cents"] == 3000


def test_concurrent_withdraw_and_claim(tmp_path):
    """Approval racing a decision withdrawal: either the claim lands before
    release (and is then released), or it is rejected as released.  The
    allowance never ends up active with committed money behind a withdrawn
    decision without that money being correctly classified."""
    for i in range(12):
        db = str(tmp_path / f"wd_race_{i}.db")
        app_config.DB_PATH = db
        database.init_db(db)
        setup = _fresh_client()
        paper, allowance, receipt = _setup_allowance(
            setup, amount_cents=5000, receipt_cents=5000
        )
        aid, rid = allowance["allowance_id"], receipt["receipt_id"]
        author_id = author_user_id(setup, "author1")
        reviewer_id = reviewer_user_id(setup, "reviewer1")

        def claim(c, aid=aid, rid=rid):
            return c.post(
                "/editor/reimbursements/claims",
                json={"allowance_id": aid, "receipt_id": rid, "amount_cents": 5000},
                headers=auth("editor"),
            ).status_code

        def withdraw(c, author_id=author_id, reviewer_id=reviewer_id):
            return c.post(
                "/editor/conflicts",
                json={
                    "author_id": author_id,
                    "reviewer_id": reviewer_id,
                    "reason": "race",
                },
                headers=auth("editor"),
            ).status_code

        out = _concurrent(claim, withdraw)
        assert out["b"] == 201, out
        assert out["a"] in (201, 409), out

        check = _fresh_client()
        view = check.get(
            f"/editor/reimbursements/allowances/{aid}", headers=auth("editor")
        ).json()
        assert view["status"] == "released"
        # Any approved money is released, never left as spendable/undelivered.
        assert view["approved_unsubmitted_cents"] == 0
        assert view["released_unsubmitted_cents"] == (5000 if out["a"] == 201 else 0)
        # No new claim may be approved after release.
        blocked = check.post(
            "/editor/reimbursements/claims",
            json={"allowance_id": aid, "receipt_id": rid, "amount_cents": 100},
            headers=auth("editor"),
        )
        assert blocked.status_code == 409


def test_concurrent_withdraw_and_batch(tmp_path):
    """A delivery batch racing a withdrawal: either the batch reserves the
    cents first (and they remain settleable), or the withdrawal releases them
    and the batch is rejected.  Never a half-delivered batch."""
    for i in range(12):
        db = str(tmp_path / f"wb_race_{i}.db")
        app_config.DB_PATH = db
        database.init_db(db)
        setup = _fresh_client()
        paper, allowance, receipt = _setup_allowance(
            setup, amount_cents=6000, receipt_cents=6000
        )
        aid, rid = allowance["allowance_id"], receipt["receipt_id"]
        claim = _claim(setup, aid, rid, 6000).json()
        cid = claim["claim_id"]
        author_id = author_user_id(setup, "author1")
        reviewer_id = reviewer_user_id(setup, "reviewer1")

        def deliver(c, cid=cid):
            r = c.post(
                "/editor/reimbursements/batches",
                json={
                    "idempotency_key": "race-batch",
                    "items": [{"claim_id": cid, "amount_cents": 6000}],
                },
                headers=auth("editor"),
            )
            return r.status_code

        def withdraw(c, author_id=author_id, reviewer_id=reviewer_id):
            return c.post(
                "/editor/conflicts",
                json={
                    "author_id": author_id,
                    "reviewer_id": reviewer_id,
                    "reason": "race",
                },
                headers=auth("editor"),
            ).status_code

        out = _concurrent(deliver, withdraw)
        assert out["b"] == 201, out
        assert out["a"] in (201, 409), out

        check = _fresh_client()
        batches = check.get("/editor/reimbursements/batches", headers=auth("editor")).json()
        view = check.get(
            f"/editor/reimbursements/allowances/{aid}", headers=auth("editor")
        ).json()

        if out["a"] == 201:
            # Delivered first: no undelivered money was released; the full
            # instruction still encumbers until finance settles it.
            assert len(batches) == 1
            assert view["delivered_pending_cents"] == 6000
            assert view["approved_unsubmitted_cents"] == 0
            assert view["released_unsubmitted_cents"] == 0
        else:
            # Withdrawal first: no batch exists and everything is released.
            assert batches == []
            assert view["delivered_pending_cents"] == 0
            assert view["released_unsubmitted_cents"] == 6000


def test_concurrent_withdraw_and_result(tmp_path):
    """Settling a payment racing a withdrawal: a paid result is never undone,
    and an unsettled instruction keeps encumbering after the withdrawal."""
    for i in range(12):
        db = str(tmp_path / f"wr_race_{i}.db")
        app_config.DB_PATH = db
        database.init_db(db)
        setup = _fresh_client()
        paper, allowance, receipt = _setup_allowance(
            setup, amount_cents=6000, receipt_cents=6000
        )
        aid, rid = allowance["allowance_id"], receipt["receipt_id"]
        claim = _claim(setup, aid, rid, 6000).json()
        batch = _batch(
            setup, "race-result", [{"claim_id": claim["claim_id"], "amount_cents": 6000}]
        ).json()
        bid, pn = batch["batch_id"], batch["payments"][0]["payment_no"]
        author_id = author_user_id(setup, "author1")
        reviewer_id = reviewer_user_id(setup, "reviewer1")

        def settle(c, bid=bid, pn=pn):
            r = c.post(
                f"/editor/reimbursements/batches/{bid}/results",
                json={"results": [{"payment_no": pn, "paid_cents": 4000}]},
                headers=auth("editor"),
            )
            return r.status_code

        def withdraw(c, author_id=author_id, reviewer_id=reviewer_id):
            return c.post(
                "/editor/conflicts",
                json={
                    "author_id": author_id,
                    "reviewer_id": reviewer_id,
                    "reason": "race",
                },
                headers=auth("editor"),
            ).status_code

        out = _concurrent(settle, withdraw)
        assert out["b"] == 201, out
        assert out["a"] == 200, out

        check = _fresh_client()
        view = check.get(
            f"/editor/reimbursements/allowances/{aid}", headers=auth("editor")
        ).json()
        assert view["status"] == "released"
        # Settlement always landed: 4000 paid, 2000 released.
        assert view["paid_cents"] == 4000
        assert view["delivered_pending_cents"] == 0
        assert view["released_payment_difference_cents"] == 2000
        assert view["approved_unsubmitted_cents"] == 0
