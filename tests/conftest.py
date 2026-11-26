"""Shared pytest fixtures: an isolated temporary database and API clients."""

from __future__ import annotations

import os
import sys

# Make the project root importable without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from review_system import db as database
from review_system import config as app_config
from review_system.main import create_app

KEYS = {
    "editor": "key-editor-1",
    "author1": "key-author-1",
    "author2": "key-author-2",
    "reviewer1": "key-reviewer-1",
    "reviewer2": "key-reviewer-2",
    "reviewer3": "key-reviewer-3",
}


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "test_review.db")
    database.init_db(path)
    app_config.DB_PATH = path
    return path


@pytest.fixture()
def client(db_path):
    # init_db has already run on this exact path in the db_path fixture.
    app = create_app(db_path)
    with TestClient(app) as c:
        yield c


def auth(role: str) -> dict:
    return {"X-API-Key": KEYS[role]}


def create_manuscript(client, author: str = "author1", title: str = "On X", content: str = "v1 body"):
    r = client.post(
        "/author/manuscripts",
        json={"title": title, "content": content},
        headers=auth(author),
    )
    assert r.status_code == 201, r.text
    return r.json()


def invite(client, revision_id: int, reviewer: str):
    reviewer_id = reviewer_user_id(client, reviewer)
    r = client.post(
        "/editor/invitations",
        json={"revision_id": revision_id, "reviewer_id": reviewer_id},
        headers=auth("editor"),
    )
    assert r.status_code == 201, r.text
    return r.json()


def reviewer_user_id(client, reviewer: str) -> int:
    r = client.get("/editor/accounts", headers=auth("editor"))
    accounts = r.json()
    return next(a["id"] for a in accounts if a["username"] == reviewer)


def author_user_id(client, author: str) -> int:
    r = client.get("/editor/accounts", headers=auth("editor"))
    accounts = r.json()
    return next(a["id"] for a in accounts if a["username"] == author)


def accept_and_review(client, invitation_id: int, reviewer: str, content: str, score: int):
    r = client.post(
        f"/reviewer/invitations/{invitation_id}/respond",
        json={"accept": True},
        headers=auth(reviewer),
    )
    assert r.status_code == 200, r.text
    r = client.post(
        f"/reviewer/invitations/{invitation_id}/reviews",
        json={"content": content, "score": score},
        headers=auth(reviewer),
    )
    assert r.status_code == 201, r.text
    return r.json()


def publish_two_review_decision(client, revision_id: int, review_ids: list[int], conclusion: str = "accept"):
    r = client.post(
        "/editor/decisions",
        json={
            "revision_id": revision_id,
            "review_ids": review_ids,
            "conclusion": conclusion,
        },
        headers=auth("editor"),
    )
    assert r.status_code == 201, r.text
    return r.json()
