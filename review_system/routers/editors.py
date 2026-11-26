"""Editor-facing endpoints: invitations, conflicts and decisions."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import config, services
from ..schemas import ConflictCreate, DecisionPublish, InvitationCreate
from ..security import require_role

router = APIRouter(prefix="/editor", tags=["editor"])


@router.get("/accounts")
def accounts(_=Depends(require_role("editor"))):
    return services.list_accounts(config.DB_PATH)


@router.get("/manuscripts")
def manuscripts(_=Depends(require_role("editor"))):
    return services.list_manuscripts_overview(config.DB_PATH)


@router.get("/manuscripts/{manuscript_id}")
def manuscript_detail(manuscript_id: int, _=Depends(require_role("editor"))):
    return services.editor_get_manuscript(config.DB_PATH, manuscript_id)


@router.post("/invitations", status_code=201)
def create_invitation(
    body: InvitationCreate, _=Depends(require_role("editor"))
):
    return services.create_invitation(config.DB_PATH, body.revision_id, body.reviewer_id)


@router.get("/conflicts")
def list_conflicts(_=Depends(require_role("editor"))):
    return services.list_conflicts(config.DB_PATH)


@router.post("/conflicts", status_code=201)
def register_conflict(
    body: ConflictCreate, _=Depends(require_role("editor"))
):
    return services.register_conflict(
        config.DB_PATH, body.author_id, body.reviewer_id, body.reason
    )


@router.post("/decisions", status_code=201)
def publish_decision(
    body: DecisionPublish, _=Depends(require_role("editor"))
):
    return services.publish_decision(
        config.DB_PATH, body.revision_id, body.review_ids, body.conclusion
    )


@router.get("/decisions")
def list_decisions(_=Depends(require_role("editor"))):
    return services.editor_list_decisions(config.DB_PATH)


@router.get("/decisions/{decision_id}")
def get_decision(decision_id: int, _=Depends(require_role("editor"))):
    return services.editor_get_decision(config.DB_PATH, decision_id)
