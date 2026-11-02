"""Collect, store and serve repository intelligence.

All GitHub access goes through GitHubClient (retries, rate limits, ETag cache). This module decides
what to fetch, tolerates partial failure per signal, and stores results in existing tables:
  * repository_snapshots.data  - repository observations + documentation detection (analysis_version intel_v1)
  * issue_features             - per-issue linked-PR info (feature_version readiness_v1)
Scores are recomputed from stored observations at read time; they are heuristics, not ML.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.github_errors import to_app_error
from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import Issue, IssueFeature, Repository, RepositorySnapshot
from app.intelligence import activity as act
from app.intelligence import readiness as rd
from app.intelligence.config import ANALYSIS_VERSION, READINESS_FEATURE_VERSION, UNKNOWN_SHA, IntelConfig
from app.intelligence.scoring import Report
from app.services import repositories as repo_service
from app.services.singleflight import locks
from app.services.github import GitHubClient
from app.services.github.errors import GitHubError, GitHubRateLimitError

SCHEMA_VERSION = 1


def intel_config(settings: Settings) -> IntelConfig:
    return IntelConfig(window_days=settings.intel_window_days, snapshot_ttl_seconds=settings.intel_snapshot_ttl_seconds,
                       unknown_fill=settings.reco_unknown_fill, issue_max_age_days=settings.reco_max_data_age_days)


def utcnow() -> datetime:
    return repo_service.utcnow()


def _age_seconds(ts: datetime, now: datetime) -> float:
    return (now - act.aware(ts)).total_seconds()


def _code(exc: Exception) -> str:
    return exc.code if isinstance(exc, AppError) else to_app_error(exc).code  # type: ignore[arg-type]


# ------------------------------------------------------------------ repository snapshots


def latest_snapshot(db: Session, repository_id: int) -> RepositorySnapshot | None:
    return db.scalar(
        select(RepositorySnapshot)
        .where(RepositorySnapshot.repository_id == repository_id, RepositorySnapshot.analysis_version == ANALYSIS_VERSION)
        .order_by(RepositorySnapshot.collected_at.desc(), RepositorySnapshot.id.desc()).limit(1)
    )


def snapshot_is_fresh(snap: RepositorySnapshot, settings: Settings, now: datetime, refresh: bool) -> bool:
    """Complete snapshots live `intel_snapshot_ttl_seconds`; partial ones only the normal sync TTL, so a
    rate-limited collection is retried soon. `refresh` is honoured only for data older than the min-refresh floor."""
    complete = bool((snap.data or {}).get("complete"))
    ttl = settings.github_min_refresh_seconds if refresh else (
        settings.intel_snapshot_ttl_seconds if complete else settings.github_sync_ttl_seconds)
    return _age_seconds(snap.collected_at, now) < ttl


def snapshot_is_stale(snap: RepositorySnapshot, cfg: IntelConfig, now: datetime) -> bool:
    return _age_seconds(snap.collected_at, now) > cfg.max_snapshot_age_days * 86400


def collect_repository(db: Session, gh: GitHubClient, settings: Settings, repo: Repository, now: datetime) -> RepositorySnapshot:
    """Fetch every signal (bounded pages), tolerating per-signal failure. Raises only if NOTHING could be fetched."""
    cfg = intel_config(settings)
    owner, name = repo.owner, repo.name
    # Aligned to midnight UTC so the `since=` parameter is identical all day: repeated collections then send identical
    # URLs and GitHub can answer conditional requests (If-None-Match) with 304 instead of resending the data.
    start = (now - timedelta(days=cfg.window_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    errors: dict[str, str] = {}
    first_exc: GitHubError | None = None
    state = {"rate_limited": False, "attempted": 0}

    def attempt(signal: str, fn):
        nonlocal first_exc
        if state["rate_limited"]:
            errors[signal] = "skipped_after_rate_limit"  # stop hammering GitHub once it says wait
            return None
        state["attempted"] += 1
        try:
            return fn()
        except GitHubRateLimitError as exc:
            state["rate_limited"] = True
            first_exc = first_exc or exc
            errors[signal] = _code(exc)
        except GitHubError as exc:
            first_exc = first_exc or exc
            errors[signal] = _code(exc)
        return None

    commits = attempt("commits", lambda: gh.list_commits(owner, name, max_pages=cfg.commit_pages))
    head_sha = commits.items[0].sha if commits and commits.items else None
    releases = attempt("releases", lambda: gh.list_releases(owner, name, max_pages=1))
    issues = attempt("issues", lambda: gh.list_issues(owner, name, state="all", since=start, max_pages=cfg.issue_pages))
    pulls = attempt("pulls", lambda: gh.list_pull_requests(owner, name, state="all", max_pages=cfg.pull_pages))
    comments = attempt("comments", lambda: gh.list_repo_issue_comments(owner, name, since=start, max_pages=cfg.comment_pages))
    tree = attempt("tree", lambda: gh.get_git_tree(owner, name, head_sha or repo.default_branch or "HEAD"))

    docs: dict = {"tree_status": "unavailable", "error": errors.get("tree", "not_collected")}
    if tree is not None:
        docs = rd.analyze_tree(tree)
        files = {}
        if docs["readme"]:
            files["README"] = attempt("readme", lambda: gh.get_file_text(owner, name, docs["readme"], ref=head_sha))
        if docs["contributing"]:
            files["CONTRIBUTING"] = attempt(
                "contributing", lambda: gh.get_file_text(owner, name, docs["contributing"]["path"], ref=head_sha))
        docs = {**docs, **rd.analyze_instructions({"README": files.get("README"), "CONTRIBUTING": files.get("CONTRIBUTING")})}

    if state["attempted"] and len(errors) >= state["attempted"] and tree is None and commits is None and first_exc:
        raise first_exc  # nothing at all was retrievable: let the caller map the real error

    def section(label: str, result, build, needs=()):
        if result is None:
            return act.unavailable(errors.get(label, "not_collected"))
        return build(result)

    obs = {
        "commits": section("commits", commits, lambda r: act.summarize_commits(r.items, r.truncated, now, cfg.window_days)),
        "releases": section("releases", releases, lambda r: act.summarize_releases(r.items, now, cfg.window_days)),
        "issues": section("issues", issues, lambda r: act.summarize_issues(r.items, r.truncated, now, cfg.window_days)),
        "pulls": section("pulls", pulls, lambda r: act.summarize_pulls(r.items, r.truncated, now, cfg.window_days)),
    }
    if issues is not None and comments is not None:
        obs["responses"] = act.summarize_responses(issues.items, pulls.items if pulls else None, comments.items,
                                                   comments.truncated, now, cfg)
    else:
        obs["responses"] = act.unavailable(errors.get("comments") or errors.get("issues", "not_collected"))
    obs["maintenance"] = act.summarize_maintenance(repo.archived, repo.license_spdx, repo.pushed_at, docs)

    data = {
        "schema": SCHEMA_VERSION, "analysis_version": ANALYSIS_VERSION, "collected_at": act.iso(now),
        "window_days": cfg.window_days, "complete": not errors, "errors": errors,
        "head_sha": head_sha, "default_branch": repo.default_branch, "observations": obs, "docs": docs,
        "pages": {"commits": getattr(commits, "pages_fetched", 0), "issues": getattr(issues, "pages_fetched", 0),
                  "pulls": getattr(pulls, "pages_fetched", 0), "comments": getattr(comments, "pages_fetched", 0)},
    }
    sha = head_sha or (tree.sha if tree else None) or UNKNOWN_SHA
    return _store_snapshot(db, repo, sha, data, now)


def _store_snapshot(db: Session, repo: Repository, sha: str, data: dict, now: datetime) -> RepositorySnapshot:
    for _ in range(2):
        row = db.scalar(select(RepositorySnapshot).where(
            RepositorySnapshot.repository_id == repo.id, RepositorySnapshot.commit_sha == sha,
            RepositorySnapshot.analysis_version == ANALYSIS_VERSION))
        if row is None:
            row = RepositorySnapshot(repository_id=repo.id, commit_sha=sha, analysis_version=ANALYSIS_VERSION,
                                     collected_at=now, data=data)
            db.add(row)
        elif (row.data or {}).get("complete") and not data["complete"]:
            return row  # never replace a complete analysis of the same commit with a partial one
        else:
            row.data, row.collected_at = data, now
        try:
            db.commit()
            return row
        except IntegrityError:  # concurrent collection stored the same key first
            db.rollback()
    raise AppError("sync_conflict", "Concurrent analysis conflict; please retry", 503)


def ensure_repo_intelligence(
    db: Session, gh: GitHubClient, settings: Settings, repo: Repository, *, refresh: bool = False
) -> tuple[RepositorySnapshot | None, str | None]:
    """Latest snapshot, collecting only if missing/stale. Returns (snapshot, refresh_error_code).

    If collection fails but an older snapshot exists, that snapshot is returned along with the error code;
    with no snapshot and a total failure the GitHub error is raised for the API layer to map."""
    now = utcnow()
    snap = latest_snapshot(db, repo.id)
    if snap is not None and snapshot_is_fresh(snap, settings, now, refresh):
        return snap, None
    repo_service.release_snapshot(db)
    with locks.hold(("intel", repo.id)):  # one analysis per repository at a time; waiters reuse its result
        repo_service.release_snapshot(db)
        now = utcnow()
        snap = latest_snapshot(db, repo.id)
        if snap is not None and snapshot_is_fresh(snap, settings, now, refresh):
            return snap, None
        try:
            return collect_repository(db, gh, settings, repo, now), None
        except GitHubError as exc:
            if snap is None:
                raise
            return snap, _code(exc)


def health_report(snap: RepositorySnapshot, settings: Settings, now: datetime | None = None) -> Report:
    return act.compute_health((snap.data or {}).get("observations", {}), now or utcnow(), intel_config(settings))


# ------------------------------------------------------------------ issue linked PRs


def get_linked_prs_row(db: Session, issue_id: int) -> IssueFeature | None:
    return db.scalar(select(IssueFeature).where(IssueFeature.issue_id == issue_id,
                                                IssueFeature.feature_version == READINESS_FEATURE_VERSION))


def ensure_linked_prs(
    db: Session, gh: GitHubClient, settings: Settings, repo: Repository, issue: Issue, *, refresh: bool = False
) -> tuple[dict | None, str | None]:
    """Linked-PR info for one issue (cross-references from the timeline). Returns (data, refresh_error_code)."""
    now = utcnow()
    row = get_linked_prs_row(db, issue.id)
    ttl = settings.github_min_refresh_seconds if refresh else settings.intel_linked_pr_ttl_seconds
    if row is not None and _age_seconds(row.created_at, now) < ttl:
        return row.structured_features, None
    repo_service.release_snapshot(db)
    issue_id, issue_number = issue.id, issue.issue_number
    with locks.hold(("linked", issue_id)):  # concurrent requests for this issue share ONE timeline lookup
        repo_service.release_snapshot(db)
        now = utcnow()
        row = get_linked_prs_row(db, issue_id)
        if row is not None and _age_seconds(row.created_at, now) < ttl:
            return row.structured_features, None
        try:
            res = gh.list_issue_timeline(repo.owner, repo.name, issue_number, max_pages=3)
        except GitHubError as exc:
            return (row.structured_features if row else None), _code(exc)
        return _store_linked(db, repo, issue_id, res, now)


def _store_linked(db: Session, repo: Repository, issue_id: int, res, now: datetime) -> tuple[dict | None, str | None]:
    data = {"schema": SCHEMA_VERSION, "collected_at": act.iso(now), "timeline_truncated": res.truncated,
            "pages_fetched": res.pages_fetched, **rd.classify_linked_prs(res.items, f"{repo.owner}/{repo.name}")}
    for _ in range(2):
        row = get_linked_prs_row(db, issue_id)
        if row is None:
            row = IssueFeature(issue_id=issue_id, feature_version=READINESS_FEATURE_VERSION, structured_features=data,
                               extracted_skills=[], created_at=now)
            db.add(row)
        else:
            row.structured_features, row.created_at = data, now
        try:
            db.commit()
            return data, None
        except IntegrityError:
            db.rollback()
    return data, "sync_conflict"


# ------------------------------------------------------------------ readiness assembly


def issue_inputs(issue: Issue) -> dict:
    return {"title": issue.title, "body": issue.body, "labels": [l.get("name", "") for l in (issue.labels or []) if isinstance(l, dict)],
            "state": issue.state, "assignees": list(issue.assignees or []), "fetched_at": act.iso(issue.fetched_at)}


def readiness_report(issue: Issue, snap: RepositorySnapshot | None, linked: dict | None, settings: Settings,
                     now: datetime | None = None) -> Report:
    cfg = intel_config(settings)
    now = now or utcnow()
    docs = (snap.data or {}).get("docs") if snap is not None else None
    if snap is not None and snapshot_is_stale(snap, cfg, now):
        docs = None  # too old to trust: treated as not collected
    return rd.compute_readiness(issue_inputs(issue), docs, linked, now, cfg)


def find_issue_rows(db: Session, gh: GitHubClient, settings: Settings, owner: str, repo_name: str, number: int
                    ) -> tuple[Repository, Issue]:
    """Stored repository + issue; the issue is fetched (and a 404 mapped) only if not stored yet."""
    repo = repo_service.get_or_sync_repository(db, gh, settings, owner, repo_name)
    found = repo_service.find_issue(db, repo.owner, repo.name, number)
    if found is None:
        repo_service.get_issue(db, gh, settings, owner, repo_name, number)
        found = repo_service.find_issue(db, repo.owner, repo.name, number)
    return found  # type: ignore[return-value]
