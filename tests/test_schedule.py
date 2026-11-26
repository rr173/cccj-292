"""Conference talk scheduling, linked group rescheduling and confirmation."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from conftest import (
    accept_and_review,
    auth,
    create_manuscript,
    invite,
    publish_two_review_decision,
)
from review_system import config

# Fixed far-future calendar so deadline < every session start.
DAY0 = datetime(2030, 6, 3, tzinfo=timezone.utc)
DEADLINE = (DAY0 - timedelta(days=30)).isoformat(timespec="seconds")


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return DAY0.replace(day=DAY0.day + day, hour=hour, minute=minute)


def range_json(day: int, start_h: int, end_h: int):
    return iso(at(day, start_h)), iso(at(day, end_h))


S_10_11 = (iso(at(0, 10)), iso(at(0, 11)))
S_11_12 = (iso(at(0, 11)), iso(at(0, 12)))
S_14_15 = (iso(at(0, 14)), iso(at(0, 15)))
S_15_16 = (iso(at(0, 15)), iso(at(0, 16)))
NEXT_10_11 = (iso(at(1, 10)), iso(at(1, 11)))
NEXT_11_12 = (iso(at(1, 11)), iso(at(1, 12)))


def make_paper(
    client,
    author: str = "author1",
    reviewers=("reviewer1", "reviewer2"),
    title: str | None = None,
    conclusion: str = "accept",
):
    m = create_manuscript(client, author=author, title=title or f"paper-{author}-{len(reviewers)}")
    revision_id = m["first_revision"]["revision_id"]
    review_ids = []
    for reviewer in reviewers:
        inv = invite(client, revision_id, reviewer)
        out = accept_and_review(
            client, inv["invitation_id"], reviewer, f"review by {reviewer}", 4
        )
        review_ids.append(out["review_id"])
    decision = publish_two_review_decision(
        client, revision_id, review_ids, conclusion=conclusion
    )
    return {
        "manuscript_id": m["manuscript_id"],
        "revision_id": revision_id,
        "decision_id": decision["decision_id"],
        "review_ids": review_ids,
    }


_UNSET = object()


def arrangement(paper, *, room="hall-a", times=S_10_11, decision=_UNSET, manuscript_id=None):
    start, end = times
    body = {
        "manuscript_id": manuscript_id if manuscript_id is not None else paper["manuscript_id"],
        "room": room,
        "starts_at": start,
        "ends_at": end,
    }
    if decision is _UNSET:
        decision = paper["decision_id"]
    if decision is not None:
        body["decision_id"] = decision
    return body


def create_group(client, arrangements, deadline: str = DEADLINE):
    return client.post(
        "/editor/schedule/groups",
        json={"confirmation_deadline": deadline, "items": arrangements},
        headers=auth("editor"),
    )


def accept_item(client, item_id: int, author: str):
    return client.post(
        f"/author/schedule/items/{item_id}/respond",
        json={"accept": True},
        headers=auth(author),
    )


def decline_item(client, item_id: int, author: str):
    return client.post(
        f"/author/schedule/items/{item_id}/respond",
        json={"accept": False},
        headers=auth(author),
    )


def confirm_single_paper(client, paper, author="author1", *, room="hall-a", times=S_10_11):
    r = create_group(client, [arrangement(paper, room=room, times=times)])
    assert r.status_code == 201, r.text
    group = r.json()
    item_id = group["items"][0]["item_id"]
    ar = accept_item(client, item_id, author)
    assert ar.status_code == 200, ar.text
    return group, ar.json()


def conflict(client, author_user: str, reviewer: str):
    from conftest import author_user_id, reviewer_user_id

    return client.post(
        "/editor/conflicts",
        json={
            "author_id": author_user_id(client, author_user),
            "reviewer_id": reviewer_user_id(client, reviewer),
            "reason": "late disclosed collaboration",
        },
        headers=auth("editor"),
    )


# ---------------------------------------------------------------------------
# Basic happy path / visibility / roles
# ---------------------------------------------------------------------------

def test_rooms_come_from_local_config(client):
    r = client.get("/editor/rooms", headers=auth("editor"))
    assert r.status_code == 200
    codes = {room["code"] for room in r.json()}
    assert {"hall-a", "hall-b"} <= codes


def test_editor_schedules_and_author_confirms_one_arrangement(client):
    paper = make_paper(client)

    # Before confirmation the public schedule is empty.
    assert client.get("/editor/schedule", headers=auth("editor")).json() == []

    r = create_group(client, [arrangement(paper, room="A", times=S_10_11)])
    assert r.status_code == 201, r.text
    group = r.json()
    assert group["status"] == "pending"
    assert group["progress"] == {
        "total": 1,
        "accepted": 0,
        "declined": 0,
        "waiting": 1,
        "all_accepted": False,
    }
    item = group["items"][0]
    # The supplied token is echoed, with the canonical config code exposed too.
    assert item["room"] == "A"
    assert item["room_code"] == "hall-a"
    assert item["room_name"] == "Grand Hall A"

    # Reservation exists but no confirmed session yet.
    occ = client.get("/editor/schedule/occupancy", headers=auth("editor")).json()
    assert any(x["kind"] == "reservation" and x["room"] == "hall-a" for x in occ)

    mine = client.get("/author/schedule/grouped", headers=auth("author1")).json()
    assert len(mine["pending_confirmations"]) == 1
    pending = mine["pending_confirmations"][0]
    assert pending["can_respond"] is True
    assert pending["room"] == "A"
    assert pending["room_code"] == "hall-a"

    ar = accept_item(client, item["item_id"], "author1")
    assert ar.status_code == 200, ar.text
    assert ar.json()["group"]["status"] == "confirmed"

    schedule = client.get("/editor/schedule", headers=auth("editor")).json()
    assert len(schedule) == 1
    assert schedule[0]["room"] == "A"
    assert schedule[0]["room_code"] == "hall-a"
    assert schedule[0]["status"] == "confirmed"

    flat = client.get("/author/schedule", headers=auth("author1")).json()
    confirmed = [e for e in flat if e["kind"] == "confirmed_session"]
    assert len(confirmed) == 1


def test_author_cannot_see_or_answer_other_authors_arrangement(client):
    paper = make_paper(client, author="author1")
    group = create_group(client, [arrangement(paper)]).json()
    item_id = group["items"][0]["item_id"]

    # author2 has no arrangements at all.
    assert client.get("/author/schedule", headers=auth("author2")).json() == []

    # Guessing author1's item id yields 404, not 403.
    r = accept_item(client, item_id, "author2")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_schedule_endpoints_respect_roles(client):
    paper = make_paper(client)
    body = {"confirmation_deadline": DEADLINE, "items": [arrangement(paper)]}
    assert client.post("/editor/schedule/groups", json=body, headers=auth("author1")).status_code == 403
    assert client.get("/editor/schedule", headers=auth("reviewer1")).status_code == 403
    assert client.get("/editor/schedule/groups", headers=auth("author1")).status_code == 403
    assert client.get("/author/schedule", headers=auth("editor")).status_code == 403


# ---------------------------------------------------------------------------
# Input validation / decision rules
# ---------------------------------------------------------------------------

def test_duplicate_manuscript_in_one_group_rejected(client):
    paper = make_paper(client)
    r = create_group(client, [arrangement(paper), arrangement(paper, room="hall-b")])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "duplicate_manuscript"


def test_editor_must_pick_live_published_decision_for_unscheduled_paper(client):
    paper = make_paper(client)

    # No decision id and no existing arrangement -> rejected.
    r = create_group(client, [arrangement(paper, decision=None)])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "decision_required"

    # Decision belonging to another manuscript.
    other = make_paper(client, author="author2", reviewers=("reviewer2", "reviewer3"))
    r = create_group(
        client,
        [arrangement(paper, decision=other["decision_id"])],
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "decision_mismatch"


def test_withdrawn_decision_cannot_back_new_arrangement(client):
    paper = make_paper(client)
    assert conflict(client, "author1", "reviewer1").status_code == 201

    r = create_group(client, [arrangement(paper)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "decision_not_active"


def test_editor_picks_papers_without_parsing_conclusion_text(client):
    # A conclusion worded like a rejection is still schedulable: the service
    # never inspects conclusion contents.
    paper = make_paper(client, conclusion="reject - weak paper")
    r = create_group(client, [arrangement(paper)])
    assert r.status_code == 201


def test_invalid_room_time_range_and_timestamp_rejected(client):
    paper = make_paper(client)

    r = create_group(client, [arrangement(paper, room="no-such-room")])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unknown_room"

    start, end = S_10_11
    r = create_group(client, [arrangement(paper, times=(end, start))])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_time_range"

    body = {
        "confirmation_deadline": DEADLINE,
        "items": [
            {
                "manuscript_id": paper["manuscript_id"],
                "decision_id": paper["decision_id"],
                "room": "hall-a",
                "starts_at": "not-a-time",
                "ends_at": end,
            }
        ],
    }
    r = client.post("/editor/schedule/groups", json=body, headers=auth("editor"))
    assert r.status_code == 422


def test_naive_timestamps_are_treated_as_utc(client):
    paper = make_paper(client)
    body = {
        "confirmation_deadline": "2030-05-01T00:00:00",
        "items": [
            {
                "manuscript_id": paper["manuscript_id"],
                "decision_id": paper["decision_id"],
                "room": "hall-a",
                "starts_at": "2030-06-03T10:00:00",
                "ends_at": "2030-06-03T11:00:00",
            }
        ],
    }
    r = client.post("/editor/schedule/groups", json=body, headers=auth("editor"))
    assert r.status_code == 201, r.text
    assert r.json()["items"][0]["starts_at"].endswith("+00:00")


def test_deadline_must_precede_new_slot_starts(client):
    paper = make_paper(client)
    too_late = iso(at(0, 10))  # equal to start is not "before"
    r = create_group(client, [arrangement(paper)], deadline=too_late)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "deadline_too_late"

    after_start = iso(at(0, 10, 30))
    r = create_group(client, [arrangement(paper)], deadline=after_start)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "deadline_too_late"


def test_deadline_must_precede_old_slot_starts_on_reschedule(client):
    paper = make_paper(client)
    confirm_single_paper(client, paper, times=S_10_11)

    # New slot is next day (fine for the new-start rule) but deadline is after
    # the *old* slot start -> rejected.
    r = create_group(
        client,
        [arrangement(paper, times=NEXT_10_11)],
        deadline=iso(at(0, 10, 30)),
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "deadline_too_late"


# ---------------------------------------------------------------------------
# Overlap rules
# ---------------------------------------------------------------------------

def test_same_room_overlap_blocked_but_back_to_back_allowed(client):
    p1 = make_paper(client, author="author1")
    p2 = make_paper(client, author="author2", reviewers=("reviewer2", "reviewer3"))

    assert create_group(client, [arrangement(p1, room="hall-a", times=S_10_11)]).status_code == 201

    # Overlapping reservation in the same room.
    r = create_group(client, [arrangement(p2, room="hall-a", times=("2030-06-03T10:30:00+00:00", "2030-06-03T11:30:00+00:00"))])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "schedule_conflict"

    # Back-to-back (11:00 start when previous ends 11:00) is allowed.
    r = create_group(client, [arrangement(p2, room="hall-a", times=S_11_12)])
    assert r.status_code == 201, r.text


def test_different_rooms_same_time_allowed(client):
    p1 = make_paper(client, author="author1")
    p2 = make_paper(client, author="author2", reviewers=("reviewer2", "reviewer3"))
    create_group(client, [arrangement(p1, room="hall-a", times=S_10_11)])
    r = create_group(client, [arrangement(p2, room="hall-b", times=S_10_11)])
    assert r.status_code == 201


def test_same_author_two_manuscripts_cannot_overlap_even_in_different_rooms(client):
    p1 = make_paper(client, author="author1")
    p2 = make_paper(
        client,
        author="author1",
        reviewers=("reviewer2", "reviewer3"),
        title="second paper by same author",
    )
    r = create_group(
        client,
        [
            arrangement(p1, room="hall-a", times=S_10_11),
            arrangement(p2, room="hall-b", times=("2030-06-03T10:30:00+00:00", "2030-06-03T11:30:00+00:00")),
        ],
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "schedule_conflict"

    # Back-to-back for the same author is fine.
    r = create_group(
        client,
        [
            arrangement(p1, room="hall-a", times=S_10_11),
            arrangement(p2, room="hall-b", times=S_11_12),
        ],
    )
    assert r.status_code == 201, r.text


def test_internal_group_overlap_rejected(client):
    p1 = make_paper(client, author="author1")
    p2 = make_paper(client, author="author2", reviewers=("reviewer2", "reviewer3"))
    r = create_group(
        client,
        [
            arrangement(p1, room="hall-a", times=S_10_11),
            arrangement(p2, room="hall-a", times=S_10_11),
        ],
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "schedule_conflict"


def test_pending_reservation_blocks_other_groups_until_released(client):
    p1 = make_paper(client, author="author1")
    p2 = make_paper(client, author="author2", reviewers=("reviewer2", "reviewer3"))
    p3 = make_paper(client, author="author2", reviewers=("reviewer1", "reviewer3"), title="p3")

    g1 = create_group(client, [arrangement(p1, room="hall-a", times=S_10_11)]).json()

    # Another group cannot take the reserved slot.
    r = create_group(client, [arrangement(p2, room="hall-a", times=S_10_11)])
    assert r.status_code == 409

    # Decline releases the reservation (single-item group terminates).
    item_id = g1["items"][0]["item_id"]
    decline_item(client, item_id, "author1")

    # Now the slot is free.
    r = create_group(client, [arrangement(p3, room="hall-a", times=S_10_11)])
    assert r.status_code == 201, r.text


def test_confirmed_session_blocks_other_groups(client):
    p1 = make_paper(client, author="author1")
    p2 = make_paper(client, author="author2", reviewers=("reviewer2", "reviewer3"))
    confirm_single_paper(client, p1, "author1", room="hall-a", times=S_10_11)

    r = create_group(client, [arrangement(p2, room="hall-a", times=S_10_11)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "schedule_conflict"


def test_manuscript_cannot_join_two_pending_groups(client):
    p1 = make_paper(client)
    create_group(client, [arrangement(p1, room="hall-a", times=S_10_11)])

    r = create_group(client, [arrangement(p1, room="hall-b", times=S_14_15)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "manuscript_pending"


# ---------------------------------------------------------------------------
# Linked rescheduling / swap
# ---------------------------------------------------------------------------

def test_swap_two_talks_without_cancelling_first(client):
    p1 = make_paper(client, author="author1")
    p2 = make_paper(client, author="author2", reviewers=("reviewer2", "reviewer3"))

    # Existing confirmed consecutive slots in the same room.
    confirm_single_paper(client, p1, "author1", room="hall-a", times=S_10_11)
    confirm_single_paper(client, p2, "author2", room="hall-a", times=S_11_12)

    schedule = client.get("/editor/schedule", headers=auth("editor")).json()
    assert [(s["manuscript_id"], s["starts_at"][11:16]) for s in schedule] == [
        (p1["manuscript_id"], "10:00"),
        (p2["manuscript_id"], "11:00"),
    ]

    # Swap: new slots overlap the group's own old slots (cross-wise), which is
    # allowed.  No prior arrangement is cancelled by the editor.
    swap = create_group(
        client,
        [
            arrangement(p1, room="hall-a", times=S_11_12),
            arrangement(p2, room="hall-a", times=S_10_11),
        ],
    )
    assert swap.status_code == 201, swap.text
    gid = swap.json()["group_id"]
    items = {it["manuscript_id"]: it["item_id"] for it in swap.json()["items"]}

    # While pending, old schedule is still exactly the old one.
    pending_schedule = client.get("/editor/schedule", headers=auth("editor")).json()
    assert [(s["manuscript_id"], s["starts_at"][11:16]) for s in pending_schedule] == [
        (p1["manuscript_id"], "10:00"),
        (p2["manuscript_id"], "11:00"),
    ]

    # Separate author confirmations; after the first, nothing changes yet.
    r1 = accept_item(client, items[p1["manuscript_id"]], "author1")
    assert r1.status_code == 200
    assert r1.json()["group"]["status"] == "pending"
    assert r1.json()["group"]["progress"]["accepted"] == 1
    assert len(client.get("/editor/schedule", headers=auth("editor")).json()) == 2

    r2 = accept_item(client, items[p2["manuscript_id"]], "author2")
    assert r2.status_code == 200
    assert r2.json()["group"]["status"] == "confirmed"

    schedule = client.get("/editor/schedule", headers=auth("editor")).json()
    by_ms = {s["manuscript_id"]: s for s in schedule}
    assert by_ms[p1["manuscript_id"]]["starts_at"][11:16] == "11:00"
    assert by_ms[p2["manuscript_id"]]["starts_at"][11:16] == "10:00"
    assert all(s["status"] == "confirmed" for s in schedule)
    assert all(s["group_id"] == gid for s in schedule)

    # Exactly one confirmed session per manuscript; old rows are superseded.
    history = client.get("/editor/schedule?history=true", headers=auth("editor")).json()
    superseded = [s for s in history if s["status"] == "superseded"]
    assert len(superseded) == 2
    assert all(s["end_reason"] == "superseded_by_reschedule" for s in superseded)


def test_reschedule_with_different_decision_is_locked_to_current_one(client):
    p1 = make_paper(client, author="author1")
    confirm_single_paper(client, p1, "author1")

    # Publish a second, independent decision is impossible on the same
    # revision; instead build another paper's decision and try to substitute.
    other = make_paper(client, author="author2", reviewers=("reviewer2", "reviewer3"))
    r = create_group(
        client,
        [arrangement(p1, decision=other["decision_id"], room="hall-b", times=NEXT_10_11)],
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "decision_locked"

    # Omitting decision_id works: the current decision is inherited.
    r = create_group(
        client,
        [arrangement(p1, decision=None, room="hall-b", times=NEXT_10_11)],
    )
    assert r.status_code == 201, r.text
    assert r.json()["items"][0]["decision_id"] == p1["decision_id"]


def test_same_author_two_papers_confirm_each_separately(client):
    p1 = make_paper(client, author="author1", title="first of mine")
    p2 = make_paper(
        client, author="author1", reviewers=("reviewer2", "reviewer3"), title="second of mine"
    )
    group = create_group(
        client,
        [
            arrangement(p1, room="hall-a", times=S_10_11),
            arrangement(p2, room="hall-b", times=S_11_12),
        ],
    ).json()
    i1 = next(it["item_id"] for it in group["items"] if it["manuscript_id"] == p1["manuscript_id"])
    i2 = next(it["item_id"] for it in group["items"] if it["manuscript_id"] == p2["manuscript_id"])

    # One acceptance only confirms that manuscript's item, not the whole group.
    r = accept_item(client, i1, "author1")
    assert r.status_code == 200
    assert r.json()["group"]["status"] == "pending"
    assert r.json()["group"]["progress"]["accepted"] == 1

    # The author's own queue shows one answered and one still actionable.
    grouped = client.get("/author/schedule/grouped", headers=auth("author1")).json()
    pending = grouped["pending_confirmations"]
    assert {p["item_id"]: p["can_respond"] for p in pending} == {i1: False, i2: True}

    r = accept_item(client, i2, "author1")
    assert r.json()["group"]["status"] == "confirmed"
    assert len(client.get("/editor/schedule", headers=auth("editor")).json()) == 2


def test_group_level_respond_endpoint(client):
    paper = make_paper(client)
    gid = create_group(client, [arrangement(paper)]).json()["group_id"]
    r = client.post(
        f"/author/schedule/groups/{gid}/respond",
        json={"accept": True},
        headers=auth("author1"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["group"]["status"] == "confirmed"

    # A stranger cannot resolve a group through this endpoint.
    gid2 = create_group(
        client,
        [arrangement(make_paper(client, author="author2", reviewers=("reviewer2", "reviewer3")), room="hall-b")],
    ).json()["group_id"]
    r = client.post(
        f"/author/schedule/groups/{gid2}/respond",
        json={"accept": True},
        headers=auth("author1"),
    )
    assert r.status_code == 404


def test_path_and_field_aliases_are_supported(client):
    paper = make_paper(client)
    # Bare single arrangement posted to /editor/schedule with alternate names.
    r = client.post(
        "/editor/schedule",
        json={
            "manuscript": paper["manuscript_id"],
            "decision": paper["decision_id"],
            "venue": "B",
            "start": S_10_11[0],
            "end": S_10_11[1],
            "deadline": DEADLINE,
        },
        headers=auth("editor"),
    )
    assert r.status_code == 201, r.text
    item_id = r.json()["items"][0]["item_id"]

    # Alternate respond path + field name.
    r = client.post(
        f"/author/schedule-items/{item_id}/confirm",
        json={"confirmed": True},
        headers=auth("author1"),
    )
    assert r.status_code == 200, r.text

    # Alternate query paths return the author's flat list.
    for path in ("/author/arrangements", "/author/schedules", "/author/talks"):
        assert client.get(path, headers=auth("author1")).status_code == 200


def test_new_slot_cannot_overlap_old_session_of_manuscript_outside_group(client):
    p1 = make_paper(client, author="author1")
    p2 = make_paper(client, author="author2", reviewers=("reviewer2", "reviewer3"))
    confirm_single_paper(client, p1, "author1", room="hall-a", times=S_10_11)

    # p2's new slot overlaps p1's confirmed old slot and p1 is not replaced.
    r = create_group(client, [arrangement(p2, room="hall-a", times=S_10_11)])
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Decline / expiry / repeat answers
# ---------------------------------------------------------------------------

def test_decline_terminates_whole_group_and_keeps_old_arrangements(client):
    p1 = make_paper(client, author="author1")
    p2 = make_paper(client, author="author2", reviewers=("reviewer2", "reviewer3"))
    confirm_single_paper(client, p1, "author1", room="hall-a", times=S_10_11)
    confirm_single_paper(client, p2, "author2", room="hall-b", times=S_14_15)

    group = create_group(
        client,
        [
            arrangement(p1, room="hall-a", times=S_11_12),
            arrangement(p2, room="hall-b", times=S_15_16),
        ],
    ).json()
    i1 = next(it["item_id"] for it in group["items"] if it["manuscript_id"] == p1["manuscript_id"])
    i2 = next(it["item_id"] for it in group["items"] if it["manuscript_id"] == p2["manuscript_id"])

    # author2 accepts first.
    assert accept_item(client, i2, "author2").json()["group"]["status"] == "pending"

    # author1 declines -> whole group terminates.
    r = decline_item(client, i1, "author1")
    assert r.status_code == 200
    body = r.json()
    assert body["response"] == "declined"
    assert body["group"]["status"] == "declined"
    assert body["group"]["end_reason"] == "author_declined"

    listed = client.get("/editor/schedule/groups", headers=auth("editor")).json()
    terminal = next(g for g in listed if g["group_id"] == group["group_id"])
    assert terminal["status"] == "declined"
    assert terminal["end_reason"] == "author_declined"

    # Original arrangements are untouched.
    schedule = client.get("/editor/schedule", headers=auth("editor")).json()
    by_ms = {s["manuscript_id"]: s for s in schedule}
    assert by_ms[p1["manuscript_id"]]["starts_at"][11:16] == "10:00"
    assert by_ms[p2["manuscript_id"]]["starts_at"][11:16] == "14:00"

    # Reservations released: the proposed new slot is bookable by another group.
    p3 = make_paper(client, author="author1", reviewers=("reviewer1", "reviewer3"), title="p3")
    r = create_group(client, [arrangement(p3, room="hall-a", times=S_11_12)])
    assert r.status_code == 201, r.text


def test_repeated_answers_cannot_revive_declined_or_confirmed_groups(client):
    p1 = make_paper(client, author="author1")
    group = create_group(client, [arrangement(p1)]).json()
    item_id = group["items"][0]["item_id"]

    decline_item(client, item_id, "author1")
    for method_body in ({"accept": True}, {"accept": False}):
        r = client.post(
            f"/author/schedule/items/{item_id}/respond",
            json=method_body,
            headers=auth("author1"),
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] in ("group_declined", "already_responded")

    # A confirmed group likewise rejects late repeats and stays confirmed once.
    p2 = make_paper(client, author="author2", reviewers=("reviewer2", "reviewer3"))
    g2 = create_group(client, [arrangement(p2, room="hall-b")]).json()
    i2 = g2["items"][0]["item_id"]
    assert accept_item(client, i2, "author2").status_code == 200
    r = accept_item(client, i2, "author2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "group_confirmed"
    # p1's group was declined (never a session); only p2 is confirmed.
    schedule = client.get("/editor/schedule", headers=auth("editor")).json()
    assert [s["manuscript_id"] for s in schedule] == [p2["manuscript_id"]]


def test_past_deadline_group_is_immediately_expired_and_answer_rejected(client):
    p1 = make_paper(client)
    r = create_group(client, [arrangement(p1)], deadline="2000-01-01T00:00:00+00:00")
    assert r.status_code == 201
    assert r.json()["status"] == "expired"
    assert r.json()["end_reason"] == "deadline_expired"
    item_id = r.json()["items"][0]["item_id"]

    r = accept_item(client, item_id, "author1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "confirmation_expired"

    # Expired group does not block a fresh pending group for the manuscript.
    r = create_group(client, [arrangement(p1, room="hall-b", times=NEXT_10_11)])
    assert r.status_code == 201, r.text


def test_deadline_expiry_with_sleep_releases_reservation_and_blocks_late_answer(client):
    p1 = make_paper(client, author="author1")
    p2 = make_paper(client, author="author2", reviewers=("reviewer2", "reviewer3"))
    near = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(timespec="seconds")
    group = create_group(client, [arrangement(p1, times=NEXT_10_11)], deadline=near).json()
    item_id = group["items"][0]["item_id"]

    time.sleep(1.2)

    # Editor query observes the terminal end reason.
    detail = client.get(
        f"/editor/schedule/groups/{group['group_id']}", headers=auth("editor")
    ).json()
    assert detail["status"] == "expired"
    assert detail["end_reason"] == "deadline_expired"

    # A late answer cannot revive it.
    r = accept_item(client, item_id, "author1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "confirmation_expired"

    # Reservation released.
    r = create_group(client, [arrangement(p2, times=NEXT_10_11)])
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Conflict withdrawal interaction
# ---------------------------------------------------------------------------

def test_conflict_withdrawal_voids_session_and_frees_slot(client):
    p1 = make_paper(client, author="author1")
    confirm_single_paper(client, p1, "author1", room="hall-a", times=S_10_11)

    r = conflict(client, "author1", "reviewer1")
    assert r.status_code == 201
    effects = r.json()
    assert len(effects["voided_session_ids"]) == 1

    assert client.get("/editor/schedule", headers=auth("editor")).json() == []
    history = client.get("/editor/schedule?history=true", headers=auth("editor")).json()
    assert history[0]["status"] == "voided"
    assert history[0]["end_reason"] == "decision_withdrawn"

    # Author calendar no longer shows the talk.
    grouped = client.get("/author/schedule/grouped", headers=auth("author1")).json()
    assert grouped["confirmed_sessions"] == []

    # The room/author slot is free for another paper.
    p2 = make_paper(client, author="author2", reviewers=("reviewer2", "reviewer3"))
    r = create_group(client, [arrangement(p2, room="hall-a", times=S_10_11)])
    assert r.status_code == 201, r.text


def test_withdrawal_cancels_pending_group_but_keeps_other_old_session(client):
    p1 = make_paper(client, author="author1")
    p2 = make_paper(client, author="author2", reviewers=("reviewer2", "reviewer3"))
    confirm_single_paper(client, p1, "author1", room="hall-a", times=S_10_11)
    confirm_single_paper(client, p2, "author2", room="hall-b", times=S_14_15)

    group = create_group(
        client,
        [
            arrangement(p1, room="hall-a", times=S_11_12),
            arrangement(p2, room="hall-b", times=S_15_16),
        ],
    ).json()
    i1 = next(it["item_id"] for it in group["items"] if it["manuscript_id"] == p1["manuscript_id"])

    r = conflict(client, "author1", "reviewer1")
    assert r.status_code == 201
    body = r.json()
    assert group["group_id"] in body["cancelled_schedule_group_ids"]
    assert len(body["voided_session_ids"]) == 1  # p1 old session

    detail = client.get(
        f"/editor/schedule/groups/{group['group_id']}", headers=auth("editor")
    ).json()
    assert detail["status"] == "cancelled"
    assert detail["end_reason"] == "decision_withdrawn"

    # Author1 can no longer confirm.
    r = accept_item(client, i1, "author1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "group_cancelled"

    # p2's existing arrangement is exactly preserved.
    schedule = client.get("/editor/schedule", headers=auth("editor")).json()
    assert len(schedule) == 1
    assert schedule[0]["manuscript_id"] == p2["manuscript_id"]
    assert schedule[0]["room"] == "hall-b"
    assert schedule[0]["starts_at"][11:16] == "14:00"

    # No reservations remain.
    occ = client.get("/editor/schedule/occupancy", headers=auth("editor")).json()
    assert [o for o in occ if o["kind"] == "reservation"] == []


def test_new_decision_does_not_revive_old_session_or_consent(client):
    p1 = make_paper(client, author="author1")
    group, respond_body = confirm_single_paper(client, p1, "author1")
    item_id = group["items"][0]["item_id"]

    conflict(client, "author1", "reviewer1")
    assert client.get("/editor/schedule", headers=auth("editor")).json() == []

    # A later replacement decision (reviewer2 + fresh reviewer3) is published.
    from conftest import reviewer_user_id

    inv3 = invite(client, p1["revision_id"], "reviewer3")
    rv3 = accept_and_review(client, inv3["invitation_id"], "reviewer3", "fresh", 5)
    new_decision = publish_two_review_decision(
        client,
        p1["revision_id"],
        [p1["review_ids"][1], rv3["review_id"]],
        conclusion="accept now",
    )

    # Nothing automatically came back.
    assert client.get("/editor/schedule", headers=auth("editor")).json() == []
    history = client.get("/editor/schedule?history=true", headers=auth("editor")).json()
    assert all(s["status"] != "confirmed" for s in history)

    # The old accepted answer cannot recreate the session.
    r = accept_item(client, item_id, "author1")
    assert r.status_code == 409

    grouped = client.get("/author/schedule/grouped", headers=auth("author1")).json()
    assert grouped["confirmed_sessions"] == []

    # Editor must make a fresh arrangement based on the new decision.
    r = create_group(
        client,
        [arrangement({**p1, "decision_id": new_decision["decision_id"]}, room="hall-b", times=NEXT_10_11)],
    )
    assert r.status_code == 201, r.text


def test_rooms_are_read_from_local_config_file(client, tmp_path, monkeypatch):
    custom = tmp_path / "custom_rooms.json"
    custom.write_text(
        json.dumps({"rooms": [{"id": 7, "code": "salon-7", "name": "Salon 7"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ROOMS_CONFIG_PATH", str(custom))

    rooms = client.get("/editor/rooms", headers=auth("editor")).json()
    assert [r["code"] for r in rooms] == ["salon-7"]

    paper = make_paper(client)
    r = create_group(client, [arrangement(paper, room="hall-a")])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unknown_room"

    r = create_group(client, [arrangement(paper, room="salon-7")])
    assert r.status_code == 201, r.text
