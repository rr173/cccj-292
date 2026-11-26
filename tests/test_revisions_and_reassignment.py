"""Revision append-only semantics, revision-scoped reviews, reassignment."""

from conftest import accept_and_review, auth, create_manuscript, invite, publish_two_review_decision


def test_revisions_are_append_only_and_independent(client):
    m = create_manuscript(client, content="original body")
    r = client.post(
        f"/author/manuscripts/{m['manuscript_id']}/revisions",
        json={"content": "second body"},
        headers=auth("author1"),
    )
    assert r.status_code == 201
    rev2 = r.json()
    assert rev2["revision_no"] == 2

    r = client.get(f"/author/manuscripts/{m['manuscript_id']}", headers=auth("author1"))
    revisions = r.json()["revisions"]
    assert [x["revision_no"] for x in revisions] == [1, 2]
    # Old version content is still present, untouched.
    assert revisions[0]["content"] == "original body"
    assert revisions[1]["content"] == "second body"


def test_invitations_target_one_revision_and_reviews_stay_there(client):
    m = create_manuscript(client)
    # Revision 1 reviews.
    inv_r1_a = invite(client, 1, "reviewer1")
    inv_r1_b = invite(client, 1, "reviewer2")
    review1 = accept_and_review(client, inv_r1_a["invitation_id"], "reviewer1", "r1 comment A", 4)
    review2 = accept_and_review(client, inv_r1_b["invitation_id"], "reviewer2", "r1 comment B", 3)

    # New revision does not carry reviews over.
    r = client.post(
        f"/author/manuscripts/{m['manuscript_id']}/revisions",
        json={"content": "v2 body"},
        headers=auth("author1"),
    )
    revision2_id = r.json()["revision_id"]

    inv_r2 = invite(client, revision2_id, "reviewer1")
    accept_and_review(client, inv_r2["invitation_id"], "reviewer1", "r2 comment", 5)

    # Publishing revision 2 cannot borrow revision-1 reviews.
    r = client.post(
        "/editor/decisions",
        json={
            "revision_id": revision2_id,
            "review_ids": [review1["review_id"], review2["review_id"]],
            "conclusion": "mixing revisions",
        },
        headers=auth("editor"),
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "revision_mismatch"

    # And a revision-1 decision cannot swallow the revision-2 review.
    r = client.get("/editor/manuscripts/1", headers=auth("editor"))
    r2_review_id = next(
        i["review"]["review_id"]
        for i in r.json()["invitations"]
        if i["revision_id"] == revision2_id
    )
    r = client.post(
        "/editor/decisions",
        json={
            "revision_id": 1,
            "review_ids": [review1["review_id"], r2_review_id],
            "conclusion": "cross revision",
        },
        headers=auth("editor"),
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "revision_mismatch"

    # The valid same-revision publication still works.
    decision = publish_two_review_decision(client, 1, [review1["review_id"], review2["review_id"]])
    assert {x["anon_number"] for x in decision["reviews"]} == {1, 2}


def test_reassignment_keeps_old_review_with_original_reviewer(client):
    m = create_manuscript(client)
    inv_r1 = invite(client, 1, "reviewer1")
    old_review = accept_and_review(client, inv_r1["invitation_id"], "reviewer1", "original review", 2)

    # Reassign the same revision to reviewer 2 (a fresh invitation).
    inv_r2 = invite(client, 1, "reviewer2")
    accept_and_review(client, inv_r2["invitation_id"], "reviewer2", "new review", 4)

    r = client.get("/editor/manuscripts/1", headers=auth("editor"))
    invitations = r.json()["invitations"]
    by_reviewer = {i["reviewer"]["username"]: i for i in invitations}

    # The old review remains owned by reviewer 1 and tied to the original invitation.
    assert by_reviewer["reviewer1"]["review"]["review_id"] == old_review["review_id"]
    assert by_reviewer["reviewer1"]["review"]["content"] == "original review"
    assert by_reviewer["reviewer2"]["review"]["content"] == "new review"
    assert by_reviewer["reviewer1"]["invitation_id"] == inv_r1["invitation_id"]
    assert by_reviewer["reviewer2"]["invitation_id"] == inv_r2["invitation_id"]

    # Reviewer 2's own view never contains reviewer 1's content.
    r = client.get("/reviewer/invitations", headers=auth("reviewer2"))
    assert "original review" not in r.text
    assert r.json()[0]["my_review"]["content"] == "new review"


def test_submitted_review_is_immutable(client):
    m = create_manuscript(client)
    rev_id = m["first_revision"]["revision_id"]
    inv = invite(client, rev_id, "reviewer1")
    first = accept_and_review(client, inv["invitation_id"], "reviewer1", "first", 3)

    # Second submission to the same invitation is refused, not replaced.
    r = client.post(
        f"/reviewer/invitations/{inv['invitation_id']}/reviews",
        json={"content": "attempted replacement", "score": 5},
        headers=auth("reviewer1"),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "review_exists"

    r = client.get("/reviewer/invitations", headers=auth("reviewer1"))
    assert r.json()[0]["my_review"]["content"] == "first"
    assert first["review_id"]
