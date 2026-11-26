#!/usr/bin/env python3
"""End-to-end scheduling / linked-rescheduling walkthrough over HTTP.

Story:
  1. Two accepted papers get initial talk slots; each author confirms alone.
  2. The editor swaps their consecutive slots in one linked group.  The old
     schedule stays valid while both authors decide, and the new slots are
     reserved immediately.
  3. Both authors confirm separately -> the swap applies atomically.
  4. A further change request is declined, so the group dies and the current
     schedule is untouched; the editor sees the end reason.

Start a server first (with a fresh database for a clean run):
    REVIEW_DB_PATH=/tmp/schedule-demo.db ./run.sh --port 8010
Then:
    PYTHONPATH=/workspace/.pylibs/lib/python3.11/site-packages \
      python3 examples/schedule_walkthrough.py http://127.0.0.1:8010
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

KEYS = {
    "editor": "key-editor-1",
    "author1": "key-author-1",
    "author2": "key-author-2",
    "reviewer1": "key-reviewer-1",
    "reviewer2": "key-reviewer-2",
    "reviewer3": "key-reviewer-3",
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
    accounts = call("editor", "GET", "/editor/accounts")
    id_of = {a["username"]: a["id"] for a in accounts}

    now = datetime.now(timezone.utc)
    deadline = iso(now + timedelta(days=20))

    def at(days_ahead: int, hour: int) -> tuple[str, str]:
        start = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=0, second=0, microsecond=0)
        return iso(start), iso(start + timedelta(hours=1))

    d10s, d10e = at(40, 10)
    d11s, d11e = at(40, 11)
    next10s, next10e = at(41, 10)

    def publish_paper(author: str, reviewers: list[str], title: str):
        ms = call(
            author,
            "POST",
            "/author/manuscripts",
            json={"title": title, "content": "full text"},
        )
        revision_id = ms["first_revision"]["revision_id"]
        review_ids = []
        for reviewer in reviewers:
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
                json={"content": "solid work", "score": 4},
            )
            review_ids.append(rv["review_id"])
        decision = call(
            "editor",
            "POST",
            "/editor/decisions",
            json={
                "revision_id": revision_id,
                "review_ids": review_ids,
                "conclusion": "accept (editor picked this paper; text is not parsed)",
            },
        )
        return ms["manuscript_id"], decision["decision_id"]

    m1, dec1 = publish_paper("author1", ["reviewer1", "reviewer2"], "Scaling Planning Systems")
    m2, dec2 = publish_paper("author2", ["reviewer2", "reviewer3"], "Verification of Schedules")

    # --- 1. Initial slots, one confirmation group per paper ----------------
    def propose_and_confirm(author: str, manuscript_id: int, decision_id: int, room: str, times):
        s, e = times
        group = call(
            "editor",
            "POST",
            "/editor/schedule/groups",
            json={
                "confirmation_deadline": deadline,
                "items": [
                    {
                        "manuscript_id": manuscript_id,
                        "decision_id": decision_id,
                        "room": room,
                        "starts_at": s,
                        "ends_at": e,
                    }
                ],
            },
        )
        item_id = group["items"][0]["item_id"]
        call(
            author,
            "POST",
            f"/author/schedule/items/{item_id}/respond",
            json={"accept": True},
        )

    propose_and_confirm("author1", m1, dec1, "hall-a", (d10s, d10e))
    propose_and_confirm("author2", m2, dec2, "hall-a", (d11s, d11e))

    call("editor", "GET", "/editor/schedule")

    # --- 2. Linked swap of the two consecutive slots ----------------------
    swap = call(
        "editor",
        "POST",
        "/editor/schedule/groups",
        json={
            "confirmation_deadline": deadline,
            "items": [
                # m1 moves into m2's old 11:00 slot ...
                {"manuscript_id": m1, "room": "hall-a", "starts_at": d11s, "ends_at": d11e},
                # ... and m2 moves into m1's old 10:00 slot.  decision_id is
                # omitted: existing arrangements reuse their decision.
                {"manuscript_id": m2, "room": "hall-a", "starts_at": d10s, "ends_at": d10e},
            ],
        },
    )
    items = {it["manuscript_id"]: it["item_id"] for it in swap["items"]}

    # Old schedule still valid; new slots reserved.
    call("editor", "GET", "/editor/schedule")
    call("editor", "GET", "/editor/schedule/occupancy")

    call(
        "author1",
        "POST",
        f"/author/schedule/items/{items[m1]}/respond",
        json={"accept": True},
    )
    # Author 2 only sees their own arrangement.
    call("author2", "GET", "/author/schedule/grouped")
    call(
        "author2",
        "POST",
        f"/author/schedule/items/{items[m2]}/respond",
        json={"accept": True},
    )

    # Swap is now effective.
    call("editor", "GET", "/editor/schedule")

    # --- 3. A proposal that gets declined ---------------------------------
    again = call(
        "editor",
        "POST",
        "/editor/schedule/groups",
        json={
            "confirmation_deadline": deadline,
            "items": [
                {
                    "manuscript_id": m1,
                    "room": "hall-b",
                    "starts_at": next10s,
                    "ends_at": next10e,
                }
            ],
        },
    )
    item_id = again["items"][0]["item_id"]
    call(
        "author1",
        "POST",
        f"/author/schedule/items/{item_id}/respond",
        json={"accept": False},
    )

    # Editor reviews every group's progress / end reason.
    groups = call("editor", "GET", "/editor/schedule/groups")
    assert any(g["status"] == "declined" and g["end_reason"] == "author_declined" for g in groups)

    # The confirmed schedule survived untouched.
    schedule = call("editor", "GET", "/editor/schedule")
    assert len(schedule) == 2

    # Authors can query their own arrangements.
    call("author1", "GET", "/author/schedule/grouped")

    print("\nSchedule walkthrough completed successfully.")


if __name__ == "__main__":
    main()
