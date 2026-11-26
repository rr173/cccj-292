"""Pydantic request bodies."""

from __future__ import annotations

from pydantic import BaseModel, Field, StrictInt


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
