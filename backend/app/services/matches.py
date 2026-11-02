"""Recommendation search: loads data, applies filters, runs a Scorer, ranks and paginates.

Scoring itself lives in app.recommend (pure, swappable). This module touches the database and,
only for explicitly requested repositories, GitHub (via existing sync functions that skip the
network when stored data is fresh).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.github_errors import to_app_error
from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import Issue, IssueFeature, Repository, RepositorySnapshot, User, UserPreference, UserSkill
from app.intelligence import activity as act
from app.intelligence.config import ANALYSIS_VERSION, READINESS_FEATURE_VERSION
from app.recommend import IssueFeatures, Scorer, UserFeatures
from app.recommend.scoring_config import ScoringConfig
from app.recommend.vocabulary import canonical_skill
from app.schemas.matches import (
    ComponentOut,
    IssueMatchOut,
    IssueRefOut,
    MatchCore,
    MatchItemOut,
    MatchSearchOut,
    MatchSearchRequest,
    ModelOut,
    RepoRefOut,
    ReliabilityOut,
    UpstreamErrorOut,
)
from app.services import intelligence as intel_service
from app.services import match_store
from app.services import repositories as repo_service
from app.services.github import GitHubClient
from app.services.github.errors import GitHubError

EXPERIENCE_TO_DIFFICULTY = {"beginner": 2, "intermediate": 3, "advanced": 4}  # assumption, documented
SCORE_NOTE = (
    "Heuristic baseline, not an ML prediction. Scores use an arbitrary 0-100 scale, are not "
    "probabilities, and have not been validated against real contribution outcomes."
)


def load_user_features(db: Session, user_id: int, request_difficulty: int | None) -> UserFeatures:
    """Build features from the *authenticated* user's own profile; a missing profile yields an empty one."""
    prefs = db.scalar(select(UserPreference).where(UserPreference.user_id == user_id))
    skills = {
        canonical_skill(s.skill_name): s.proficiency
        for s in db.scalars(select(UserSkill).where(UserSkill.user_id == user_id))
    }
    target, source = None, None
    if request_difficulty is not None:
        target, source = request_difficulty, "request"
    elif prefs is not None and prefs.preferred_difficulty is not None:
        target, source = prefs.preferred_difficulty, "profile"
    elif prefs is not None and prefs.experience_level in EXPERIENCE_TO_DIFFICULTY:
        target, source = EXPERIENCE_TO_DIFFICULTY[prefs.experience_level], "experience_level"
    return UserFeatures(
        has_profile=prefs is not None or bool(skills),
        skills=skills,
        interests=tuple(prefs.interests or []) if prefs else (),
        preferred_task_types=tuple(prefs.preferred_task_types or []) if prefs else (),
        target_difficulty=target,
        target_difficulty_source=source,
    )


def _issue_features(issue: Issue, repo: Repository, intel: dict | None = None) -> IssueFeatures:
    return IssueFeatures(
        owner=repo.owner, repo=repo.name, number=issue.issue_number, title=issue.title, body=issue.body,
        labels=tuple(l.get("name", "") for l in (issue.labels or []) if isinstance(l, dict)),
        state=issue.state, assignees=tuple(issue.assignees or []), issue_fetched_at=issue.fetched_at,
        repo_language=repo.primary_language, repo_topics=tuple(repo.topics or []),
        repo_description=repo.description, repo_archived=bool(repo.archived),
        repo_pushed_at=repo.pushed_at, repo_fetched_at=repo.last_fetched_at, intel=intel,
    )


