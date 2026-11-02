"""Shared request fragments."""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def check_name(value: str) -> str:
    if not _NAME.match(value) or value in {".", ".."}:
        raise ValueError("invalid GitHub name")
    return value


class IssueRef(BaseModel):
    """A GitHub issue identified by owner, repository name and number (never by internal ids)."""

    model_config = ConfigDict(extra="forbid")

    owner: str
    repo: str
    number: int = Field(ge=1, le=2_147_483_647)

    _v = field_validator("owner", "repo")(check_name)


class IssueRefOut(BaseModel):
    owner: str
    repo: str
    number: int
    title: str
    state: str
    html_url: str
    fetched_at: str | None = None
