"""Editor-facing endpoints: invitations, conflicts, decisions, scheduling."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import config, reimbursement, scheduling, services
from ..schemas import (
    AllowanceCreate,
    BatchCreate,
    BatchResultsInput,
    ClaimCreate,
    ConflictCreate,
    DecisionPublish,
    InvitationCreate,
    ReceiptCreate,
    ScheduleGroupCreate,
)
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


# ---------------------------------------------------------------------------
# Rooms (local configuration)
# ---------------------------------------------------------------------------

@router.get("/rooms")
def list_rooms(_=Depends(require_role("editor"))):
    return config.public_rooms()


# ---------------------------------------------------------------------------
# Conference schedule
# ---------------------------------------------------------------------------

def _create_group(body: ScheduleGroupCreate):
    items = [item.model_dump() for item in body.items]
    return scheduling.create_schedule_group(
        config.DB_PATH, items, body.confirmation_deadline
    )


# Primary collection URL plus common aliases.
@router.post("/schedule/groups", status_code=201)
def create_schedule_group(body: ScheduleGroupCreate, _=Depends(require_role("editor"))):
    return _create_group(body)


@router.post("/schedule", status_code=201, include_in_schema=False)
def create_schedule_group_alias_schedule(
    body: ScheduleGroupCreate, _=Depends(require_role("editor"))
):
    return _create_group(body)


@router.post("/schedules", status_code=201, include_in_schema=False)
def create_schedule_group_alias_schedules(
    body: ScheduleGroupCreate, _=Depends(require_role("editor"))
):
    return _create_group(body)


@router.post("/arrangements", status_code=201, include_in_schema=False)
def create_schedule_group_alias_arrangements(
    body: ScheduleGroupCreate, _=Depends(require_role("editor"))
):
    return _create_group(body)


@router.get("/schedule")
def schedule(history: bool = False, _=Depends(require_role("editor"))):
    """Confirmed conference schedule; ``?history=true`` also lists ended slots."""
    return scheduling.editor_list_sessions(config.DB_PATH, include_history=history)


@router.get("/schedules", include_in_schema=False)
def schedules_alias(history: bool = False, _=Depends(require_role("editor"))):
    return scheduling.editor_list_sessions(config.DB_PATH, include_history=history)


@router.get("/sessions", include_in_schema=False)
def sessions_alias(history: bool = False, _=Depends(require_role("editor"))):
    return scheduling.editor_list_sessions(config.DB_PATH, include_history=history)


@router.get("/talks", include_in_schema=False)
def talks_alias(history: bool = False, _=Depends(require_role("editor"))):
    return scheduling.editor_list_sessions(config.DB_PATH, include_history=history)


@router.post("/schedule-groups", status_code=201, include_in_schema=False)
def create_schedule_group_hyphen_alias(
    body: ScheduleGroupCreate, _=Depends(require_role("editor"))
):
    return _create_group(body)


@router.get("/schedule-groups", include_in_schema=False)
def list_schedule_groups_hyphen_alias(_=Depends(require_role("editor"))):
    return scheduling.editor_list_groups(config.DB_PATH)


@router.get("/schedule-groups/{group_id}", include_in_schema=False)
def get_schedule_group_hyphen_alias(group_id: int, _=Depends(require_role("editor"))):
    return scheduling.editor_get_group(config.DB_PATH, group_id)


@router.get("/schedule/occupancy")
def schedule_occupancy(_=Depends(require_role("editor"))):
    """Confirmed sessions plus slots currently reserved by pending groups."""
    return scheduling.editor_occupancy(config.DB_PATH)


@router.get("/schedule/confirmed")
def schedule_confirmed(_=Depends(require_role("editor"))):
    return scheduling.editor_list_sessions(config.DB_PATH, include_history=False)


@router.get("/schedule/pending")
def schedule_pending(_=Depends(require_role("editor"))):
    groups = scheduling.editor_list_groups(config.DB_PATH)
    return [g for g in groups if g["status"] == "pending"]


@router.get("/schedule/groups")
def list_schedule_groups(_=Depends(require_role("editor"))):
    return scheduling.editor_list_groups(config.DB_PATH)


@router.get("/arrangements/groups", include_in_schema=False)
def list_arrangement_groups_alias(_=Depends(require_role("editor"))):
    return scheduling.editor_list_groups(config.DB_PATH)


@router.get("/schedule/groups/{group_id}")
def get_schedule_group(group_id: int, _=Depends(require_role("editor"))):
    return scheduling.editor_get_group(config.DB_PATH, group_id)


@router.get("/schedule/{group_id}", include_in_schema=False)
def get_schedule_group_short_alias(group_id: int, _=Depends(require_role("editor"))):
    return scheduling.editor_get_group(config.DB_PATH, group_id)


@router.get("/arrangements/groups/{group_id}", include_in_schema=False)
def get_arrangement_group_alias(group_id: int, _=Depends(require_role("editor"))):
    return scheduling.editor_get_group(config.DB_PATH, group_id)


# ---------------------------------------------------------------------------
# Speaker travel reimbursement (integer cents)
# ---------------------------------------------------------------------------

@router.post("/reimbursements/allowances", status_code=201)
def create_reimbursement_allowance(
    body: AllowanceCreate, _=Depends(require_role("editor"))
):
    return reimbursement.create_allowance(
        config.DB_PATH, body.manuscript_id, body.amount_cents, body.decision_id
    )


@router.get("/reimbursements/allowances")
def list_reimbursement_allowances(_=Depends(require_role("editor"))):
    return reimbursement.list_allowances(config.DB_PATH)


@router.get("/reimbursements/allowances/{allowance_id}")
def get_reimbursement_allowance(allowance_id: int, _=Depends(require_role("editor"))):
    return reimbursement.get_allowance(config.DB_PATH, allowance_id)


@router.post("/reimbursements/receipts", status_code=201)
def create_reimbursement_receipt(
    body: ReceiptCreate, _=Depends(require_role("editor"))
):
    return reimbursement.create_receipt(
        config.DB_PATH, body.voucher_no, body.author_id, body.amount_cents
    )


@router.get("/reimbursements/receipts")
def list_reimbursement_receipts(_=Depends(require_role("editor"))):
    return reimbursement.list_receipts(config.DB_PATH)


@router.get("/reimbursements/receipts/{receipt_id}")
def get_reimbursement_receipt(receipt_id: int, _=Depends(require_role("editor"))):
    return reimbursement.get_receipt(config.DB_PATH, receipt_id)


@router.post("/reimbursements/claims", status_code=201)
def create_reimbursement_claim(body: ClaimCreate, _=Depends(require_role("editor"))):
    """Approve an expense against (allowance, same-author receipt, amount)."""
    return reimbursement.create_claim(
        config.DB_PATH, body.allowance_id, body.receipt_id, body.amount_cents
    )


@router.get("/reimbursements/claims")
def list_reimbursement_claims(_=Depends(require_role("editor"))):
    return reimbursement.list_claims(config.DB_PATH)


@router.get("/reimbursements/claims/{claim_id}")
def get_reimbursement_claim(claim_id: int, _=Depends(require_role("editor"))):
    return reimbursement.get_claim(config.DB_PATH, claim_id)


@router.post("/reimbursements/batches", status_code=201)
def create_reimbursement_batch(body: BatchCreate, _=Depends(require_role("editor"))):
    """Hand approved amounts to finance.  Idempotent on (request no, content).

    The request number is the body's ``idempotency_key``; a replay with the
    same number and content returns the original delivery list and per-payment
    numbers without calling any payment service.
    """
    items = [item.model_dump() for item in body.items]
    return reimbursement.create_batch(config.DB_PATH, body.idempotency_key, items)


@router.get("/reimbursements/batches")
def list_reimbursement_batches(_=Depends(require_role("editor"))):
    return reimbursement.list_batches(config.DB_PATH)


@router.get("/reimbursements/batches/{batch_id}")
def get_reimbursement_batch(batch_id: int, _=Depends(require_role("editor"))):
    return reimbursement.get_batch(config.DB_PATH, batch_id)


@router.post("/reimbursements/batches/{batch_id}/results")
def record_reimbursement_results(
    batch_id: int, body: BatchResultsInput, _=Depends(require_role("editor"))
):
    """Record finance's (partial) result for one payment number at a time."""
    results = [item.model_dump() for item in body.results]
    return reimbursement.record_results(config.DB_PATH, batch_id, results)


@router.get("/reimbursements/summary")
def reimbursement_summary(_=Depends(require_role("editor"))):
    """Conference budget plus every allowance's and receipt's encumbrance/spend."""
    return reimbursement.editor_summary(config.DB_PATH)