def attach_intelligence(db: Session, settings: Settings, pairs: list[tuple[Issue, Repository]], now: datetime) -> dict[int, dict]:
    """Stored health/readiness reports per candidate issue (keyed by Issue.id). Makes NO GitHub calls:
    anything not already collected is simply absent and scored as unknown."""
    if not pairs:
        return {}
    cfg = intel_service.intel_config(settings)
    repo_ids = {repo.id for _, repo in pairs}
    snaps: dict[int, RepositorySnapshot] = {}
    for snap in db.scalars(select(RepositorySnapshot).where(
            RepositorySnapshot.repository_id.in_(repo_ids), RepositorySnapshot.analysis_version == ANALYSIS_VERSION
    ).order_by(RepositorySnapshot.collected_at.desc(), RepositorySnapshot.id.desc())):
        snaps.setdefault(snap.repository_id, snap)  # newest first
    linked = {
        f.issue_id: f.structured_features
        for f in db.scalars(select(IssueFeature).where(
            IssueFeature.issue_id.in_({i.id for i, _ in pairs}), IssueFeature.feature_version == READINESS_FEATURE_VERSION))
    }
    health: dict[int, dict | None] = {}
    for rid, snap in snaps.items():
        report = intel_service.health_report(snap, settings, now)
        health[rid] = {"report": report.as_dict(), "collected_at": act.iso(snap.collected_at),
                       "stale": intel_service.snapshot_is_stale(snap, cfg, now),
                       "head_sha": (snap.data or {}).get("head_sha")}
    out: dict[int, dict] = {}
    for issue, repo in pairs:
        snap = snaps.get(repo.id)
        report = intel_service.readiness_report(issue, snap, linked.get(issue.id), settings, now)
        out[issue.id] = {
            "health": health.get(repo.id),
            "readiness": {"report": report.as_dict(),
                          "docs_collected_at": act.iso(snap.collected_at) if snap else None,
                          "linked_collected_at": (linked.get(issue.id) or {}).get("collected_at")},
        }
    return out


@dataclass
class _Candidates:
    pairs: list[tuple[Issue, Repository]]
    truncated: bool


def _load_candidates(db: Session, settings: Settings, req: MatchSearchRequest, repo_ids: list[int] | None, now: datetime) -> _Candidates:
    stmt = select(Issue, Repository).join(Repository, Issue.repository_id == Repository.id).where(Issue.state == "open")
    if repo_ids is not None:
        stmt = stmt.where(Repository.id.in_(repo_ids))
    if not req.include_archived:
        stmt = stmt.where(Repository.archived.is_(False))
    if req.language:
        stmt = stmt.where(func.lower(Repository.primary_language) == req.language.lower())
    stmt = stmt.order_by(Issue.updated_at.desc(), Issue.id).execution_options(yield_per=500)

    topic = req.topic.lower() if req.topic else None
    pairs: list[tuple[Issue, Repository]] = []
    truncated = False
    for issue, repo in db.execute(stmt):
        if topic and topic not in [t.lower() for t in (repo.topics or [])]:
            continue
        if req.max_inactive_days is not None:
            if repo.pushed_at is None or (now - repo_service._aware(repo.pushed_at)).days > req.max_inactive_days:
                continue
        if len(pairs) >= settings.reco_max_candidates:
            truncated = True
            break
        pairs.append((issue, repo))
    return _Candidates(pairs, truncated)


def _sync_requested(db, gh, settings, refs: list[str], analyze: bool = False
                    ) -> tuple[list[int], list[tuple[str, Exception]], list[tuple[str, Exception]]]:
    """Synchronise requested repositories. Failures are collected, not raised, so one bad repo doesn't hide the rest.

    With `analyze` (heuristic_v2) each repository's intelligence snapshot is also ensured; failures there are
    returned separately because they degrade scoring to "unknown" rather than blocking results."""
    errors: list[tuple[str, Exception]] = []
    intel_errors: list[tuple[str, Exception]] = []
    for ref in refs:
        owner, name = ref.split("/", 1)
        try:
            row = repo_service.ensure_repository_issues(db, gh, settings, owner, name)
        except (GitHubError, AppError) as exc:
            errors.append((ref, exc))
            continue
        if analyze:
            try:
                intel_service.ensure_repo_intelligence(db, gh, settings, row)
            except (GitHubError, AppError) as exc:
                intel_errors.append((f"{ref} (health analysis)", exc))
    ids = []
    for ref in refs:
        row = repo_service.find_repository(db, *ref.split("/", 1))
        if row is not None:
            ids.append(row.id)
    return ids, errors, intel_errors


