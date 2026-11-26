"""Authentication, role boundaries and cross-tenant isolation."""

from conftest import auth, create_manuscript, invite


def test_missing_or_bad_api_key_rejected(client):
    assert client.get("/health").status_code == 200
    r = client.get("/author/manuscripts")
    assert r.status_code == 401
    r = client.get("/author/manuscripts", headers={"X-API-Key": "nope"})
    assert r.status_code == 401


def test_role_boundaries(client):
    # Author cannot call reviewer/editor endpoints and vice versa.
    assert client.get("/reviewer/invitations", headers=auth("author1")).status_code == 403
    assert client.get("/editor/accounts", headers=auth("reviewer1")).status_code == 403
    assert client.get("/author/manuscripts", headers=auth("editor")).status_code == 403


def test_author_cannot_see_other_authors_manuscript(client):
    m = create_manuscript(client, "author1")
    r = client.get(f"/author/manuscripts/{m['manuscript_id']}", headers=auth("author2"))
    assert r.status_code == 404


def test_author_sees_nothing_before_publication(client):
    m = create_manuscript(client)
    inv = invite(client, 1, "reviewer1")
    # Even while a review exists, the author has no route to review bodies.
    from conftest import accept_and_review

    accept_and_review(client, inv["invitation_id"], "reviewer1", "secret comments", 4)

    r = client.get(f"/author/manuscripts/{m['manuscript_id']}", headers=auth("author1"))
    assert r.status_code == 200
    assert "invitations" not in r.json()
    assert "review" not in r.text and "secret comments" not in r.text

    r = client.get("/author/decisions", headers=auth("author1"))
    assert r.json() == []


def test_reviewer_cannot_read_other_reviewers_invitation_or_review(client):
    m = create_manuscript(client)
    inv1 = invite(client, 1, "reviewer1")
    invite(client, 1, "reviewer2")

    # Reviewer 2 must not reach reviewer 1's invitation by id.
    r = client.post(
        f"/reviewer/invitations/{inv1['invitation_id']}/respond",
        json={"accept": True},
        headers=auth("reviewer2"),
    )
    assert r.status_code == 404

    from conftest import accept_and_review

    accept_and_review(client, inv1["invitation_id"], "reviewer1", "r1 only", 5)

    r = client.post(
        f"/reviewer/invitations/{inv1['invitation_id']}/reviews",
        json={"content": "hijack", "score": 1},
        headers=auth("reviewer2"),
    )
    assert r.status_code == 404

    # The listing for reviewer 2 contains only reviewer 2's own invitation.
    r = client.get("/reviewer/invitations", headers=auth("reviewer2"))
    items = r.json()
    assert len(items) == 1
    assert "my_review" not in items[0]
    assert "r1 only" not in r.text


def test_score_must_be_integer_1_to_5(client):
    m = create_manuscript(client)
    inv = invite(client, m["first_revision"]["revision_id"], "reviewer1")
    client.post(
        f"/reviewer/invitations/{inv['invitation_id']}/respond",
        json={"accept": True},
        headers=auth("reviewer1"),
    )
    for bad in (0, 6, 3.5, "4"):
        r = client.post(
            f"/reviewer/invitations/{inv['invitation_id']}/reviews",
            json={"content": "x", "score": bad},
            headers=auth("reviewer1"),
        )
        assert r.status_code == 422, bad
