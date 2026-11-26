"""Conflict of interest lifecycle, publication gating and withdrawal."""

from conftest import (
    accept_and_review,
    auth,
    author_user_id,
    create_manuscript,
    invite,
    publish_two_review_decision,
    reviewer_user_id,
)


def _conflict(client, author: str, reviewer: str, reason: str = "same lab"):
    return client.post(
        "/editor/conflicts",
        json={
            "author_id": author_user_id(client, author),
            "reviewer_id": reviewer_user_id(client, reviewer),
            "reason": reason,
        },
        headers=auth("editor"),
    )


def test_existing_conflict_blocks_invitation(client):
    create_manuscript(client)
    r = _conflict(client, "author1", "reviewer1")
    assert r.status_code == 201
    r = client.post(
        "/editor/invitations",
        json={"revision_id": 1, "reviewer_id": reviewer_user_id(client, "reviewer1")},
        headers=auth("editor"),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict_exists"

    # Duplicate conflict registration is rejected.
    assert _conflict(client, "author1", "reviewer1").status_code == 409


def test_conflict_after_invitation_blocks_acceptance_and_submission(client):
    create_manuscript(client)
    inv = invite(client, 1, "reviewer1")

    r = _conflict(client, "author1", "reviewer1")
    assert r.status_code == 201
    assert r.json()["blocked_invitations"] == 1

    # Cannot accept an invitation blocked by a freshly registered conflict.
    r = client.post(
        f"/reviewer/invitations/{inv['invitation_id']}/respond",
        json={"accept": True},
        headers=auth("reviewer1"),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict_blocked"

    r = client.get("/reviewer/invitations", headers=auth("reviewer1"))
    assert r.json()[0]["status"] == "blocked"


def test_conflict_after_acceptance_blocks_submission(client):
    m = create_manuscript(client)
    rev_id = m["first_revision"]["revision_id"]
    inv = invite(client, rev_id, "reviewer1")
    r = client.post(
        f"/reviewer/invitations/{inv['invitation_id']}/respond",
        json={"accept": True},
        headers=auth("reviewer1"),
    )
    assert r.status_code == 200

    _conflict(client, "author1", "reviewer1")
    r = client.post(
        f"/reviewer/invitations/{inv['invitation_id']}/reviews",
        json={"content": "too late", "score": 3},
        headers=auth("reviewer1"),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict_blocked"


def test_conflict_after_review_keeps_review_editor_visible_but_excludes_from_decision(client):
    create_manuscript(client)
    inv1 = invite(client, 1, "reviewer1")
    inv2 = invite(client, 1, "reviewer2")
    rv1 = accept_and_review(client, inv1["invitation_id"], "reviewer1", "conflicted review", 2)
    rv2 = accept_and_review(client, inv2["invitation_id"], "reviewer2", "clean review", 4)

    r = _conflict(client, "author1", "reviewer1")
    assert r.status_code == 201

    # Editor can still see the original review content and the 'reviewed' invitation.
    r = client.get("/editor/manuscripts/1", headers=auth("editor"))
    invitations = {i["reviewer"]["username"]: i for i in r.json()["invitations"]}
    assert invitations["reviewer1"]["review"]["content"] == "conflicted review"
    assert invitations["reviewer1"]["status"] == "reviewed"

    # But it cannot enter a decision: only one eligible review remains.
    r = client.post(
        "/editor/decisions",
        json={"revision_id": 1, "review_ids": [rv1["review_id"], rv2["review_id"]], "conclusion": "x"},
        headers=auth("editor"),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict_blocks_review"


def test_publication_requires_two_reviews_same_revision_no_conflict(client):
    create_manuscript(client)
    inv1 = invite(client, 1, "reviewer1")
    rv1 = accept_and_review(client, inv1["invitation_id"], "reviewer1", "only one", 3)

    r = client.post(
        "/editor/decisions",
        json={"revision_id": 1, "review_ids": [rv1["review_id"]], "conclusion": "x"},
        headers=auth("editor"),
    )
    assert r.status_code == 422  # pydantic min_length=2

    # Duplicate selection cannot pad the count.
    r = client.post(
        "/editor/decisions",
        json={"revision_id": 1, "review_ids": [rv1["review_id"], rv1["review_id"]], "conclusion": "x"},
        headers=auth("editor"),
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "duplicate_review"


def test_published_version_withdrawn_when_conflict_registered_afterward(client):
    create_manuscript(client)
    inv1 = invite(client, 1, "reviewer1")
    inv2 = invite(client, 1, "reviewer2")
    rv1 = accept_and_review(client, inv1["invitation_id"], "reviewer1", "published A", 4)
    rv2 = accept_and_review(client, inv2["invitation_id"], "reviewer2", "published B", 5)
    decision = publish_two_review_decision(client, 1, [rv1["review_id"], rv2["review_id"]])

    # Conflict involving a selected reviewer withdraws the frozen version.
    r = _conflict(client, "author1", "reviewer1", reason="disclosed collaboration")
    body = r.json()
    assert decision["decision_id"] in body["withdrawn_decision_ids"]

    # Author link no longer serves the body (410 Gone), and the list view hides it.
    r = client.get(f"/author/decisions/{decision['decision_id']}", headers=auth("author1"))
    assert r.status_code == 410
    r = client.get("/author/decisions", headers=auth("author1"))
    entry = r.json()[0]
    assert entry["status"] == "withdrawn"
    assert "reviews" not in entry and "conclusion" not in entry

    # Editor retains the original frozen content plus the withdrawal reason.
    r = client.get(f"/editor/decisions/{decision['decision_id']}", headers=auth("editor"))
    editor_view = r.json()
    assert editor_view["status"] == "withdrawn"
    assert editor_view["withdrawal_reason"] == "disclosed collaboration"
    assert sorted(x["content"] for x in editor_view["reviews"]) == ["published A", "published B"]


def test_republish_after_withdrawal_requires_new_qualified_reviews(client):
    create_manuscript(client)
    inv1 = invite(client, 1, "reviewer1")
    inv2 = invite(client, 1, "reviewer2")
    rv1 = accept_and_review(client, inv1["invitation_id"], "reviewer1", "old A", 4)
    rv2 = accept_and_review(client, inv2["invitation_id"], "reviewer2", "clean B", 5)
    decision = publish_two_review_decision(client, 1, [rv1["review_id"], rv2["review_id"]])
    _conflict(client, "author1", "reviewer1")

    # Old conflicted review can never be selected again.
    r = client.post(
        "/editor/decisions",
        json={"revision_id": 1, "review_ids": [rv1["review_id"], rv2["review_id"]], "conclusion": "retry"},
        headers=auth("editor"),
    )
    assert r.status_code == 409

    # A fresh, conflict-free third reviewer allows a new publication.
    inv3 = invite(client, 1, "reviewer3")
    rv3 = accept_and_review(client, inv3["invitation_id"], "reviewer3", "new C", 3)
    new_decision = publish_two_review_decision(
        client, 1, [rv2["review_id"], rv3["review_id"]], conclusion="accept after re-review"
    )
    assert new_decision["decision_id"] != decision["decision_id"]

    r = client.get("/author/decisions", headers=auth("author1"))
    statuses = {d["decision_id"]: d.get("status", "published") for d in r.json()}
    assert statuses[decision["decision_id"]] == "withdrawn"
    assert statuses[new_decision["decision_id"]] == "published"
    live = next(d for d in r.json() if d["decision_id"] == new_decision["decision_id"])
    assert sorted(x["anon_number"] for x in live["reviews"]) == [2, 3]


def test_only_one_active_decision_per_revision(client):
    create_manuscript(client)
    inv1 = invite(client, 1, "reviewer1")
    inv2 = invite(client, 1, "reviewer2")
    rv1 = accept_and_review(client, inv1["invitation_id"], "reviewer1", "A", 4)
    rv2 = accept_and_review(client, inv2["invitation_id"], "reviewer2", "B", 5)
    publish_two_review_decision(client, 1, [rv1["review_id"], rv2["review_id"]])

    r = client.post(
        "/editor/decisions",
        json={"revision_id": 1, "review_ids": [rv1["review_id"], rv2["review_id"]], "conclusion": "again"},
        headers=auth("editor"),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "decision_exists"
