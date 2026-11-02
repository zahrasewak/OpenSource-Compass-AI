"""Data types shared by every scorer (heuristic now, ML later).

A scorer takes `UserFeatures` + `IssueFeatures` (plain data, no DB/HTTP objects) and returns a
`ScoredIssue`. Keeping this contract stable lets a trained model be evaluated against the
heuristic baseline on identical inputs.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

COMPONENT_NAMES = (
    "skill_compatibility",
    "interest_similarity",
    "difficulty_suitability",
    "repository_activity",
    "contribution_readiness",
)


@dataclass(frozen=True)
class UserFeatures:
    has_profile: bool
    skills: dict[str, float] = field(default_factory=dict)  # canonical skill -> self-reported proficiency 0..1
    interests: tuple[str, ...] = ()
    preferred_task_types: tuple[str, ...] = ()
    target_difficulty: int | None = None  # 1..5, None = unknown
    target_difficulty_source: str | None = None  # "request" | "profile" | "experience_level"


@dataclass(frozen=True)
class IssueFeatures:
    owner: str
    repo: str
    number: int
    title: str
    body: str | None
    labels: tuple[str, ...]
    state: str
    assignees: tuple[str, ...]
    issue_fetched_at: datetime
    repo_language: str | None
    repo_topics: tuple[str, ...]
    repo_description: str | None
    repo_archived: bool
    repo_pushed_at: datetime | None
    repo_fetched_at: datetime | None
    # Stored repository-intelligence reports (health/readiness) for scorers that use them (heuristic_v2).
    # None for v1, and then omitted from stored snapshots so v1 hashes/history are unchanged.
    intel: dict | None = None

    @property
    def html_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/issues/{self.number}"


@dataclass(frozen=True)
class ComponentScore:
    """One 0-100 component. `score is None` means the information needed was unavailable."""

    name: str
    weight: float
    score: float | None
    confidence: float  # 0..1, how much evidence supports `score`; forced to 0 when score is None
    status: str  # "known" | "unknown" | "stale"
    evidence: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    effective_score: float = 0.0  # score actually used in the final sum (see heuristic.combine)


@dataclass(frozen=True)
class ModelInfo:
    name: str
    version: str
    kind: str  # "heuristic" | "ml"
    is_ml_prediction: bool
    description: str

    @property
    def model_id(self) -> str:
        """Stable identifier stored with every persisted result, e.g. "heuristic_v1"."""
        return f"{self.kind}_{self.version}"


@dataclass(frozen=True)
class ScoredIssue:
    final_score: float  # 0..100, NOT a probability
    coverage: float  # 0..1, share of the weighting backed by real evidence
    insufficient_information: bool
    components: dict[str, ComponentScore]
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    missing_skills: tuple[str, ...]
    model: ModelInfo


class Scorer(Protocol):
    """Interface every recommendation scorer implements."""

    info: ModelInfo

    def score(self, user: UserFeatures, issue: IssueFeatures, now: datetime) -> ScoredIssue: ...
