"""Race between the final author consent and a conflict withdrawal.

Required invariant: after the conflict-withdrawal transaction completes, no
confirmed session and no reservation may remain that is based on the withdrawn
decision.  Writes are serialised by the process lock + BEGIN IMMEDIATE, so the
two transactions have exactly two legal orderings.
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
from review_system.main import create_app

DEADLINE = "2030-05-01T00:00:00+00:00"
START = "2030-06-03T10:00:00+00:00"
END = "2030-06-03T11:00:00+00:00"
START_2 = "2030-06-03T14:00:00+00:00"
END_2 = "2030-06-03T15:00:00+00:00"


def _fresh_client():
    app = create_app(app_config.DB_PATH)
    return TestClient(app)


def _paper(client, author, reviewers, title):
    m = create_manuscript(client, author=author, title=title)
    revision_id = m["first_revision"]["revision_id"]
    review_ids = []
    for reviewer in reviewers:
        inv = invite(client, revision_id, reviewer)
        out = accept_and_review(client, inv["invitation_id"], reviewer, "review", 4)
        review_ids.append(out["review_id"])
    decision = publish_two_review_decision(client, revision_id, review_ids)
    return {"manuscript_id": m["manuscript_id"], "decision_id": decision["decision_id"]}


def _group_body(papers_and_slots):
    return {
        "confirmation_deadline": DEADLINE,
        "items": [
            {
                "manuscript_id": p["manuscript_id"],
                "decision_id": p["decision_id"],
                "room": room,
                "starts_at": start,
                "ends_at": end,
            }
            for p, room, start, end in papers_and_slots
        ],
    }


def test_final_consent_vs_withdrawal_single_paper_race(tmp_path):
    iterations = 12
    for i in range(iterations):
        db = str(tmp_path / f"single_{i}.db")
        app_config.DB_PATH = db
        from review_system import db as database

        database.init_db(db)
        setup = _fresh_client()
        paper = _paper(setup, "author1", ("reviewer1", "reviewer2"), f"P{i}")
        gid = setup.post(
            "/editor/schedule/groups",
            json=_group_body([(paper, "hall-a", START, END)]),
            headers=auth("editor"),
        ).json()["group_id"]
        item_id = setup.get(
            f"/editor/schedule/groups/{gid}", headers=auth("editor")
        ).json()["items"][0]["item_id"]
        author_id = author_user_id(setup, "author1")
        reviewer_id = reviewer_user_id(setup, "reviewer1")

        barrier = threading.Barrier(2)
        outcomes: dict[str, int] = {}

        def consent():
            c = _fresh_client()
            barrier.wait()
            r = c.post(
                f"/author/schedule/items/{item_id}/respond",
                json={"accept": True},
                headers=auth("author1"),
            )
            outcomes["consent"] = r.status_code
            outcomes["consent_code"] = r.json().get("error", {}).get("code")

        def withdraw():
            c = _fresh_client()
            barrier.wait()
            r = c.post(
                "/editor/conflicts",
                json={
                    "author_id": author_id,
                    "reviewer_id": reviewer_id,
                    "reason": "race conflict",
                },
                headers=auth("editor"),
            )
            outcomes["withdraw"] = r.status_code

        t1 = threading.Thread(target=consent)
        t2 = threading.Thread(target=withdraw)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert outcomes["withdraw"] == 201, outcomes
        assert outcomes["consent"] in (200, 409), outcomes

        check = _fresh_client()
        schedule = check.get("/editor/schedule", headers=auth("editor")).json()
        occupancy = check.get("/editor/schedule/occupancy", headers=auth("editor")).json()
        group = check.get(f"/editor/schedule/groups/{gid}", headers=auth("editor")).json()
        history = check.get(
            "/editor/schedule?history=true", headers=auth("editor")
        ).json()

        # Never an occupying session or reservation based on the withdrawn decision.
        assert schedule == [], outcomes
        assert occupancy == [], outcomes

        if outcomes["consent"] == 200:
            # Consent finalised first; withdrawal then voided the new session.
            assert group["status"] == "confirmed"
            assert len(history) == 1
            assert history[0]["status"] == "voided"
            assert history[0]["end_reason"] == "decision_withdrawn"
        else:
            # Withdrawal committed first and cancelled the pending group.
            assert outcomes["consent_code"] == "group_cancelled", outcomes
            assert group["status"] == "cancelled"
            assert group["end_reason"] == "decision_withdrawn"
            assert history == []

        # Author calendar agrees.
        mine = check.get("/author/schedule/grouped", headers=auth("author1")).json()
        assert mine["confirmed_sessions"] == []


def test_final_consent_vs_withdrawal_multi_paper_race(tmp_path):
    iterations = 10
    for i in range(iterations):
        db = str(tmp_path / f"multi_{i}.db")
        app_config.DB_PATH = db
        from review_system import db as database

        database.init_db(db)
        setup = _fresh_client()
        p1 = _paper(setup, "author1", ("reviewer1", "reviewer2"), f"A{i}")
        p2 = _paper(setup, "author2", ("reviewer2", "reviewer3"), f"B{i}")

        created = setup.post(
            "/editor/schedule/groups",
            json=_group_body(
                [
                    (p1, "hall-a", START, END),
                    (p2, "hall-b", START_2, END_2),
                ]
            ),
            headers=auth("editor"),
        ).json()
        gid = created["group_id"]
        i1 = next(x["item_id"] for x in created["items"] if x["manuscript_id"] == p1["manuscript_id"])
        i2 = next(x["item_id"] for x in created["items"] if x["manuscript_id"] == p2["manuscript_id"])

        # author1 answers first, sequentially; author2's consent is the last one.
        r = setup.post(
            f"/author/schedule/items/{i1}/respond",
            json={"accept": True},
            headers=auth("author1"),
        )
        assert r.status_code == 200, r.text

        author_id = author_user_id(setup, "author1")
        reviewer_id = reviewer_user_id(setup, "reviewer1")
        barrier = threading.Barrier(2)
        outcomes: dict[str, int] = {}

        def last_consent():
            c = _fresh_client()
            barrier.wait()
            rr = c.post(
                f"/author/schedule/items/{i2}/respond",
                json={"accept": True},
                headers=auth("author2"),
            )
            outcomes["consent"] = rr.status_code
            outcomes["consent_code"] = rr.json().get("error", {}).get("code")

        def withdraw():
            c = _fresh_client()
            barrier.wait()
            rr = c.post(
                "/editor/conflicts",
                json={
                    "author_id": author_id,
                    "reviewer_id": reviewer_id,
                    "reason": "race conflict multi",
                },
                headers=auth("editor"),
            )
            outcomes["withdraw"] = rr.status_code

        t1 = threading.Thread(target=last_consent)
        t2 = threading.Thread(target=withdraw)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert outcomes["withdraw"] == 201, outcomes
        assert outcomes["consent"] in (200, 409), outcomes

        check = _fresh_client()
        schedule = check.get("/editor/schedule", headers=auth("editor")).json()
        group = check.get(f"/editor/schedule/groups/{gid}", headers=auth("editor")).json()

        p1_sessions = [s for s in schedule if s["manuscript_id"] == p1["manuscript_id"]]
        assert p1_sessions == [], outcomes  # withdrawn decision never occupies

        if outcomes["consent"] == 200:
            assert group["status"] == "confirmed"
            # p2's fresh arrangement survives; only p1's session was voided.
            survivors = [s for s in schedule if s["status"] == "confirmed"]
            assert len(survivors) == 1
            assert survivors[0]["manuscript_id"] == p2["manuscript_id"]
        else:
            assert outcomes["consent_code"] == "group_cancelled", outcomes
            assert group["status"] == "cancelled"
            # Conflict won before finalisation: nothing was created at all.
            assert schedule == [], outcomes

        # No pending reservations survive in either ordering.
        occ = check.get("/editor/schedule/occupancy", headers=auth("editor")).json()
        pending = [o for o in occ if o["kind"] == "reservation"]
        assert pending == [], outcomes
