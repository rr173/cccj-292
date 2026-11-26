"""Audience seat reservations: registration, waitlist/seat ordering,
reschedule/capacity re-evaluation, withdrawal, freeze-at-start and races.
"""

from __future__ import annotations

import threading
import time
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

DAY0 = datetime(2030, 6, 3, tzinfo=timezone.utc)
DEADLINE = (DAY0 - timedelta(days=30)).isoformat(timespec="seconds")


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return DAY0.replace(day=DAY0.day + day, hour=hour, minute=minute)


# Talk slots (all far future).
T_10_11 = (iso(at(0, 10)), iso(at(0, 11)))
T_1030_1130 = (iso(at(0, 10, 30)), iso(at(0, 11, 30)))
T_11_12 = (iso(at(0, 11)), iso(at(0, 12)))
T_14_15 = (iso(at(0, 14)), iso(at(0, 15)))
T_15_16 = (iso(at(0, 15)), iso(at(0, 16)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_paper(client, author="author1", reviewers=("reviewer1", "reviewer2"), title=None):
    m = create_manuscript(client, author=author, title=title or f"paper-{author}")
    revision_id = m["first_revision"]["revision_id"]
    review_ids = []
    for reviewer in reviewers:
        inv = invite(client, revision_id, reviewer)
        out = accept_and_review(client, inv["invitation_id"], reviewer, f"review by {reviewer}", 4)
        review_ids.append(out["review_id"])
    decision = publish_two_review_decision(client, revision_id, review_ids)
    return {
        "manuscript_id": m["manuscript_id"],
        "revision_id": revision_id,
        "decision_id": decision["decision_id"],
    }


def confirm_talk(client, paper, author="author1", room="hall-a", times=T_10_11):
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
    ar = client.post(
        f"/author/schedule/items/{item_id}/respond",
        json={"accept": True},
        headers=auth(author),
    )
    assert ar.status_code == 200, ar.text
    return r.json()


def reschedule(client, paper, author, room, times, deadline=DEADLINE):
    """Propose + fully accept a reschedule group for one talk."""
    start, end = times
    r = client.post(
        "/editor/schedule/groups",
        json={
            "confirmation_deadline": deadline,
            "items": [
                {
                    "manuscript_id": paper["manuscript_id"],
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
    ar = client.post(
        f"/author/schedule/items/{item_id}/respond",
        json={"accept": True},
        headers=auth(author),
    )
    assert ar.status_code == 200, ar.text
    return r.json()


def set_capacity(client, room, capacity):
    r = client.put(
        f"/editor/rooms/{room}/audience-capacity",
        json={"capacity": capacity},
        headers=auth("editor"),
    )
    assert r.status_code == 200, r.text
    return r.json()


def register(client, talk_id, user):
    return client.post(f"/audience/talks/{talk_id}/register", headers=auth(user))


def cancel(client, talk_id, user):
    return client.post(f"/audience/talks/{talk_id}/cancel", headers=auth(user))


def admissions(client, talk_id):
    r = client.get(f"/editor/talks/{talk_id}/admissions", headers=auth("editor"))
    assert r.status_code == 200, r.text
    return r.json()


def seated_usernames(payload):
    return [row["audience"]["username"] for row in payload["seated"]]


def waitlist_usernames(payload):
    return [row["audience"]["username"] for row in payload["waitlist"]]


def my_status(client, talk_id, user):
    talks = client.get("/audience/talks", headers=auth(user)).json()
    mine = [t for t in talks if t["manuscript_id"] == talk_id]
    assert len(mine) == 1
    return mine[0]


# ---------------------------------------------------------------------------
# Basics: capacity default, registration, ordering
# ---------------------------------------------------------------------------

def test_rooms_list_shows_default_audience_capacity_zero(client):
    rooms = client.get("/editor/rooms", headers=auth("editor")).json()
    assert rooms, "expected configured rooms"
    assert all(room["audience_capacity"] == 0 for room in rooms)


def test_default_capacity_zero_waitlists(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    talk_id = paper["manuscript_id"]

    r = register(client, talk_id, "reviewer1")
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "waitlisted"
    assert r.json()["waitlist_position"] == 1

    view = my_status(client, talk_id, "reviewer2")
    assert view["audience_capacity"] == 0
    assert view["seats_available"] == 0
    assert view["waitlist_count"] == 1
    assert view["open_for_registration"] is True


def test_capacity_set_promotes_in_registration_order(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    talk_id = paper["manuscript_id"]
    for user in ("reviewer1", "reviewer2", "reviewer3"):
        register(client, talk_id, user)

    set_capacity(client, "hall-a", 2)
    adm = admissions(client, talk_id)
    assert seated_usernames(adm) == ["reviewer1", "reviewer2"]
    assert waitlist_usernames(adm) == ["reviewer3"]
    assert adm["seats_available"] == 0
    assert [w["position"] for w in adm["waitlist"]] == [1]


def test_seat_immediately_when_available(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    set_capacity(client, "hall-a", 1)
    talk_id = paper["manuscript_id"]

    r = register(client, talk_id, "reviewer1")
    assert r.json()["status"] == "seated"
    assert r.json()["seated_at"] is not None
    r = register(client, talk_id, "reviewer2")
    assert r.json()["status"] == "waitlisted"


def test_duplicate_registration_keeps_order(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    talk_id = paper["manuscript_id"]
    register(client, talk_id, "reviewer1")
    register(client, talk_id, "reviewer2")
    register(client, talk_id, "reviewer3")

    first = register(client, talk_id, "reviewer1").json()
    again = register(client, talk_id, "reviewer1")
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["already_registered"] is True
    assert body["registration_id"] == first["registration_id"]
    assert body["registered_at"] == first["registered_at"]
    assert body["waitlist_position"] == 1  # order unchanged

    adm = admissions(client, talk_id)
    assert waitlist_usernames(adm) == ["reviewer1", "reviewer2", "reviewer3"]


def test_cancel_then_reregister_goes_to_the_back(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    talk_id = paper["manuscript_id"]
    register(client, talk_id, "reviewer1")
    register(client, talk_id, "reviewer2")
    register(client, talk_id, "reviewer3")

    r = cancel(client, talk_id, "reviewer1")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"

    r = register(client, talk_id, "reviewer1")
    assert r.status_code == 201, r.text
    assert r.json()["waitlist_position"] == 3  # queued behind everyone

    adm = admissions(client, talk_id)
    assert waitlist_usernames(adm) == ["reviewer2", "reviewer3", "reviewer1"]


def test_cancel_without_registration_is_404(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    r = cancel(client, paper["manuscript_id"], "reviewer1")
    assert r.status_code == 404, r.text


def test_register_requires_confirmed_talk(client):
    paper = make_paper(client)  # decided but not scheduled
    r = register(client, paper["manuscript_id"], "reviewer1")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "not_open"

    r = register(client, 99999, "reviewer1")
    assert r.status_code == 404, r.text


def test_admissions_unknown_or_unscheduled(client):
    paper = make_paper(client)  # decided but not scheduled
    r = client.get(
        f"/editor/talks/{paper['manuscript_id']}/admissions", headers=auth("editor")
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_scheduled"

    r = client.get("/editor/talks/99999/admissions", headers=auth("editor"))
    assert r.status_code == 404, r.text
    r = client.get("/editor/sessions/99999/admissions", headers=auth("editor"))
    assert r.status_code == 404, r.text


def test_capacity_validation(client):
    r = client.put(
        "/editor/rooms/no-such-room/audience-capacity",
        json={"capacity": 1},
        headers=auth("editor"),
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "unknown_room"

    r = client.put(
        "/editor/rooms/hall-a/audience-capacity",
        json={"capacity": -1},
        headers=auth("editor"),
    )
    assert r.status_code == 422, r.text

    # Non-editors may not manage capacity.
    r = client.put(
        "/editor/rooms/hall-a/audience-capacity",
        json={"capacity": 1},
        headers=auth("reviewer1"),
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Overlap rules
# ---------------------------------------------------------------------------

def _two_overlapping_talks(client, cap1=1, cap2=1):
    p1 = make_paper(client, "author1", ("reviewer1", "reviewer2"), "talk-one")
    p2 = make_paper(client, "author2", ("reviewer2", "reviewer3"), "talk-two")
    confirm_talk(client, p1, "author1", "hall-a", T_10_11)
    confirm_talk(client, p2, "author2", "hall-b", T_1030_1130)
    set_capacity(client, "hall-a", cap1)
    set_capacity(client, "hall-b", cap2)
    return p1, p2


def test_waitlist_allowed_for_overlap_but_no_double_seat(client):
    p1, p2 = _two_overlapping_talks(client)
    t1, t2 = p1["manuscript_id"], p2["manuscript_id"]

    assert register(client, t1, "author2").json()["status"] == "seated"
    # Same person, overlapping talk: may waitlist, must not get a seat.
    r = register(client, t2, "author2")
    assert r.json()["status"] == "waitlisted"
    # Someone without a clash takes the seat instead (skip the clasher).
    assert register(client, t2, "reviewer1").json()["status"] == "seated"

    # Cancelling the t1 seat makes author2 eligible, but t2 is full and
    # confirmed seats are never bumped by an earlier waitlisted applicant.
    cancel(client, t1, "author2")
    assert my_status(client, t2, "author2")["my_registration"]["status"] == "waitlisted"
    assert seated_usernames(admissions(client, t2)) == ["reviewer1"]

    # Once reviewer1 frees the t2 seat, author2 is promoted.
    cancel(client, t2, "reviewer1")
    assert my_status(client, t2, "author2")["my_registration"]["status"] == "seated"


def test_back_to_back_talks_are_not_an_overlap(client):
    p1 = make_paper(client, "author1", ("reviewer1", "reviewer2"), "talk-one")
    p2 = make_paper(client, "author2", ("reviewer2", "reviewer3"), "talk-two")
    confirm_talk(client, p1, "author1", "hall-a", T_10_11)
    confirm_talk(client, p2, "author2", "hall-a", T_11_12)  # starts when t1 ends
    set_capacity(client, "hall-a", 1)

    assert register(client, p1["manuscript_id"], "reviewer1").json()["status"] == "seated"
    assert register(client, p2["manuscript_id"], "reviewer1").json()["status"] == "seated"


def test_promotion_order_across_talks_follows_registration_order(client):
    """One person waitlisted in two overlapping talks: the earlier
    registration wins the seat when both talks could take them."""
    p1, p2 = _two_overlapping_talks(client, cap1=0, cap2=0)
    t1, t2 = p1["manuscript_id"], p2["manuscript_id"]
    # reviewer1 registers for t2 first, then t1.
    register(client, t2, "reviewer1")
    register(client, t1, "reviewer1")

    set_capacity(client, "hall-a", 1)  # t1 gets a seat first (later registration)
    assert my_status(client, t1, "reviewer1")["my_registration"]["status"] == "seated"
    set_capacity(client, "hall-b", 1)  # t2: clashes with the t1 seat now
    assert my_status(client, t2, "reviewer1")["my_registration"]["status"] == "waitlisted"


def test_cancel_in_one_talk_unblocks_waitlist_in_another(client):
    p1, p2 = _two_overlapping_talks(client)
    t1, t2 = p1["manuscript_id"], p2["manuscript_id"]
    register(client, t1, "author2")          # seated in t1
    register(client, t2, "author2")          # waitlisted in t2 (clash)

    cancel(client, t1, "author2")            # frees the overlap block
    assert my_status(client, t2, "author2")["my_registration"]["status"] == "seated"


def test_withdrawal_propagates_to_several_talks_in_one_pass(client):
    """One talk's cancellation frees one person's way into several other
    talks at once; the same promotion pass seats them everywhere eligible."""
    pa = make_paper(client, "author1", ("reviewer1", "reviewer2"), "talk-a")
    pb = make_paper(client, "author2", ("reviewer2", "reviewer3"), "talk-b")
    pc = make_paper(client, "author2", ("reviewer2", "reviewer3"), "talk-c")
    # A long talk by author1; B and C by author2 sit inside A's span but do
    # not overlap each other.
    confirm_talk(client, pa, "author1", "hall-a", (iso(at(0, 10)), iso(at(0, 13))))
    confirm_talk(client, pb, "author2", "hall-b", T_1030_1130)
    confirm_talk(client, pc, "author2", "hall-b", (iso(at(0, 12)), iso(at(0, 13))))
    set_capacity(client, "hall-a", 1)
    set_capacity(client, "hall-b", 1)
    ta, tb, tc = (p["manuscript_id"] for p in (pa, pb, pc))

    register(client, ta, "reviewer1")   # seated in A (10:00-13:00)
    register(client, tb, "reviewer1")   # waitlisted in B (clash with A)
    register(client, tc, "reviewer1")   # waitlisted in C (clash with A)

    _withdraw(client, "author1", "reviewer1")
    # The single pass after the withdrawal seats reviewer1 in both B and C.
    assert my_status(client, tb, "reviewer1")["my_registration"]["status"] == "seated"
    assert my_status(client, tc, "reviewer1")["my_registration"]["status"] == "seated"


# ---------------------------------------------------------------------------
# Reschedule re-evaluation
# ---------------------------------------------------------------------------

def test_confirmed_reschedule_preserves_registrations(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    set_capacity(client, "hall-a", 1)
    talk_id = paper["manuscript_id"]
    reg_seated = register(client, talk_id, "reviewer1").json()
    register(client, talk_id, "reviewer2")

    reschedule(client, paper, "author1", "hall-a", T_14_15)

    adm = admissions(client, talk_id)
    assert adm["session"]["starts_at"] == T_14_15[0]
    assert seated_usernames(adm) == ["reviewer1"]
    assert waitlist_usernames(adm) == ["reviewer2"]
    # Same application rows — nothing was copied by hand.
    assert adm["seated"][0]["registration_id"] == reg_seated["registration_id"]


def test_pending_reschedule_does_not_affect_registrations(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    set_capacity(client, "hall-a", 1)
    talk_id = paper["manuscript_id"]
    register(client, talk_id, "reviewer1")
    register(client, talk_id, "reviewer2")

    # Propose (but do not confirm) a move to a zero-capacity room.
    r = client.post(
        "/editor/schedule/groups",
        json={
            "confirmation_deadline": DEADLINE,
            "items": [
                {
                    "manuscript_id": talk_id,
                    "room": "room-101",
                    "starts_at": T_14_15[0],
                    "ends_at": T_14_15[1],
                }
            ],
        },
        headers=auth("editor"),
    )
    assert r.status_code == 201, r.text

    adm = admissions(client, talk_id)
    assert seated_usernames(adm) == ["reviewer1"]
    assert waitlist_usernames(adm) == ["reviewer2"]
    assert adm["session"]["starts_at"] == T_10_11[0]  # still the old slot


def test_reschedule_into_clash_demotes_later_registration(client):
    p1, p2 = _two_overlapping_talks(client, cap1=1, cap2=1)
    # Move t2 away from t1 first: put t2 at 14:00 so reviewer1 can hold both.
    reschedule(client, p2, "author2", "hall-b", T_14_15)
    t1, t2 = p1["manuscript_id"], p2["manuscript_id"]
    register(client, t1, "reviewer1")  # earlier registration
    register(client, t2, "reviewer1")  # later registration
    assert my_status(client, t2, "reviewer1")["my_registration"]["status"] == "seated"

    # Move t2 back into t1's slot: the later registration loses its seat.
    reschedule(client, p2, "author2", "hall-b", T_1030_1130)
    assert my_status(client, t1, "reviewer1")["my_registration"]["status"] == "seated"
    view = my_status(client, t2, "reviewer1")
    assert view["my_registration"]["status"] == "waitlisted"
    assert view["my_registration"]["waitlist_position"] == 1


def test_reschedule_to_smaller_room_uses_new_capacity(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    set_capacity(client, "hall-a", 2)
    set_capacity(client, "room-202", 1)
    talk_id = paper["manuscript_id"]
    register(client, talk_id, "reviewer1")
    register(client, talk_id, "reviewer2")

    reschedule(client, paper, "author1", "room-202", T_14_15)
    adm = admissions(client, talk_id)
    assert adm["audience_capacity"] == 1
    assert seated_usernames(adm) == ["reviewer1"]
    assert waitlist_usernames(adm) == ["reviewer2"]


def test_multi_talk_group_reschedule_reevaluates_all(client):
    p1 = make_paper(client, "author1", ("reviewer1", "reviewer2"), "talk-one")
    p2 = make_paper(client, "author2", ("reviewer2", "reviewer3"), "talk-two")
    confirm_talk(client, p1, "author1", "hall-a", T_10_11)
    confirm_talk(client, p2, "author2", "hall-a", T_11_12)
    set_capacity(client, "hall-a", 1)
    t1, t2 = p1["manuscript_id"], p2["manuscript_id"]
    register(client, t1, "reviewer1")
    register(client, t2, "reviewer3")

    # Linked swap of the two slots in one group.
    r = client.post(
        "/editor/schedule/groups",
        json={
            "confirmation_deadline": DEADLINE,
            "items": [
                {"manuscript_id": t1, "room": "hall-a",
                 "starts_at": T_11_12[0], "ends_at": T_11_12[1]},
                {"manuscript_id": t2, "room": "hall-a",
                 "starts_at": T_10_11[0], "ends_at": T_10_11[1]},
            ],
        },
        headers=auth("editor"),
    )
    assert r.status_code == 201, r.text
    items = {it["manuscript_id"]: it["item_id"] for it in r.json()["items"]}
    for talk_id, author in ((t1, "author1"), (t2, "author2")):
        ar = client.post(
            f"/author/schedule/items/{items[talk_id]}/respond",
            json={"accept": True},
            headers=auth(author),
        )
        assert ar.status_code == 200, ar.text

    adm1 = admissions(client, t1)
    adm2 = admissions(client, t2)
    assert adm1["session"]["starts_at"] == T_11_12[0]
    assert adm2["session"]["starts_at"] == T_10_11[0]
    assert seated_usernames(adm1) == ["reviewer1"]
    assert seated_usernames(adm2) == ["reviewer3"]


# ---------------------------------------------------------------------------
# Capacity changes
# ---------------------------------------------------------------------------

def test_capacity_shrink_demotes_by_order_and_keeps_waitlist_order(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    set_capacity(client, "hall-a", 3)
    talk_id = paper["manuscript_id"]
    for user in ("reviewer1", "reviewer2", "reviewer3"):
        assert register(client, talk_id, user).json()["status"] == "seated"
    register(client, talk_id, "author2")  # waitlisted

    set_capacity(client, "hall-a", 1)
    adm = admissions(client, talk_id)
    assert seated_usernames(adm) == ["reviewer1"]
    # Demoted seats return to the waitlist in their original order.
    assert waitlist_usernames(adm) == ["reviewer2", "reviewer3", "author2"]
    assert [w["position"] for w in adm["waitlist"]] == [1, 2, 3]


def test_capacity_growth_promotes_skipping_clashers(client):
    p1, p2 = _two_overlapping_talks(client, cap1=1, cap2=0)
    t1, t2 = p1["manuscript_id"], p2["manuscript_id"]
    register(client, t1, "author2")       # seated in t1
    register(client, t2, "author2")       # waitlisted (clash with t1 seat)
    register(client, t2, "reviewer1")     # waitlisted
    register(client, t2, "reviewer2")     # waitlisted

    set_capacity(client, "hall-b", 2)
    adm = admissions(client, t2)
    # author2 is skipped (still seated in the overlapping t1); the other two
    # are promoted in registration order.
    assert seated_usernames(adm) == ["reviewer1", "reviewer2"]
    assert waitlist_usernames(adm) == ["author2"]


# ---------------------------------------------------------------------------
# Decision withdrawal
# ---------------------------------------------------------------------------

def _withdraw(client, author="author1", reviewer="reviewer1"):
    r = client.post(
        "/editor/conflicts",
        json={
            "author_id": author_user_id(client, author),
            "reviewer_id": reviewer_user_id(client, reviewer),
            "reason": "shared lab",
        },
        headers=auth("editor"),
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_withdrawal_terminates_registrations_and_propagates(client):
    p1, p2 = _two_overlapping_talks(client, cap1=1, cap2=1)
    t1, t2 = p1["manuscript_id"], p2["manuscript_id"]
    register(client, t1, "author2")   # seated in t1
    register(client, t2, "author2")   # waitlisted in t2 (clash)
    register(client, t1, "reviewer1") # waitlisted in t1

    effects = _withdraw(client, "author1", "reviewer1")
    assert len(effects["voided_session_ids"]) == 1
    assert len(effects["ended_registration_ids"]) == 2

    # t1's list is terminated; the freed overlap block promotes author2 in t2.
    adm1 = admissions(client, t1)
    assert adm1["seated"] == [] and adm1["waitlist"] == []
    assert my_status(client, t2, "author2")["my_registration"]["status"] == "seated"

    # The talk is gone from the audience board; new registrations are refused.
    assert all(t["manuscript_id"] != t1 for t in client.get(
        "/audience/talks", headers=auth("reviewer1")).json())
    r = register(client, t1, "reviewer3")
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_open"


def test_new_decision_does_not_inherit_old_registrations(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    set_capacity(client, "hall-a", 1)
    talk_id = paper["manuscript_id"]
    register(client, talk_id, "reviewer3")
    register(client, talk_id, "author2")

    _withdraw(client, "author1", "reviewer1")

    # A fresh decision on the same revision (reviewer1's review is dead).
    revision_id = paper["revision_id"]
    inv = invite(client, revision_id, "reviewer3")
    out = accept_and_review(client, inv["invitation_id"], "reviewer3", "third look", 4)
    reviews = client.get(f"/editor/manuscripts/{talk_id}", headers=auth("editor")).json()
    live_review_ids = [
        inv_row["review"]["review_id"]
        for inv_row in reviews["invitations"]
        if inv_row["review"] is not None and inv_row["reviewer"]["username"] != "reviewer1"
    ]
    assert out["review_id"] in live_review_ids
    decision = publish_two_review_decision(client, revision_id, live_review_ids[:2])
    paper2 = {"manuscript_id": talk_id, "decision_id": decision["decision_id"]}
    confirm_talk(client, paper2, "author1", "hall-a", T_10_11)

    # Old applications stay ended; nobody is carried over.
    adm = admissions(client, talk_id)
    assert adm["seated"] == [] and adm["waitlist"] == []
    mine = client.get("/audience/registrations", headers=auth("reviewer3")).json()
    assert any(row["status"] == "ended" for row in mine)

    # Re-registering starts a brand-new application under the new decision.
    r = register(client, talk_id, "reviewer3")
    assert r.status_code == 201 and r.json()["status"] == "seated"


# ---------------------------------------------------------------------------
# Freeze at talk start
# ---------------------------------------------------------------------------

def test_freeze_lifecycle(client):
    set_capacity(client, "hall-a", 1)
    set_capacity(client, "hall-b", 0)
    p1 = make_paper(client, "author1", ("reviewer1", "reviewer2"), "early-talk")
    p2 = make_paper(client, "author2", ("reviewer2", "reviewer3"), "later-talk")

    now = datetime.now(timezone.utc)
    start1 = now + timedelta(seconds=6)
    slot1 = (iso(start1), iso(start1 + timedelta(hours=1)))
    slot2 = (iso(start1 + timedelta(minutes=30)), iso(start1 + timedelta(minutes=90)))

    def tight_group(paper, author, room, slot):
        deadline = iso(start1 - timedelta(seconds=1))
        r = client.post(
            "/editor/schedule/groups",
            json={
                "confirmation_deadline": deadline,
                "items": [
                    {
                        "manuscript_id": paper["manuscript_id"],
                        "decision_id": paper["decision_id"],
                        "room": room,
                        "starts_at": slot[0],
                        "ends_at": slot[1],
                    }
                ],
            },
            headers=auth("editor"),
        )
        assert r.status_code == 201, r.text
        item_id = r.json()["items"][0]["item_id"]
        ar = client.post(
            f"/author/schedule/items/{item_id}/respond",
            json={"accept": True},
            headers=auth(author),
        )
        assert ar.status_code == 200, ar.text

    tight_group(p1, "author1", "hall-a", slot1)
    tight_group(p2, "author2", "hall-b", slot2)
    t1, t2 = p1["manuscript_id"], p2["manuscript_id"]

    # Before the start: normal registration behaviour.
    assert register(client, t1, "reviewer1").json()["status"] == "seated"
    assert register(client, t1, "reviewer2").json()["status"] == "waitlisted"
    assert register(client, t2, "reviewer1").json()["status"] == "waitlisted"  # clash

    time.sleep(max(0.0, (start1 - datetime.now(timezone.utc)).total_seconds()) + 1.0)

    # The talk has started: no registration, cancellation or promotion.
    assert register(client, t1, "reviewer3").status_code == 409
    assert register(client, t1, "reviewer3").json()["error"]["code"] == "session_started"
    assert cancel(client, t1, "reviewer1").status_code == 409

    set_capacity(client, "hall-a", 5)  # capacity change must not rewrite it
    adm = admissions(client, t1)
    assert adm["frozen"] is True
    assert seated_usernames(adm) == ["reviewer1"]
    assert waitlist_usernames(adm) == ["reviewer2"]

    # The frozen seat still blocks the overlapping t2.
    set_capacity(client, "hall-b", 1)
    assert my_status(client, t2, "reviewer1")["my_registration"]["status"] == "waitlisted"

    # Withdrawing the decision afterwards does not rewrite the frozen list,
    # and the frozen seat keeps blocking by its original time slot.
    _withdraw(client, "author1", "reviewer1")
    adm = admissions(client, t1)
    assert adm["frozen"] is True
    assert adm["session"]["session_status"] == "voided"
    assert seated_usernames(adm) == ["reviewer1"]
    assert waitlist_usernames(adm) == ["reviewer2"]
    assert my_status(client, t2, "reviewer1")["my_registration"]["status"] == "waitlisted"


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

def test_audience_never_sees_other_registrations(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    set_capacity(client, "hall-a", 1)
    talk_id = paper["manuscript_id"]
    register(client, talk_id, "reviewer1")
    register(client, talk_id, "reviewer2")

    talks = client.get("/audience/talks", headers=auth("reviewer3")).json()
    assert talks, "expected at least one talk"
    for talk in talks:
        assert "seated" not in talk and "waitlist" not in talk
        assert "registrations" not in talk
        assert talk["my_registration"] is None

    mine = client.get("/audience/registrations", headers=auth("reviewer2")).json()
    assert len(mine) == 1
    assert mine[0]["manuscript_id"] == talk_id
    assert "audience" not in mine[0]  # own row needs no identity field

    # Admission lists are editor-only.
    for user in ("author1", "reviewer1"):
        r = client.get(f"/editor/talks/{talk_id}/admissions", headers=auth(user))
        assert r.status_code == 403, r.text
    r = client.get(f"/editor/talks/{talk_id}/admissions")
    assert r.status_code == 401, r.text


def test_audience_endpoints_require_authentication(client):
    assert client.get("/audience/talks").status_code == 401
    assert client.get("/audience/registrations").status_code == 401
    assert client.post("/audience/talks/1/register").status_code == 401


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def _fresh_client():
    return TestClient(create_app(app_config.DB_PATH))


def test_concurrent_registrations_never_exceed_capacity(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    set_capacity(client, "hall-a", 2)
    talk_id = paper["manuscript_id"]
    users = ["editor", "author1", "author2", "reviewer1", "reviewer2", "reviewer3"]

    barrier = threading.Barrier(len(users))
    results: dict[str, tuple[int, str]] = {}

    def do_register(user):
        c = _fresh_client()
        barrier.wait()
        r = c.post(f"/audience/talks/{talk_id}/register", headers=auth(user))
        results[user] = (r.status_code, r.json().get("status"))

    threads = [threading.Thread(target=do_register, args=(u,)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(status == 201 for status, _ in results.values()), results
    adm = admissions(client, talk_id)
    assert adm["seated_count"] == 2
    assert adm["waitlist_count"] == 4
    # The seats went to the two earliest applications in registration order.
    all_ids = [row["registration_id"] for row in adm["seated"] + adm["waitlist"]]
    assert [row["registration_id"] for row in adm["seated"]] == sorted(all_ids)[:2]


def test_concurrent_cancel_and_register_keeps_order(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    set_capacity(client, "hall-a", 1)
    talk_id = paper["manuscript_id"]
    register(client, talk_id, "reviewer1")  # seated
    register(client, talk_id, "reviewer2")  # waitlisted first
    register(client, talk_id, "reviewer3")  # waitlisted second

    barrier = threading.Barrier(2)
    outcomes: dict[str, int] = {}

    def do_cancel():
        c = _fresh_client()
        barrier.wait()
        outcomes["cancel"] = c.post(
            f"/audience/talks/{talk_id}/cancel", headers=auth("reviewer1")
        ).status_code

    def do_register():
        c = _fresh_client()
        barrier.wait()
        outcomes["register"] = c.post(
            f"/audience/talks/{talk_id}/register", headers=auth("author2")
        ).status_code

    t1 = threading.Thread(target=do_cancel)
    t2 = threading.Thread(target=do_register)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert outcomes["cancel"] == 200 and outcomes["register"] == 201, outcomes
    # Either ordering frees exactly one seat, and the earliest waitlisted
    # application always wins it.
    adm = admissions(client, talk_id)
    assert seated_usernames(adm) == ["reviewer2"]
    assert waitlist_usernames(adm) == ["reviewer3", "author2"]


def test_concurrent_capacity_change_and_registration_stays_consistent(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    talk_id = paper["manuscript_id"]
    register(client, talk_id, "reviewer1")
    register(client, talk_id, "reviewer2")

    barrier = threading.Barrier(2)
    outcomes: dict[str, int] = {}

    def shrink():
        c = _fresh_client()
        barrier.wait()
        outcomes["capacity"] = c.put(
            "/editor/rooms/hall-a/audience-capacity",
            json={"capacity": 1},
            headers=auth("editor"),
        ).status_code

    def register_late():
        c = _fresh_client()
        barrier.wait()
        outcomes["register"] = c.post(
            f"/audience/talks/{talk_id}/register", headers=auth("reviewer3")
        ).status_code

    t1 = threading.Thread(target=shrink)
    t2 = threading.Thread(target=register_late)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert outcomes["capacity"] == 200 and outcomes["register"] == 201, outcomes
    adm = admissions(client, talk_id)
    # Whatever the interleaving, the final list obeys the capacity and order.
    assert adm["seated_count"] <= 1
    assert seated_usernames(adm) == ["reviewer1"]
    assert waitlist_usernames(adm) == ["reviewer2", "reviewer3"]


def test_concurrent_register_and_withdrawal_leaves_no_active_registration(client):
    paper = make_paper(client)
    confirm_talk(client, paper)
    set_capacity(client, "hall-a", 2)
    talk_id = paper["manuscript_id"]

    barrier = threading.Barrier(2)
    outcomes: dict[str, int] = {}

    def do_register():
        c = _fresh_client()
        barrier.wait()
        r = c.post(f"/audience/talks/{talk_id}/register", headers=auth("reviewer3"))
        outcomes["register"] = r.status_code

    def do_withdraw():
        c = _fresh_client()
        barrier.wait()
        outcomes["withdraw"] = c.post(
            "/editor/conflicts",
            json={
                "author_id": author_user_id(client, "author1"),
                "reviewer_id": reviewer_user_id(client, "reviewer1"),
                "reason": "race",
            },
            headers=auth("editor"),
        ).status_code

    t1 = threading.Thread(target=do_register)
    t2 = threading.Thread(target=do_withdraw)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert outcomes["withdraw"] == 201, outcomes
    assert outcomes["register"] in (201, 409), outcomes
    # Whatever won, no active registration survives for the withdrawn talk.
    adm = admissions(client, talk_id)
    assert adm["seated"] == [] and adm["waitlist"] == []
