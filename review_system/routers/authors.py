"""Author-facing endpoints."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from .. import config, reimbursement, scheduling, services
from ..schemas import ManuscriptCreate, RevisionCreate, ScheduleResponseInput
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


# ---------------------------------------------------------------------------
# Conference schedule — authors only ever see their own manuscripts.
# ---------------------------------------------------------------------------

@router.get("/schedule")
def my_schedule(
    grouped: bool = False,
    view: str | None = None,
    user: sqlite3.Row = Depends(require_role("author")),
):
    """Flat list of my confirmed talks and my (past/pending) confirmations.

    ``?grouped=true`` (or ``?view=grouped``) returns confirmed/pending/history
    sections instead of the flat list.
    """
    if grouped or view == "grouped":
        return scheduling.author_schedule_grouped(config.DB_PATH, user)
    return scheduling.author_schedule_flat(config.DB_PATH, user)


@router.get("/schedule/grouped")
def my_schedule_grouped(user: sqlite3.Row = Depends(require_role("author"))):
    return scheduling.author_schedule_grouped(config.DB_PATH, user)


@router.get("/schedules", include_in_schema=False)
def my_schedule_alias_schedules(user: sqlite3.Row = Depends(require_role("author"))):
    return scheduling.author_schedule_flat(config.DB_PATH, user)


@router.get("/arrangements", include_in_schema=False)
def my_arrangements_alias(user: sqlite3.Row = Depends(require_role("author"))):
    return scheduling.author_schedule_flat(config.DB_PATH, user)


@router.get("/talks", include_in_schema=False)
def my_talks_alias(user: sqlite3.Row = Depends(require_role("author"))):
    return scheduling.author_schedule_flat(config.DB_PATH, user)


def _respond(item_id: int, body: ScheduleResponseInput, user: sqlite3.Row):
    return scheduling.respond_to_item(config.DB_PATH, user, item_id, bool(body.accept))


@router.post("/schedule/groups/{group_id}/respond")
def respond_to_group(
    group_id: int,
    body: ScheduleResponseInput,
    user: sqlite3.Row = Depends(require_role("author")),
):
    item_id = scheduling.resolve_own_group_item(
        config.DB_PATH, user, group_id, body.item_id
    )
    return _respond(item_id, body, user)


@router.post("/schedule/groups/{group_id}/confirm", include_in_schema=False)
def confirm_group_alias(
    group_id: int,
    body: ScheduleResponseInput,
    user: sqlite3.Row = Depends(require_role("author")),
):
    item_id = scheduling.resolve_own_group_item(
        config.DB_PATH, user, group_id, body.item_id
    )
    return _respond(item_id, body, user)


@router.post("/confirmations/{item_id}/respond", include_in_schema=False)
def respond_confirmation_alias(
    item_id: int,
    body: ScheduleResponseInput,
    user: sqlite3.Row = Depends(require_role("author")),
):
    return _respond(item_id, body, user)


@router.post("/confirmations/{item_id}", include_in_schema=False)
def post_confirmation_bare_alias(
    item_id: int,
    body: ScheduleResponseInput,
    user: sqlite3.Row = Depends(require_role("author")),
):
    return _respond(item_id, body, user)


@router.post("/schedule/items/{item_id}/respond")
def respond_schedule_item(
    item_id: int,
    body: ScheduleResponseInput,
    user: sqlite3.Row = Depends(require_role("author")),
):
    return _respond(item_id, body, user)


@router.post("/schedule/items/{item_id}/confirm", include_in_schema=False)
def confirm_schedule_item(
    item_id: int,
    body: ScheduleResponseInput,
    user: sqlite3.Row = Depends(require_role("author")),
):
    return _respond(item_id, body, user)


@router.post("/schedule/{item_id}/respond", include_in_schema=False)
def respond_schedule_short_alias(
    item_id: int,
    body: ScheduleResponseInput,
    user: sqlite3.Row = Depends(require_role("author")),
):
    return _respond(item_id, body, user)


@router.post("/schedule-items/{item_id}/respond", include_in_schema=False)
def respond_schedule_items_hyphen_alias(
    item_id: int,
    body: ScheduleResponseInput,
    user: sqlite3.Row = Depends(require_role("author")),
):
    return _respond(item_id, body, user)


@router.post("/schedule-items/{item_id}/confirm", include_in_schema=False)
def confirm_schedule_items_hyphen_alias(
    item_id: int,
    body: ScheduleResponseInput,
    user: sqlite3.Row = Depends(require_role("author")),
):
    return _respond(item_id, body, user)


@router.post("/arrangements/{item_id}/respond", include_in_schema=False)
def respond_arrangement_alias(
    item_id: int,
    body: ScheduleResponseInput,
    user: sqlite3.Row = Depends(require_role("author")),
):
    return _respond(item_id, body, user)


@router.post("/arrangements/{item_id}/confirm", include_in_schema=False)
def confirm_arrangement_alias(
    item_id: int,
    body: ScheduleResponseInput,
    user: sqlite3.Row = Depends(require_role("author")),
):
    return _respond(item_id, body, user)


@router.post("/schedules/{item_id}/respond", include_in_schema=False)
def respond_schedules_alias(
    item_id: int,
    body: ScheduleResponseInput,
    user: sqlite3.Row = Depends(require_role("author")),
):
    return _respond(item_id, body, user)


@router.post("/schedules/{item_id}/confirm", include_in_schema=False)
def confirm_schedules_alias(
    item_id: int,
    body: ScheduleResponseInput,
    user: sqlite3.Row = Depends(require_role("author")),
):
    return _respond(item_id, body, user)


@router.post("/schedule/{item_id}", include_in_schema=False)
def post_schedule_bare_alias(
    item_id: int,
    body: ScheduleResponseInput,
    user: sqlite3.Row = Depends(require_role("author")),
):
    return _respond(item_id, body, user)


@router.post("/schedule-items/{item_id}", include_in_schema=False)
def post_schedule_item_bare_alias(
    item_id: int,
    body: ScheduleResponseInput,
    user: sqlite3.Row = Depends(require_role("author")),
):
    return _respond(item_id, body, user)


# ---------------------------------------------------------------------------
# Speaker travel reimbursement — the author sees only their own progress.
# ---------------------------------------------------------------------------

@router.get("/reimbursements")
def my_reimbursements(user: sqlite3.Row = Depends(require_role("author"))):
    return reimbursement.author_reimbursements(config.DB_PATH, user)


@router.get("/reimbursements/allowances/{allowance_id}")
def my_reimbursement_allowance(
    allowance_id: int, user: sqlite3.Row = Depends(require_role("author"))
):
    return reimbursement.author_allowance(config.DB_PATH, user, allowance_id)
