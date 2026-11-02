"""Response models for repository and issue endpoints.

These are explicit allow-lists: nothing from the upstream GitHub payload is passed through
unless it is declared here. No user-specific (profile) data appears in these models.
"""

from datetime import datetime
from typing import Annotated

from fastapi import Path
from pydantic import AfterValidator, BaseModel

# GitHub: names are [A-Za-z0-9_.-]; "." and ".." are not valid and would be path-traversal-like.
_NAME_PATTERN = r"^[A-Za-z0-9_.-]{1,100}$"


def _reject_dot_names(value: str) -> str:
    if value in {".", ".."}:
        raise ValueError("invalid name")
    return value


OwnerPath = Annotated[
    str, Path(pattern=_NAME_PATTERN, description="GitHub owner (user or organisation)"), AfterValidator(_reject_dot_names)
]
RepoPath = Annotated[
    str, Path(pattern=_NAME_PATTERN, description="GitHub repository name"), AfterValidator(_reject_dot_names)
]
IssueNumberPath = Annotated[int, Path(ge=1, le=2_147_483_647, description="Issue number")]


class LabelOut(BaseModel):
    name: str
    color: str | None = None
    description: str | None = None


class RepositoryOut(BaseModel):
    owner: str
    name: str
    full_name: str
    html_url: str
    description: str | None
    default_branch: str | None
    primary_language: str | None
    topics: list[str]
    stargazers_count: int | None
    forks_count: int | None
    open_issues_count: int | None
    archived: bool
    license_spdx: str | None
    pushed_at: datetime | None
    fetched_at: datetime | None  # when this data was last synchronised from GitHub
    issues_synced_at: datetime | None


class IssueOut(BaseModel):
    repository: str  # "owner/name"
    number: int
    title: str
    body: str | None
    state: str
    labels: list[LabelOut]
    author: str | None
    author_association: str | None
    assignees: list[str]
    comments_count: int | None
    html_url: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    fetched_at: datetime


class IssueListOut(BaseModel):
    items: list[IssueOut]
    total: int
    limit: int
    offset: int
    issues_synced_at: datetime | None
    # True if the last synchronisation hit the page limit, so the stored list may be incomplete.
    sync_truncated: bool | None
