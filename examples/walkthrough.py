#!/usr/bin/env python3
"""End-to-end role walkthrough using httpx.

Runs the whole happy-path workflow plus a conflict/withdrawal/republish
scenario against a running server (defaults to http://127.0.0.1:8000).

Usage:
    ./run.sh &
    python3 examples/walkthrough.py
"""

from __future__ import annotations

import sys

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
    headers.update(kwargs.pop("headers", {}))
    r = httpx.request(method, BASE + path, headers=headers, timeout=10, **kwargs)
    print(f"\n=== {role:9s} {method:5s} {path} -> {r.status_code}")
    data = r.json()
    import json

    print(json.dumps(data, indent=2, ensure_ascii=False))
    r.raise_for_status()
    return data


def main() -> None:
    # 0. Health + prepared accounts.
    httpx.get(BASE + "/health", timeout=10).raise_for_status()
    accounts = call("editor", "GET", "/editor/accounts")
    id_of = {a["username"]: a["id"] for a in accounts}

    # 1. Author submits a manuscript and a later revision.
    ms = call(
        "author1",
        "POST",
        "/author/manuscripts",
        json={"title": "Graph Neural Networks for Review Routing", "content": "revision 1 text"},
    )
    manuscript_id = ms["manuscript_id"]
    revision1_id = ms["first_revision"]["revision_id"]

    rev2 = call(
        "author1",
        "POST",
        f"/author/manuscripts/{manuscript_id}/revisions",
        json={"content": "revision 2 text, old revision stays untouched"},
    )
    revision2_id = rev2["revision_id"]

    # 2. Editor invites reviewers to revision 1 only.
    inv1 = call(
        "editor",
        "POST",
        "/editor/invitations",
        json={"revision_id": revision1_id, "reviewer_id": id_of["reviewer1"]},
    )
    inv2 = call(
        "editor",
        "POST",
        "/editor/invitations",
        json={"revision_id": revision1_id, "reviewer_id": id_of["reviewer2"]},
    )

    # 3. Reviewers accept (no invitation id exposed to other roles) and review.
    call(
        "reviewer1",
        "POST",
        f"/reviewer/invitations/{inv1['invitation_id']}/respond",
        json={"accept": True},
    )
    review1 = call(
        "reviewer1",
        "POST",
        f"/reviewer/invitations/{inv1['invitation_id']}/reviews",
        json={"content": "Strong method; experiments need ablations.", "score": 4},
    )
    call(
        "reviewer2",
        "POST",
        f"/reviewer/invitations/{inv2['invitation_id']}/respond",
        json={"accept": True},
    )
    review2 = call(
        "reviewer2",
        "POST",
        f"/reviewer/invitations/{inv2['invitation_id']}/reviews",
        json={"content": "Novel framing, but related work is thin.", "score": 3},
    )

    # 4. Author still sees nothing.
    call("author1", "GET", "/author/decisions")

    # 5. Editor publishes a decision on revision 1 with the two clean reviews.
    decision = call(
        "editor",
        "POST",
        "/editor/decisions",
        json={
            "revision_id": revision1_id,
            "review_ids": [review1["review_id"], review2["review_id"]],
            "conclusion": "minor revision",
        },
    )
    decision_id = decision["decision_id"]

    # 6. Author reads the frozen, anonymized version.
    call("author1", "GET", "/author/decisions")
    call("author1", "GET", f"/author/decisions/{decision_id}")

    # 7. A conflict involving reviewer 1 is registered after publication.
    conflict = call(
        "editor",
        "POST",
        "/editor/conflicts",
        json={
            "author_id": id_of["author1"],
            "reviewer_id": id_of["reviewer1"],
            "reason": "prior joint grant (disclosed late)",
        },
    )
    assert decision_id in conflict["withdrawn_decision_ids"]

    # 8. The old author link now returns 410 Gone; the editor keeps the original.
    r = httpx.get(
        BASE + f"/author/decisions/{decision_id}",
        headers={"X-API-Key": KEYS["author1"]},
        timeout=10,
    )
    print(f"\n=== author1 GET old link -> {r.status_code} (withdrawn body is unavailable)")
    print(r.json())
    assert r.status_code == 410
    call("editor", "GET", f"/editor/decisions/{decision_id}")

    # 9. Re-publication needs newly qualified reviews: reviewer 3 joins reviewer 2.
    inv3 = call(
        "editor",
        "POST",
        "/editor/invitations",
        json={"revision_id": revision1_id, "reviewer_id": id_of["reviewer3"]},
    )
    call(
        "reviewer3",
        "POST",
        f"/reviewer/invitations/{inv3['invitation_id']}/respond",
        json={"accept": True},
    )
    review3 = call(
        "reviewer3",
        "POST",
        f"/reviewer/invitations/{inv3['invitation_id']}/reviews",
        json={"content": "Ablations added in the rebuttal address my concerns.", "score": 4},
    )
    new_decision = call(
        "editor",
        "POST",
        "/editor/decisions",
        json={
            "revision_id": revision1_id,
            "review_ids": [review2["review_id"], review3["review_id"]],
            "conclusion": "accept",
        },
    )
    call("author1", "GET", f"/author/decisions/{new_decision['decision_id']}")
    print("\nWalkthrough completed successfully.")


if __name__ == "__main__":
    main()
