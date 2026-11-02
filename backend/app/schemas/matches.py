"""Request/response models for POST /matches/search."""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

_REF = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class MatchSearchRequest(BaseModel):
    """Search options. The user is always the authenticated caller; unknown fields (e.g. user_id) are rejected."""

    model_config = ConfigDict(extra="forbid")

    repositories: list[str] = Field(
        default_factory=list, max_length=5,
        description="Optional 'owner/name' list. These are synchronised from GitHub if missing/stale. "
                    "If empty, only already-stored repositories are searched (no GitHub calls).",
    )
    language: Short | None = Field(default=None, description="Keep only repositories with this primary language")
    topic: Short | None = Field(default=None, description="Keep only repositories with this topic")
    preferred_difficulty: int | None = Field(
        default=None, ge=1, le=5, description="Overrides the profile's difficulty target for this search only"
    )
    max_inactive_days: int | None = Field(
        default=None, ge=1, le=3650,
        description="Exclude repositories not pushed to within N days (repositories with no push date are excluded)",
    )
    include_archived: bool = False
    scorer: Literal["heuristic_v1", "heuristic_v2"] = Field(
        default="heuristic_v1",
        description="Scoring method. v1 is the default. v2 adds repository health and readiness signals and is "
                    "experimental: it has not been evaluated and is not claimed to be better.",
    )
    persist: bool = Field(
        default=True,
        description="Store the returned page of results (idempotent: identical results are stored once). "
                    "Set false for a dry run.",
    )
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0)

    @field_validator("repositories")
    @classmethod
    def _valid_refs(cls, refs: list[str]) -> list[str]:
        seen: set[str] = set()
        for ref in refs:
            owner, _, name = ref.partition("/")
            if not _REF.match(ref) or owner in {".", ".."} or name in {".", ".."}:
                raise ValueError(f"invalid repository reference {ref!r}; expected 'owner/name'")
            if ref.lower() in seen:
                raise ValueError(f"duplicate repository {ref!r}")
            seen.add(ref.lower())
        return refs


class ModelOut(BaseModel):
    name: str
    version: str
    kind: str
    is_ml_prediction: bool
    description: str
    score_note: str


class ComponentOut(BaseModel):
    score: float | None  # 0-100, null = unknown
    status: str  # known | unknown | stale
    weight: float
    confidence: float  # 0-1 evidence strength
    effective_score: float  # value actually used in the final sum
    evidence: list[str]
    warnings: list[str]


class RepoRefOut(BaseModel):
    owner: str
    name: str
    full_name: str


class IssueRefOut(BaseModel):
    number: int
    title: str
    html_url: str


class MatchCore(BaseModel):
    """One scored issue (everything except its position in a ranking)."""

    match_id: int | None = None  # set when the result was persisted
    generated_at: datetime | None = None
    repository: RepoRefOut
    issue: IssueRefOut
    final_score: float
    coverage: float
    insufficient_information: bool
    components: dict[str, ComponentOut]
    reasons: list[str]
    missing_skills: list[str]
    warnings: list[str]


class MatchItemOut(MatchCore):
    rank: int


class ReliabilityOut(BaseModel):
    status: str  # ok | limited | insufficient | no_candidates
    reasons: list[str]


class UpstreamErrorOut(BaseModel):
    repository: str
    code: str
    message: str


class MatchSearchOut(BaseModel):
    model: ModelOut
    weights: dict[str, float]
    ranking_reliability: ReliabilityOut
    total: int
    limit: int
    offset: int
    candidates_considered: int
    candidates_truncated: bool
    items: list[MatchItemOut]
    upstream_errors: list[UpstreamErrorOut]


class StoredModelOut(BaseModel):
    id: str  # e.g. "heuristic_v1"
    name: str | None = None
    version: str | None = None
    kind: str | None = None
    is_ml_prediction: bool | None = None


class StoredMatchSummaryOut(BaseModel):
    id: int
    generated_at: datetime
    model: StoredModelOut
    repository: RepoRefOut
    issue: IssueRefOut
    final_score: float
    coverage: float | None
    insufficient_information: bool
    component_scores: dict[str, float | None]  # raw 0-100; null = unknown at the time


class StoredMatchOut(StoredMatchSummaryOut):
    """Full historical record. Everything below was stored at generation time and is never recomputed."""

    components: dict[str, ComponentOut]
    reasons: list[str]
    missing_skills: list[str]
    warnings: list[str]
    scoring_config: dict
    data_snapshot: dict
    note: str


class StoredMatchListOut(BaseModel):
    items: list[StoredMatchSummaryOut]
    total: int
    limit: int
    offset: int


class IssueMatchRequest(BaseModel):
    """Options for scoring ONE issue for the caller. Unknown fields (e.g. user_id) are rejected."""

    model_config = ConfigDict(extra="forbid")

    scorer: Literal["heuristic_v1", "heuristic_v2"] = "heuristic_v1"
    persist: bool = Field(default=True, description="Store the result (idempotent) so feedback can reference its match_id")


class IssueMatchOut(BaseModel):
    model: ModelOut
    weights: dict[str, float]
    ranking_reliability: ReliabilityOut
    match: MatchCore
