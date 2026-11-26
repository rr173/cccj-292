"""Audience-facing endpoints: browse talks, manage own seat registrations.

Every locally prepared account may act as audience.  These endpoints never
expose other audience members' registrations or any review material — only
per-talk counts and the caller's own status.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Response

from .. import audience, config
from ..security import current_user

router = APIRouter(prefix="/audience", tags=["audience"])


@router.get("/talks")
def list_talks(user: sqlite3.Row = Depends(current_user)):
    """Confirmed talks with seat availability and the caller's own status."""
    return audience.list_talks(config.DB_PATH, user)


def _register(manuscript_id: int, response: Response, user: sqlite3.Row):
    result = audience.register(config.DB_PATH, user, manuscript_id)
    if not result["created"]:
        # Re-registering while active is a no-op, not a creation.
        response.status_code = 200
    return result["registration"]


@router.post("/talks/{manuscript_id}/register", status_code=201)
def register_for_talk(
    manuscript_id: int,
    response: Response,
    user: sqlite3.Row = Depends(current_user),
):
    """Register for a talk; seats are confirmed in registration order."""
    return _register(manuscript_id, response, user)


@router.post("/talks/{manuscript_id}/cancel")
def cancel_registration(manuscript_id: int, user: sqlite3.Row = Depends(current_user)):
    """Cancel the caller's active registration for the talk."""
    return audience.cancel(config.DB_PATH, user, manuscript_id)


@router.delete("/talks/{manuscript_id}/registration", include_in_schema=False)
def cancel_registration_delete_alias(
    manuscript_id: int, user: sqlite3.Row = Depends(current_user)
):
    return audience.cancel(config.DB_PATH, user, manuscript_id)


@router.post("/talks/{manuscript_id}/registration", include_in_schema=False)
def register_registration_alias(
    manuscript_id: int,
    response: Response,
    user: sqlite3.Row = Depends(current_user),
):
    return _register(manuscript_id, response, user)


@router.get("/registrations")
def my_registrations(user: sqlite3.Row = Depends(current_user)):
    """The caller's own registrations: seats, waitlist positions and history."""
    return audience.my_registrations(config.DB_PATH, user)
