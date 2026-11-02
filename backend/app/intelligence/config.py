"""Parameters and weights of the repository-intelligence analyzers (untuned defaults, not validated)."""

from dataclasses import asdict, dataclass

# Repository health: weights per signal. Signals that are "not_applicable" are excluded and the rest renormalised.
HEALTH_WEIGHTS = {
    "commit_activity": 0.30,
    "release_cadence": 0.10,
    "issue_pr_flow": 0.15,
    "maintainer_responsiveness": 0.30,
    "maintenance_indicators": 0.15,
}
HEALTH_GROUPS = {
    "commit_activity": "activity",
    "release_cadence": "activity",
    "issue_pr_flow": "activity",
    "maintainer_responsiveness": "responsiveness",
    "maintenance_indicators": "maintenance",
}

# Issue readiness: weights per signal.
READINESS_WEIGHTS = {
    "contribution_guide": 0.20,
    "tests": 0.20,
    "issue_description": 0.20,
    "linked_pull_requests": 0.20,
    "labels": 0.05,
    "availability": 0.10,
    "other_documentation": 0.05,
}

MAINTAINER_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
ANALYSIS_VERSION = "intel_v1"
READINESS_FEATURE_VERSION = "readiness_v1"
UNKNOWN_SHA = "0" * 40  # stored when neither a head commit nor a tree SHA could be obtained


@dataclass(frozen=True)
class IntelConfig:
    window_days: int = 90  # observation window for commits / issues / PRs / responses
    unknown_fill: float = 25.0  # score used in place of missing or low-confidence evidence
    min_coverage: float = 0.4  # below this a report is labelled "insufficient_data"
    response_grace_days: int = 7  # an unanswered item younger than this is "pending", not "ignored"
    snapshot_ttl_seconds: int = 24 * 3600  # complete snapshots are reused for this long without calling GitHub
    max_snapshot_age_days: int = 14  # older snapshots are treated as stale (unknown) when scoring
    linked_pr_max_age_days: int = 3  # linked-PR info changes quickly
    issue_max_age_days: int = 7
    commit_pages: int = 3
    issue_pages: int = 3
    pull_pages: int = 3
    comment_pages: int = 3

    def describe(self) -> dict:
        return {**asdict(self), "health_weights": HEALTH_WEIGHTS, "readiness_weights": READINESS_WEIGHTS}
