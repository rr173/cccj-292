"""Anonymous numbering: stable within a manuscript, disjoint across manuscripts."""

from conftest import accept_and_review, auth, create_manuscript, invite, publish_two_review_decision


def test_author_payload_exposes_only_anonymous_numbers(client):
    m = create_manuscript(client)
    inv1 = invite(client, 1, "reviewer1")
    inv2 = invite(client, 1, "reviewer2")
    rv1 = accept_and_review(client, inv1["invitation_id"], "reviewer1", "comment one", 4)
    rv2 = accept_and_review(client, inv2["invitation_id"], "reviewer2", "comment two", 5)

    publish_two_review_decision(client, 1, [rv1["review_id"], rv2["review_id"]])

    r = client.get("/author/decisions", headers=auth("author1"))
    decision = r.json()[0]
    text = r.text
    # No real accounts, no invitation ids, no internal review ids.
    for leaked in ("reviewer1", "reviewer2", "invitation", "review_id", "key-"):
        assert leaked not in text
    assert {x["anon_number"] for x in decision["reviews"]} == {1, 2}
    assert {x["score"] for x in decision["reviews"]} == {4, 5}

    # Stable numbers: direct decision link returns the same payload.
    r2 = client.get(f"/author/decisions/{decision['decision_id']}", headers=auth("author1"))
    assert r2.json() == decision


def test_anon_numbers_are_per_manuscript_and_stable(client):
    # Manuscript 1: reviewer1 -> #1, reviewer2 -> #2
    m1 = create_manuscript(client, "author1", title="Paper A")
    a = invite(client, m1["manuscript_id"], "reviewer1")
    b = invite(client, m1["manuscript_id"], "reviewer2")
    assert a["anon_number"] == 1 and b["anon_number"] == 2

    # Manuscript 2 (different author): reviewer1 starts again at #1.
    m2 = create_manuscript(client, "author2", title="Paper B")
    r = client.get(f"/editor/manuscripts/{m2['manuscript_id']}", headers=auth("editor"))
    revision2 = r.json()["revisions"][0]["revision_id"]
    c = invite(client, revision2, "reviewer1")
    assert c["anon_number"] == 1

    # Re-inviting the same reviewer to a new revision of manuscript 1 keeps #1.
    r = client.post(
        f"/author/manuscripts/{m1['manuscript_id']}/revisions",
        json={"content": "paper A revision 2"},
        headers=auth("author1"),
    )
    new_revision = r.json()["revision_id"]
    d = invite(client, new_revision, "reviewer1")
    assert d["anon_number"] == 1


def test_published_payload_is_a_fixed_snapshot(client):
    m = create_manuscript(client)
    inv = invite(client, 1, "reviewer1")
    inv2 = invite(client, 1, "reviewer2")
    rv1 = accept_and_review(client, inv["invitation_id"], "reviewer1", "frozen A", 3)
    rv2 = accept_and_review(client, inv2["invitation_id"], "reviewer2", "frozen B", 4)
    publish_two_review_decision(client, 1, [rv1["review_id"], rv2["review_id"]], conclusion="minor revision")

    # The author-facing content is the frozen snapshot; underlying review rows
    # are never updated anywhere in the system anyway.
    r = client.get("/author/decisions", headers=auth("author1"))
    decision = r.json()[0]
    assert decision["conclusion"] == "minor revision"
    assert sorted(x["content"] for x in decision["reviews"]) == ["frozen A", "frozen B"]
