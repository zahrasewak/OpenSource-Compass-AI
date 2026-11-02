"""Responses for repository health and issue readiness."""

from datetime import datetime

from pydantic import BaseModel


class SignalOut(BaseModel):
    group: str
    weight: float
    score: float | None  # 0-100; null = unknown / stale / not applicable
    confidence: float
    status: str  # known | unknown | stale | not_applicable
    effective_score: float | None  # value used in the report score (null when not applicable)
    evidence: list[str]
    warnings: list[str]


class ReportOut(BaseModel):
    kind: str
    score: float | None
    label: str
    coverage: float  # share of applicable weight backed by real evidence
    group_scores: dict[str, float | None]
    warnings: list[str]
    notes: list[str]
    signals: dict[str, SignalOut]


class ScoringNote(BaseModel):
    kind: str = "heuristic"
    is_ml_prediction: bool = False
    version: str = "intel_v1"
    note: str = ("Heuristic summary from observed GitHub data. Not an ML prediction or a calibrated probability; "
                 "the formula has not been validated against outcomes.")


class RepoRef(BaseModel):
    owner: str
    name: str
    full_name: str


class FreshnessOut(BaseModel):
    collected_at: datetime | None
    age_hours: float | None
    window_days: int | None
    head_sha: str | None
    complete: bool | None
    stale: bool
    errors: dict[str, str]  # signal -> error code for anything that could not be retrieved
    refresh_error: str | None = None  # set when a refresh failed and older stored data was served


class HealthOut(BaseModel):
    repository: RepoRef
    scoring: ScoringNote
    report: ReportOut
    freshness: FreshnessOut
    observations: dict  # raw stored observations the report was computed from


class IssueSummary(BaseModel):
    number: int
    title: str
    state: str
    html_url: str


class ReadinessFreshness(BaseModel):
    repository_snapshot: FreshnessOut | None
    linked_prs_collected_at: datetime | None


class ReadinessOut(BaseModel):
    repository: RepoRef
    issue: IssueSummary
    scoring: ScoringNote
    report: ReportOut
    freshness: ReadinessFreshness
    linked_pull_requests: dict | None  # classified cross-references; null = not checked
    repository_docs: dict | None  # detection summary; files were NOT executed
    upstream_errors: dict[str, str]