def _error_info(exc: Exception) -> tuple[str, str]:
    app_err = exc if isinstance(exc, AppError) else to_app_error(exc)  # type: ignore[arg-type]
    return app_err.code, app_err.message


def _core_fields(repo: Repository, issue: Issue, res, row) -> dict:
    """Fields shared by every scored-issue response (search items and the single-issue match)."""
    return dict(
        match_id=row.id if row is not None else None,
        generated_at=row.created_at if row is not None else None,
        repository=RepoRefOut(owner=repo.owner, name=repo.name, full_name=f"{repo.owner}/{repo.name}"),
        issue=IssueRefOut(number=issue.issue_number, title=issue.title,
                          html_url=f"https://github.com/{repo.owner}/{repo.name}/issues/{issue.issue_number}"),
        final_score=res.final_score,
        coverage=res.coverage,
        insufficient_information=res.insufficient_information,
        components={
            n: ComponentOut(score=c.score, status=c.status, weight=c.weight, confidence=c.confidence,
                            effective_score=c.effective_score, evidence=list(c.evidence), warnings=list(c.warnings))
            for n, c in res.components.items()
        },
        reasons=list(res.reasons),
        missing_skills=list(res.missing_skills),
        warnings=list(res.warnings),
    )


def _model_out(scorer: Scorer) -> ModelOut:
    info = scorer.info
    return ModelOut(name=info.name, version=info.version, kind=info.kind, is_ml_prediction=info.is_ml_prediction,
                    description=info.description, score_note=SCORE_NOTE)


def _weights(scorer: Scorer, settings: Settings) -> dict[str, float]:
    weights_fn = getattr(scorer, "weights", None)
    return weights_fn() if callable(weights_fn) else getattr(scorer, "config", ScoringConfig.from_settings(settings)).weights()


def match_single_issue(db: Session, gh: GitHubClient, settings: Settings, scorer: Scorer, user: User,
                       owner: str, repo_name: str, number: int, *, persist: bool) -> IssueMatchOut:
    """Score one issue for the authenticated user. The issue is fetched (and a 404 mapped) only if not stored yet;
    health/readiness for heuristic_v2 come from stored data only (no extra GitHub calls)."""
    now = repo_service.utcnow()
    repo, issue = intel_service.find_issue_rows(db, gh, settings, owner, repo_name, number)
    features = load_user_features(db, user.id, None)
    intel = attach_intelligence(db, settings, [(issue, repo)], now).get(issue.id) if getattr(scorer, "uses_intelligence", False) else None
    ifeat = _issue_features(issue, repo, intel)
    res = scorer.score(features, ifeat, now)
    row = match_store.persist_matches(db, user.id, [(issue.id, features, ifeat, res)], scorer, now)[0] if persist else None

    reasons: list[str] = []
    personalizable = bool(features.skills or features.interests)
    if not features.has_profile:
        reasons.append("You have no profile; create one with skills and interests for personalised scoring.")
    elif not personalizable:
        reasons.append("Your profile has no skills or interests, so this score cannot be personalised.")
    if res.insufficient_information:
        reasons.append("This score rests on little evidence and is low-confidence.")
    status = "insufficient" if not personalizable else "limited" if reasons else "ok"
    return IssueMatchOut(model=_model_out(scorer), weights=_weights(scorer, settings),
                          ranking_reliability=ReliabilityOut(status=status, reasons=reasons),
                          match=MatchCore(**_core_fields(repo, issue, res, row)))


