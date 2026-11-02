"""Feedback request/response models.

Two kinds of records share the table but are never mixed up:
  * explicit_feedback - the user's stated opinion (relevance, difficulty, interest)
  * event             - something that happened: `saved`/`unsaved` (system observed) and
                        `attempted`/`contributed` (SELF-REPORTED, unverified claims)
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.refs import check_name


class FeedbackType(str, Enum):
    relevance = "relevance"
    difficulty = "difficulty"
    interest = "interest"
    attempted = "attempted"
    contributed = "contributed"


# public value name -> stored integer
VALUES: dict[str, dict[str, int]] = {
    "relevance": {"relevant": 1, "not_relevant": -1},
    "difficulty": {"too_easy": -1, "appropriate": 0, "too_difficult": 1},
    "interest": {"interested": 1, "not_interested": -1},
    "attempted": {"yes": 1},
    "contributed": {"yes": 1},
}
# stored (category, source) per type. `saved`/`unsaved` are written by the system, not accepted here.
CLASSIFICATION = {
    "relevance": ("explicit_feedback", "user"),
    "difficulty": ("explicit_feedback", "user"),
    "interest": ("explicit_feedback", "user"),
    "attempted": ("event", "self_reported"),
    "contributed": ("event", "self_reported"),
    "saved": ("event", "system"),
    "unsaved": ("event", "system"),
}
SYSTEM_VALUES = {"saved": {1: "yes"}, "unsaved": {0: "yes"}}


def value_name(feedback_type: str, stored: int) -> str:
    table = VALUES.get(feedback_type) or SYSTEM_VALUES.get(feedback_type, {})
    inverse = {v: k for k, v in table.items()} if feedback_type in VALUES else table
    return inverse.get(stored, str(stored))


class FeedbackRequest(BaseModel):
    """Target is a stored match (`match_id`) and/or an issue reference. The user is the bearer-token identity."""

    model_config = ConfigDict(extra="forbid")

    type: FeedbackType
    value: str | None = None
    match_id: int | None = Field(default=None, ge=1)
    owner: str | None = None
    repo: str | None = None
    number: int | None = Field(default=None, ge=1, le=2_147_483_647)

    @field_validator("owner", "repo")
    @classmethod
    def _names(cls, v: str | None) -> str | None:
        return check_name(v) if v is not None else v

    @model_validator(mode="after")
    def _consistent(self) -> "FeedbackRequest":
        allowed = VALUES[self.type.value]
        if self.value is None and len(allowed) == 1:
            self.value = next(iter(allowed))  # attempted/contributed: no value needed
        if self.value not in allowed:
            raise ValueError(f"value for '{self.type.value}' must be one of {sorted(allowed)}")
        ref_parts = [self.owner, self.repo, self.number]
        if any(p is not None for p in ref_parts) and not all(p is not None for p in ref_parts):
            raise ValueError("owner, repo and number must be given together")
        if self.match_id is None and self.owner is None:
            raise ValueError("provide match_id or owner/repo/number")
        return self


class FeedbackOut(BaseModel):
    id: int
    type: str
    value: str
    category: str
    source: str
    match_id: int | None
    owner: str
    repo: str
    number: int
    created_at: datetime
    duplicate: bool  # True: identical to your latest submission, nothing new was stored
    note: str | None = None
