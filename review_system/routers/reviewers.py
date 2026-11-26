"""Reviewer-facing endpoints.

Reviewers see only invitations addressed to them and the review content they
themselves submitted.  There is intentionally no "fetch review by id" route:
other reviewers' unpublished reviews cannot be read even by guessing ids.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from .. import config, services
from ..schemas import InvitationResponse, ReviewSubmit
from ..security import require_role

router = APIRouter(prefix="/reviewer", tags=["reviewer"])


@router.get("/invitations")
def my_invitations(user: sqlite3.Row = Depends(require_role("reviewer"))):
    return services.list_my_invitations(config.DB_PATH, user)


@router.post("/invitations/{invitation_id}/respond")
def respond(
    invitation_id: int,
    body: InvitationResponse,
    user: sqlite3.Row = Depends(require_role("reviewer")),
):
    return services.respond_invitation(config.DB_PATH, user, invitation_id, body.accept)


@router.post("/invitations/{invitation_id}/reviews", status_code=201)
def submit_review(
    invitation_id: int,
    body: ReviewSubmit,
    user: sqlite3.Row = Depends(require_role("reviewer")),
):
    return services.submit_review(
        config.DB_PATH, user, invitation_id, body.content, body.score
    )
