"""Author-facing endpoints."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from .. import config, services
from ..schemas import ManuscriptCreate, RevisionCreate
from ..security import require_role

router = APIRouter(prefix="/author", tags=["author"])


@router.post("/manuscripts", status_code=201)
def submit_manuscript(
    body: ManuscriptCreate,
    user: sqlite3.Row = Depends(require_role("author")),
):
    return services.create_manuscript(config.DB_PATH, user, body.title, body.content)


@router.post("/manuscripts/{manuscript_id}/revisions", status_code=201)
def submit_revision(
    manuscript_id: int,
    body: RevisionCreate,
    user: sqlite3.Row = Depends(require_role("author")),
):
    return services.add_revision(config.DB_PATH, user, manuscript_id, body.content)


@router.get("/manuscripts")
def my_manuscripts(user: sqlite3.Row = Depends(require_role("author"))):
    return services.list_my_manuscripts(config.DB_PATH, user)


@router.get("/manuscripts/{manuscript_id}")
def my_manuscript(
    manuscript_id: int, user: sqlite3.Row = Depends(require_role("author"))
):
    return services.get_author_manuscript(config.DB_PATH, user, manuscript_id)


@router.get("/decisions")
def my_decisions(user: sqlite3.Row = Depends(require_role("author"))):
    return services.list_author_decisions(config.DB_PATH, user)


@router.get("/decisions/{decision_id}")
def my_decision(
    decision_id: int, user: sqlite3.Row = Depends(require_role("author"))
):
    return services.get_author_decision(config.DB_PATH, user, decision_id)
