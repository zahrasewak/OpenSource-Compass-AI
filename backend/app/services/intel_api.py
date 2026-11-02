"""Assemble the repository-health and issue-readiness API responses."""

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import RepositorySnapshot
from app.intelligence import activity as act
from app.schemas.intelligence import (
    FreshnessOut, HealthOut, IssueSummary, ReadinessFreshness, ReadinessOut, RepoRef, ReportOut, ScoringNote,
)
from app.services import intelligence as intel
from app.services import repositories as repo_service
from app.services.github import GitHubClient
from app.services.github.errors import GitHubError


def _freshness(snap: RepositorySnapshot | None, settings: Settings, now, refresh_error: str | None = None) -> FreshnessOut | None:
    if snap is None:
        return None
    data = snap.data or {}
    return FreshnessOut(
        collected_at=snap.collected_at, age_hours=round(intel._age_seconds(snap.collected_at, now) / 3600, 2),
        window_days=data.get("window_days"), head_sha=data.get("head_sha"), complete=data.get("complete"),
        stale=intel.snapshot_is_stale(snap, intel.intel_config(settings), now), errors=data.get("errors", {}),
        refresh_error=refresh_error,
    )


def repository_health(db: Session, gh: GitHubClient, settings: Settings, owner: str, repo: str, refresh: bool) -> HealthOut:
    row = repo_service.get_or_sync_repository(db, gh, settings, owner, repo)  # also enforces "public only"
    snap, refresh_error = intel.ensure_repo_intelligence(db, gh, settings, row, refresh=refresh)
    now = intel.utcnow()
    report = intel.health_report(snap, settings, now)  # type: ignore[arg-type]
    fresh = _freshness(snap, settings, now, refresh_error)
    out_report = report.as_dict()
    if fresh and fresh.stale:
        out_report["warnings"] = [*out_report["warnings"], "This analysis is older than the staleness limit; refresh it before relying on it."]
    return HealthOut(repository=RepoRef(owner=row.owner, name=row.name, full_name=f"{row.owner}/{row.name}"),
                     scoring=ScoringNote(), report=ReportOut(**out_report), freshness=fresh,  # type: ignore[arg-type]
                     observations=(snap.data or {}).get("observations", {}))


def _docs_summary(snap: RepositorySnapshot | None) -> dict | None:
    docs = (snap.data or {}).get("docs") if snap else None
    if not docs or docs.get("tree_status") == "unavailable":
        return None
    return {k: docs.get(k) for k in ("tree_status", "contributing", "readme", "code_of_conduct", "security_policy",
                                     "license_file", "pr_template", "issue_templates", "test_files", "ci_files",
                                     "instructions_status", "test_commands_documented", "readme_has_contributing_section")}


def issue_readiness(db: Session, gh: GitHubClient, settings: Settings, owner: str, repo: str, number: int,
                    refresh: bool) -> ReadinessOut:
    repo_row, issue = intel.find_issue_rows(db, gh, settings, owner, repo, number)
    upstream: dict[str, str] = {}
    snap = None
    try:
        snap, err = intel.ensure_repo_intelligence(db, gh, settings, repo_row, refresh=refresh)
        if err:
            upstream["repository_analysis"] = err
    except GitHubError as exc:  # readiness still returns what is known locally
        upstream["repository_analysis"] = intel._code(exc)
        snap = intel.latest_snapshot(db, repo_row.id)
    linked, err = intel.ensure_linked_prs(db, gh, settings, repo_row, issue, refresh=refresh)
    if err:
        upstream["linked_pull_requests"] = err
    now = intel.utcnow()
    report = intel.readiness_report(issue, snap, linked, settings, now)
    return ReadinessOut(
        repository=RepoRef(owner=repo_row.owner, name=repo_row.name, full_name=f"{repo_row.owner}/{repo_row.name}"),
        issue=IssueSummary(number=issue.issue_number, title=issue.title, state=issue.state,
                           html_url=f"https://github.com/{repo_row.owner}/{repo_row.name}/issues/{issue.issue_number}"),
        scoring=ScoringNote(), report=ReportOut(**report.as_dict()),
        freshness=ReadinessFreshness(repository_snapshot=_freshness(snap, settings, now, upstream.get("repository_analysis")),
                                     linked_prs_collected_at=act.parse(linked["collected_at"]) if linked else None),
        linked_pull_requests=linked, repository_docs=_docs_summary(snap), upstream_errors=upstream,
    )
