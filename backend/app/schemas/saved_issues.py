"""Saved-issue request/response models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.refs import check_name


class SaveIssueRequest(BaseModel):
    """Reference an issue by GitHub identity. `match_id` optionally records which match led to the save."""

    model_config = ConfigDict(extra="forbid")

    owner: str
    repo: str
    number: int = Field(ge=1, le=2_147_483_647)
    match_id: int | None = Field(default=None, ge=1)

    _v = field_validator("owner", "repo")(check_name)


class SavedIssueOut(BaseModel):
    id: int
    saved_at: datetime
    created: bool | None = None  # only set on POST: False = it was already saved
    owner: str
    repo: str
    number: int
    title: str
    state: str
    html_url: str
    issue_fetched_at: datetime  # freshness of the stored issue data; it may have changed on GitHub since


class SavedIssueListOut(BaseModel):
    items: list[SavedIssueOut]
    total: int
    limit: int
    offset: int


class SavedLookupOut(BaseModel):
    """Whether the caller has saved a given issue (used to show the saved state when a page opens)."""

    saved: bool
    saved_issue: SavedIssueOut | None = None