def search_matches(
    db: Session, gh: GitHubClient, settings: Settings, scorer: Scorer, user: User, req: MatchSearchRequest
) -> MatchSearchOut:
    now = repo_service.utcnow()
    features = load_user_features(db, user.id, req.preferred_difficulty)

    uses_intel = bool(getattr(scorer, "uses_intelligence", False))
    errors: list[tuple[str, Exception]] = []
    intel_errors: list[tuple[str, Exception]] = []
    repo_ids: list[int] | None = None
    if req.repositories:
        repo_ids, errors, intel_errors = _sync_requested(db, gh, settings, req.repositories, analyze=uses_intel)

    cands = _load_candidates(db, settings, req, repo_ids, now)
    if not cands.pairs and errors:
        raise errors[0][1]  # nothing to show and upstream failed: surface the real error (e.g. 429 + Retry-After)

    intel_map = attach_intelligence(db, settings, cands.pairs, now) if uses_intel else {}
    scored = []
    for issue, repo in cands.pairs:
        ifeat = _issue_features(issue, repo, intel_map.get(issue.id))
        scored.append((repo, issue, scorer.score(features, ifeat, now), ifeat))
    # Pure score order. Weak evidence is already pulled toward the conservative fill inside the score,
    # so no separate "insufficient goes last" rule is needed (it would rank sparse-but-perfect matches
    # below known mismatches). Ties break on repository then issue number for a stable order.
    scored.sort(key=lambda t: (-t[2].final_score, t[0].owner.lower(), t[0].name.lower(), t[1].issue_number))

    total = len(scored)
    page = scored[req.offset: req.offset + req.limit]

    rows = []
    if req.persist and page:  # only the returned page is stored, not every scored candidate
        rows = match_store.persist_matches(
            db, user.id, [(issue.id, features, ifeat, res) for _, issue, res, ifeat in page], scorer, now)
    items = [
        MatchItemOut(
            rank=req.offset + i + 1,
            **_core_fields(repo, issue, res, rows[i] if rows else None),
        )
        for i, (repo, issue, res, _) in enumerate(page)
    ]

    reasons: list[str] = []
    personalizable = bool(features.skills or features.interests)
    if not features.has_profile:
        reasons.append("You have no profile; create one with skills and interests for personalised ranking.")
    elif not personalizable:
        reasons.append("Your profile has no skills or interests, so results cannot be personalised.")
    if total == 0:
        reasons.append("No stored open issues match. Pass `repositories` to synchronise specific repositories, "
                       "or relax the filters.")
    if any(t[2].insufficient_information for t in scored):
        reasons.append("Some issues are flagged insufficient_information: their scores rest on little evidence and are low-confidence.")
    if cands.truncated:
        reasons.append(f"Only the {settings.reco_max_candidates} most recently updated matching issues were scored.")
    if errors:
        reasons.append("Some requested repositories could not be synchronised; stored data (possibly stale) was used.")
    if uses_intel and scored:
        no_health = len({r.id for r, _, _, f in scored if not (f.intel or {}).get("health")})
        if no_health:
            reasons.append(f"Repository health has not been analysed for {no_health} repositor{'y' if no_health == 1 else 'ies'}; "
                           "that component is scored as unknown. List them in `repositories` or call "
                           "GET /repositories/{owner}/{repo}/health.")
        no_linked = sum(1 for _, _, _, f in scored if not (f.intel or {}).get("readiness", {}).get("linked_collected_at"))
        if no_linked:
            reasons.append(f"Linked pull requests have not been checked for {no_linked} issue(s) "
                           "(use GET /issues/{owner}/{repo}/{number}/readiness).")
    if intel_errors:
        reasons.append("Repository health analysis could not be completed for some requested repositories.")
    status = ("no_candidates" if total == 0 else "insufficient" if not personalizable
              else "limited" if reasons else "ok")

    errors = errors + intel_errors
    return MatchSearchOut(
        model=_model_out(scorer),
        weights=_weights(scorer, settings),
        ranking_reliability=ReliabilityOut(status=status, reasons=reasons),
        total=total, limit=req.limit, offset=req.offset,
        candidates_considered=total, candidates_truncated=cands.truncated,
        items=items,
        upstream_errors=[UpstreamErrorOut(repository=ref, code=_error_info(e)[0], message=_error_info(e)[1])
                         for ref, e in errors],
    )
