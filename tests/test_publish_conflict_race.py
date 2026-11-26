"""Interleaving of conflict registration and decision publication.

The required invariant: it must never happen that a conflict is in effect AND
a decision using that conflicted review is simultaneously published as an
active version.  Writes are serialized (process lock + BEGIN IMMEDIATE), so
for every iteration exactly one of two outcomes is allowed:

* conflict commits first -> publication fails with 409 and no active decision;
* publication commits first -> it is then withdrawn by the conflict.
"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from conftest import accept_and_review, auth, author_user_id, create_manuscript, invite, reviewer_user_id
from review_system import config as app_config
from review_system.main import create_app


def _fresh_client():
    app = create_app(app_config.DB_PATH)
    return TestClient(app)


def _setup_publishable_case(client):
    m = create_manuscript(client)
    inv1 = invite(client, 1, "reviewer1")
    inv2 = invite(client, 1, "reviewer2")
    rv1 = accept_and_review(client, inv1["invitation_id"], "reviewer1", "A", 4)
    rv2 = accept_and_review(client, inv2["invitation_id"], "reviewer2", "B", 5)
    return m, [rv1["review_id"], rv2["review_id"]]


def _final_invariant(client, decision_before: int | None):
    decisions = client.get("/editor/decisions", headers=auth("editor")).json()
    conflicts = client.get("/editor/conflicts", headers=auth("editor")).json()
    assert len(conflicts) == 1
    active = [d for d in decisions if d["status"] == "published"]
    # No active decision may contain the conflicted reviewer's frozen review.
    for d in active:
        editor_view = client.get(
            f"/editor/decisions/{d['decision_id']}", headers=auth("editor")
        ).json()
        # The conflicted reviewer is reviewer1 = anonymous number 1 on this manuscript.
        assert all(r["anon_number"] != 1 for r in editor_view["reviews"])

    if decision_before is not None:
        # If publication happened first, the conflict must have withdrawn it.
        published_versions = [d for d in decisions if d["decision_id"] == decision_before]
        if published_versions:
            assert published_versions[0]["status"] == "withdrawn"


def test_conflict_and_publication_race(tmp_path):
    iterations = 20
    for i in range(iterations):
        db = str(tmp_path / f"race_{i}.db")
        app_config.DB_PATH = db
        from review_system import db as database

        database.init_db(db)
        setup_client = _fresh_client()
        _, review_ids = _setup_publishable_case(setup_client)
        author_id = author_user_id(setup_client, "author1")
        reviewer_id = reviewer_user_id(setup_client, "reviewer1")
        setup_client.close()

        barrier = threading.Barrier(2)
        outcomes: dict[str, int] = {}

        def publish():
            c = _fresh_client()
            barrier.wait()
            r = c.post(
                "/editor/decisions",
                json={"revision_id": 1, "review_ids": review_ids, "conclusion": f"race {i}"},
                headers=auth("editor"),
            )
            outcomes["publish"] = r.status_code
            c.close()

        def conflict():
            c = _fresh_client()
            barrier.wait()
            r = c.post(
                "/editor/conflicts",
                json={"author_id": author_id, "reviewer_id": reviewer_id, "reason": "race conflict"},
                headers=auth("editor"),
            )
            outcomes["conflict"] = r.status_code
            c.close()

        t1 = threading.Thread(target=publish)
        t2 = threading.Thread(target=conflict)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert outcomes["conflict"] == 201, outcomes
        assert outcomes["publish"] in (201, 409), outcomes

        check_client = _fresh_client()
        decision_before = None
        if outcomes["publish"] == 201:
            listing = check_client.get("/editor/decisions", headers=auth("editor")).json()
            decision_before = listing[0]["decision_id"]
        _final_invariant(check_client, decision_before)

        # Author can never see an active body containing the conflicted review.
        author_view = check_client.get("/author/decisions", headers=auth("author1")).json()
        for d in author_view:
            if d.get("status") == "published":
                assert all(r["anon_number"] != 1 for r in d["reviews"])
        check_client.close()


def test_conflict_first_then_publication_is_rejected(tmp_path):
    db = str(tmp_path / "ordering.db")
    app_config.DB_PATH = db
    from review_system import db as database

    database.init_db(db)
    client = _fresh_client()
    _, review_ids = _setup_publishable_case(client)

    r = client.post(
        "/editor/conflicts",
        json={
            "author_id": author_user_id(client, "author1"),
            "reviewer_id": reviewer_user_id(client, "reviewer1"),
            "reason": "first",
        },
        headers=auth("editor"),
    )
    assert r.status_code == 201

    r = client.post(
        "/editor/decisions",
        json={"revision_id": 1, "review_ids": review_ids, "conclusion": "blocked"},
        headers=auth("editor"),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict_blocks_review"
    assert client.get("/editor/decisions", headers=auth("editor")).json() == []


def test_publication_first_then_conflict_withdraws(tmp_path):
    db = str(tmp_path / "ordering2.db")
    app_config.DB_PATH = db
    from review_system import db as database

    database.init_db(db)
    client = _fresh_client()
    _, review_ids = _setup_publishable_case(client)
    r = client.post(
        "/editor/decisions",
        json={"revision_id": 1, "review_ids": review_ids, "conclusion": "first"},
        headers=auth("editor"),
    )
    assert r.status_code == 201
    decision_id = r.json()["decision_id"]

    r = client.post(
        "/editor/conflicts",
        json={
            "author_id": author_user_id(client, "author1"),
            "reviewer_id": reviewer_user_id(client, "reviewer1"),
            "reason": "after",
        },
        headers=auth("editor"),
    )
    assert r.status_code == 201
    assert decision_id in r.json()["withdrawn_decision_ids"]

    assert (
        client.get(f"/author/decisions/{decision_id}", headers=auth("author1")).status_code
        == 410
    )
    view = client.get(f"/editor/decisions/{decision_id}", headers=auth("editor")).json()
    assert view["status"] == "withdrawn" and view["withdrawal_reason"] == "after"
