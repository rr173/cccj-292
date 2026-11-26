"""Pydantic request bodies."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StrictInt, model_validator


class ManuscriptCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1)


class RevisionCreate(BaseModel):
    content: str = Field(min_length=1)


class InvitationResponse(BaseModel):
    accept: bool


class ReviewSubmit(BaseModel):
    # StrictInt: floats and numeric strings are rejected with 422 rather than
    # coerced; ge/le enforce the required 1..5 integer range.
    content: str = Field(min_length=1)
    score: StrictInt = Field(ge=1, le=5)


class InvitationCreate(BaseModel):
    revision_id: int
    reviewer_id: int


class ConflictCreate(BaseModel):
    author_id: int
    reviewer_id: int
    reason: str = Field(min_length=1, max_length=500)


class DecisionPublish(BaseModel):
    revision_id: int
    review_ids: list[int] = Field(min_length=2)
    conclusion: str = Field(min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def _as_utc(value: datetime) -> datetime:
    # Naive datetimes denote UTC by house convention.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ScheduleItemInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    manuscript_id: int = Field(
        validation_alias=AliasChoices("manuscript_id", "manuscript", "paper_id", "id")
    )
    decision_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("decision_id", "decision"),
    )
    room: Any = Field(
        validation_alias=AliasChoices("room", "room_id", "room_code", "venue", "hall")
    )
    starts_at: datetime = Field(
        validation_alias=AliasChoices(
            "starts_at", "start_at", "start", "start_time", "begin", "from"
        )
    )
    ends_at: datetime = Field(
        validation_alias=AliasChoices("ends_at", "end_at", "end", "end_time", "to")
    )

    @model_validator(mode="after")
    def _normalise_times(self) -> "ScheduleItemInput":
        self.starts_at = _as_utc(self.starts_at)
        self.ends_at = _as_utc(self.ends_at)
        return self


_GROUP_LIST_KEYS = ("items", "arrangements", "entries", "schedule", "slots")
_DEADLINE_KEYS = (
    "confirmation_deadline",
    "deadline",
    "confirm_deadline",
    "response_deadline",
    "acceptance_deadline",
)


class ScheduleGroupCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    confirmation_deadline: datetime = Field(
        validation_alias=AliasChoices(*_DEADLINE_KEYS)
    )
    items: list[ScheduleItemInput] = Field(
        min_length=1, validation_alias=AliasChoices(*_GROUP_LIST_KEYS)
    )

    @model_validator(mode="before")
    @classmethod
    def _wrap_single_arrangement(cls, data: Any) -> Any:
        # Allow POSTing one bare arrangement object directly to /editor/schedule.
        if isinstance(data, dict):
            has_list = any(k in data for k in _GROUP_LIST_KEYS)
            looks_like_item = "manuscript_id" in data or "manuscript" in data
            if not has_list and looks_like_item:
                deadline_value = next(
                    (data[k] for k in _DEADLINE_KEYS if k in data), None
                )
                return {"items": [data], "confirmation_deadline": deadline_value}
        return data

    @model_validator(mode="after")
    def _normalise_deadline(self) -> "ScheduleGroupCreate":
        self.confirmation_deadline = _as_utc(self.confirmation_deadline)
        return self


class ScheduleResponseInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    item_id: int | None = Field(
        default=None, validation_alias=AliasChoices("item_id", "arrangement_id")
    )
    accept: bool | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "accept",
            "accepted",
            "agree",
            "agreed",
            "confirm",
            "confirmed",
            "approve",
            "approved",
            "will_present",
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _normalise_decision_word(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "accept" not in data:
                word = data.get("decision") or data.get("response") or data.get("answer")
                if isinstance(word, str):
                    lowered = word.strip().lower()
                    if lowered in {"accept", "accepted", "yes", "agree", "confirmed", "approve"}:
                        return {**data, "accept": True}
                    if lowered in {
                        "decline",
                        "declined",
                        "reject",
                        "rejected",
                        "no",
                        "disagree",
                        "cancel",
                    }:
                        return {**data, "accept": False}
        return data

    @model_validator(mode="after")
    def _require_choice(self) -> "ScheduleResponseInput":
        if self.accept is None:
            raise ValueError("an explicit boolean accept/confirm choice is required")
        return self


# ---------------------------------------------------------------------------
# Speaker travel reimbursement — every monetary field is integer cents.
# ---------------------------------------------------------------------------

class AllowanceCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    manuscript_id: int
    amount_cents: StrictInt = Field(gt=0)
    # Optional; when present it must equal the confirmed talk's decision.
    decision_id: int | None = None


class ReceiptCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    voucher_no: str = Field(min_length=1, max_length=120)
    author_id: int
    amount_cents: StrictInt = Field(gt=0)


class ClaimCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    allowance_id: int
    receipt_id: int
    amount_cents: StrictInt = Field(gt=0)


class BatchItemInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    claim_id: int
    amount_cents: StrictInt = Field(gt=0)


_BATCH_KEY_KEYS = (
    "idempotency_key",
    "request_id",
    "batch_no",
    "batch_key",
    "request_no",
)


class BatchCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    idempotency_key: str = Field(
        min_length=1, max_length=120, validation_alias=AliasChoices(*_BATCH_KEY_KEYS)
    )
    items: list[BatchItemInput] = Field(min_length=1)


class PaymentResultInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    payment_no: str = Field(min_length=1, max_length=120)
    paid_cents: StrictInt = Field(ge=0)
    note: str | None = Field(default=None, max_length=500)


class BatchResultsInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    results: list[PaymentResultInput] = Field(min_length=1)

