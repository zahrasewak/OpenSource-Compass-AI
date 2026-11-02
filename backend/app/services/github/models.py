"""Typed, validated views of the GitHub payloads this project uses.

Only the fields we need are modelled; unknown fields are ignored. Missing optional data stays
None so downstream code can treat it as *unknown* rather than as a value.
"""

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")


def _login(data: dict, key: str) -> str | None:
    value = data.get(key)
    return value.get("login") if isinstance(value, dict) else None


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RateLimit(_Model):
    limit: int | None = None
    remaining: int | None = None
    reset_epoch: int | None = None


class Repository(_Model):
    owner: str
    name: str
    full_name: str
    html_url: str
    default_branch: str
    description: str | None = None
    language: str | None = None
    topics: list[str] = Field(default_factory=list)
    stargazers_count: int | None = None
    forks_count: int | None = None
    open_issues_count: int | None = None  # includes open pull requests (GitHub semantics)
    archived: bool = False
    fork: bool = False
    private: bool | None = None  # None = GitHub did not say; callers must not treat that as public
    has_issues: bool | None = None
    license_spdx: str | None = None
    created_at: datetime | None = None
    pushed_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, d: Any) -> Any:
        if not isinstance(d, dict):
            return d
        d = dict(d)
        d["owner"] = _login(d, "owner")
        lic = d.get("license")
        d["license_spdx"] = lic.get("spdx_id") if isinstance(lic, dict) else None
        return d


class Label(_Model):
    name: str
    description: str | None = None
    color: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_string(cls, d: Any) -> Any:
        return {"name": d} if isinstance(d, str) else d


class Issue(_Model):
    number: int
    title: str
    body: str | None = None
    state: str
    html_url: str
    labels: list[Label] = Field(default_factory=list)
    author: str | None = None
    author_association: str | None = None
    assignees: list[str] = Field(default_factory=list)
    comments: int | None = None
    is_pull_request: bool = False
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, d: Any) -> Any:
        if not isinstance(d, dict):
            return d
        d = dict(d)
        d["author"] = _login(d, "user")
        d["assignees"] = [a["login"] for a in d.get("assignees") or [] if isinstance(a, dict) and "login" in a]
        d["is_pull_request"] = "pull_request" in d
        return d


class IssueComment(_Model):
    id: int
    author: str | None = None
    author_association: str | None = None
    body: str | None = None
    created_at: datetime
    issue_number: int | None = None  # parsed from issue_url; present on repository-wide comment listings

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, d: Any) -> Any:
        if isinstance(d, dict):
            number = None
            url = d.get("issue_url")
            if isinstance(url, str) and url.rsplit("/", 1)[-1].isdigit():
                number = int(url.rsplit("/", 1)[-1])
            d = {**d, "author": _login(d, "user"), "issue_number": number}
        return d


class IssueEvent(_Model):
    id: int
    event: str
    actor: str | None = None
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, d: Any) -> Any:
        if isinstance(d, dict):
            d = {**d, "actor": _login(d, "actor")}
        return d


class PullRequestSummary(_Model):
    number: int
    title: str
    state: str
    author: str | None = None
    author_association: str | None = None
    created_at: datetime
    closed_at: datetime | None = None
    merged_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, d: Any) -> Any:
        if isinstance(d, dict):
            d = {**d, "author": _login(d, "user")}
        return d


class CommitSummary(_Model):
    sha: str
    author: str | None = None
    committed_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, d: Any) -> Any:
        if not isinstance(d, dict):
            return d
        commit = d.get("commit") if isinstance(d.get("commit"), dict) else {}
        committer = commit.get("committer") if isinstance(commit.get("committer"), dict) else {}
        return {"sha": d.get("sha"), "author": _login(d, "author"), "committed_at": committer.get("date")}


class Release(_Model):
    tag_name: str
    draft: bool = False
    prerelease: bool = False
    published_at: datetime | None = None


class TreeEntry(_Model):
    path: str
    type: str  # "blob" (file) | "tree" (directory) | "commit" (submodule)


class GitTree(_Model):
    """Recursive file listing of one commit/branch. `truncated` means GitHub cut the listing short."""

    sha: str
    truncated: bool = False
    entries: list[TreeEntry] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _rename(cls, d: Any) -> Any:
        if isinstance(d, dict):
            d = {**d, "entries": d.get("tree", [])}
        return d


class FileText(_Model):
    """Decoded text of a small repository file. `text` is None when the file was too large to read."""

    path: str
    size: int | None = None
    text: str | None = None


class TimelineReference(_Model):
    """A "cross-referenced" timeline event: another issue/PR mentioned this issue.

    A mention is NOT proof that the referencing PR fixes the issue.
    """

    source_number: int
    source_is_pull_request: bool
    source_state: str  # "open" | "closed"
    source_merged_at: datetime | None = None
    source_repository: str | None = None  # "owner/name"
    source_html_url: str | None = None
    created_at: datetime | None = None


class PagedResult(BaseModel, Generic[T]):
    """Items from a paginated endpoint. `truncated` is True if more pages existed than we fetched."""

    items: list[T]
    truncated: bool = False
    pages_fetched: int = 0
