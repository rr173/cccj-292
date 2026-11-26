#!/usr/bin/env python3
"""End-to-end audience seat-reservation walkthrough over HTTP.

Story:
  1. The editor sets audience capacities on the configured rooms (default 0).
  2. Two accepted papers get confirmed talks whose slots overlap.
  3. Audience members (the prepared accounts) register: seats fill in
     registration order, later applicants waitlist, and one person cannot
     hold seats in two overlapping talks (waitlisting is allowed).
  4. A cancellation frees a seat and the earliest waitlisted applicant is
     promoted automatically.
  5. A confirmed reschedule carries the registrations over and re-checks
     them; a capacity shrink demotes the newest seats back to the waitlist.
  6. The editor reads the final admission lists — no manual copying.

Start a server first (with a fresh database for a clean run):
    REVIEW_DB_PATH=/tmp/audience-demo.db ./run.sh --port 8012
Then:
    PYTHONPATH=/workspace/.pylibs/lib/python3.11/site-packages \
      python3 examples/audience_walkthrough.py http://127.0.0.1:8012
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
    print(f"\n=== {role:9s} {method:5s} {path} -> {r.status_code}")
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

    def slot(days_ahead: int, hour: int, minute: int = 0) -> tuple[str, str]:
        start = (now + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        return iso(start), iso(start + timedelta(hours=1))

    t1_slot = slot(40, 10)          # talk 1: 10:00-11:00
    t2_slot = slot(40, 10, 30)      # talk 2: 10:30-11:30 (overlaps talk 1)
    moved_slot = slot(40, 14)       # talk 1 moves to 14:00-15:00 later

    # --- 1. Editor sets audience capacities (default is zero) -------------
    call("editor", "GET", "/editor/rooms")
    call("editor", "PUT", "/editor/rooms/hall-a/audience-capacity", json={"capacity": 2})
    call("editor", "PUT", "/editor/rooms/hall-b/audience-capacity", json={"capacity": 2})

    # --- 2. Two papers get confirmed, overlapping talks -------------------
    def publish_paper(author: str, reviewers: list[str], title: str):
        ms = call(
            author, "POST", "/author/manuscripts",
            json={"title": title, "content": "full text"},
        )
        revision_id = ms["first_revision"]["revision_id"]
        review_ids = []
        for reviewer in reviewers:
            inv = call(
                "editor", "POST", "/editor/invitations",
                json={"revision_id": revision_id, "reviewer_id": id_of[reviewer]},
            )
            call(reviewer, "POST", f"/reviewer/invitations/{inv['invitation_id']}/respond",
                 json={"accept": True})
            rv = call(reviewer, "POST", f"/reviewer/invitations/{inv['invitation_id']}/reviews",
                      json={"content": "solid work", "score": 4})
            review_ids.append(rv["review_id"])
        decision = call(
            "editor", "POST", "/editor/decisions",
            json={"revision_id": revision_id, "review_ids": review_ids,
                  "conclusion": "accept"},
        )
        return ms["manuscript_id"], decision["decision_id"]

    def confirm_talk(author: str, manuscript_id: int, decision_id: int, room: str, times):
        start, end = times
        group = call(
            "editor", "POST", "/editor/schedule/groups",
            json={
                "confirmation_deadline": deadline,
                "items": [{
                    "manuscript_id": manuscript_id,
                    "decision_id": decision_id,
                    "room": room,
                    "starts_at": start,
                    "ends_at": end,
                }],
            },
        )
        item_id = group["items"][0]["item_id"]
        call(author, "POST", f"/author/schedule/items/{item_id}/respond",
             json={"accept": True})

    m1, dec1 = publish_paper("author1", ["reviewer1", "reviewer2"], "Scaling Planning Systems")
    m2, dec2 = publish_paper("author2", ["reviewer2", "reviewer3"], "Verification of Schedules")
    confirm_talk("author1", m1, dec1, "hall-a", t1_slot)
    confirm_talk("author2", m2, dec2, "hall-b", t2_slot)

    # --- 3. Audience registration: seats fill, then waitlist --------------
    call("reviewer1", "GET", "/audience/talks")
    call("reviewer1", "POST", f"/audience/talks/{m1}/register")   # seated
    call("reviewer2", "POST", f"/audience/talks/{m1}/register")   # seated
    call("reviewer3", "POST", f"/audience/talks/{m1}/register")   # waitlisted

    # reviewer1 holds a seat in talk 1 (10:00-11:00); talk 2 overlaps, so
    # they may only waitlist.  author1 has no clash and takes the t2 seat.
    call("reviewer1", "POST", f"/audience/talks/{m2}/register")   # waitlisted (clash)
    call("author1", "POST", f"/audience/talks/{m2}/register")     # seated

    # --- 4. A cancellation promotes the earliest waitlisted applicant -----
    call("reviewer2", "POST", f"/audience/talks/{m1}/cancel")
    call("reviewer3", "GET", "/audience/registrations")           # now seated in t1

    # --- 5. Reschedule carries registrations; capacity shrink demotes -----
    group = call(
        "editor", "POST", "/editor/schedule/groups",
        json={
            "confirmation_deadline": deadline,
            "items": [{
                "manuscript_id": m1,
                "room": "hall-a",
                "starts_at": moved_slot[0],
                "ends_at": moved_slot[1],
            }],
        },
    )
    item_id = group["items"][0]["item_id"]
    call("author1", "POST", f"/author/schedule/items/{item_id}/respond",
         json={"accept": True})
    # Talk 1 moved to 14:00: its list came along untouched, and reviewer1's
    # overlap with talk 2 is gone, so their t2 waitlist entry is promoted.
    call("editor", "GET", f"/editor/talks/{m1}/admissions")
    call("editor", "GET", f"/editor/talks/{m2}/admissions")

    # Shrinking hall-a to one seat demotes the newest seat back to waitlist.
    call("editor", "PUT", "/editor/rooms/hall-a/audience-capacity", json={"capacity": 1})

    # --- 6. Final admission lists, straight from the system ---------------
    call("editor", "GET", f"/editor/talks/{m1}/admissions")
    call("editor", "GET", f"/editor/talks/{m2}/admissions")
    call("reviewer1", "GET", "/audience/registrations")

    print("\nAudience walkthrough completed successfully.")


if __name__ == "__main__":
    main()
