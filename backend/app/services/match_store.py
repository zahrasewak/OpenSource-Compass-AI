"""Persistence and retrieval of recommendation results.

Rows are append-only and immutable: nothing here ever updates a stored score. A different scoring
method/version, changed inputs, or changed outputs produce a *new* row, so history stays comparable
across `heuristic_v1` and any later (e.g. ML) scorer.
"""

import dataclasses
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import Issue, Recommendation, Repository
from app.recommend.types import IssueFeatures, Scorer, ScoredIssue, UserFeatures
from app.schemas.matches import (
    ComponentOut,
    IssueRefOut,
    RepoRefOut,
    StoredMatchOut,
    StoredMatchListOut,
    StoredMatchSummaryOut,
    StoredModelOut,
)

SCHEMA_VERSION = 1
SCORE_COLUMNS = {
    "skill_compatibility": "skill_score",
    "interest_similarity": "interest_score",
    "difficulty_suitability": "difficulty_score",
    "repository_activity": "activity_score",
    "repository_health": "activity_score",  # heuristic_v2 stores its repository-health score in this column
    "contribution_readiness": "readiness_score",
}
HISTORY_NOTE = (
    "Stored at generation time and never recomputed. The issue, repository and your profile may have "
    "changed since; `data_snapshot` holds the inputs that were scored."
)


def _iso(value: datetime) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def _jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, datetime):
        return _iso(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    return obj


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _component_json(result: ScoredIssue) -> dict:
    return {
        name: {
            "score": c.score, "status": c.status, "weight": c.weight, "confidence": c.confidence,
            "effective_score": c.effective_score, "evidence": list(c.evidence), "warnings": list(c.warnings),
        }
        for name, c in result.components.items()
    }


def build_record(
    user: UserFeatures, issue: IssueFeatures, result: ScoredIssue, scorer: Scorer, now: datetime
) -> dict:
    """Everything needed to store (and later audit) one result, plus its idempotency hash."""
    info = result.model
    describe = getattr(scorer, "describe_config", None)
    config = describe() if callable(describe) else {}
    user_snapshot, issue_snapshot = _jsonable(user), _jsonable(issue)
    if issue_snapshot.get("intel") is None:
        issue_snapshot.pop("intel", None)  # keeps heuristic_v1 snapshots/hashes identical to earlier versions
    components = {
        "schema_version": SCHEMA_VERSION,
        "model": {"id": info.model_id, "name": info.name, "version": info.version, "kind": info.kind,
                  "is_ml_prediction": info.is_ml_prediction},
        "components": _component_json(result),
        "reasons": list(result.reasons),
        "warnings": list(result.warnings),
        "missing_skills": list(result.missing_skills),
        "scoring_config": config,
    }
    # The hash covers inputs AND outputs but not wall-clock timestamps, so a retry of the same request
    # maps to the same row while any change in data, config or resulting scores creates a new one.
    digest = _hash({"model": info.model_id, "config": config, "user": user_snapshot, "issue": issue_snapshot,
                    "final": result.final_score, "coverage": result.coverage, "components": components["components"]})
    return {
        "components": components,
        "data_snapshot": {"scored_at": _iso(now), "user": user_snapshot, "issue": issue_snapshot},
        "input_hash": digest,
        "model_id": info.model_id,
        "final_score": result.final_score,
        "coverage": result.coverage,
        "insufficient_information": result.insufficient_information,
        "scores": {SCORE_COLUMNS[n]: c.score for n, c in result.components.items() if n in SCORE_COLUMNS},
    }


def persist_matches(
    db: Session, user_id: int, entries: list[tuple[int, UserFeatures, IssueFeatures, ScoredIssue]],
    scorer: Scorer, now: datetime,
) -> list[Recommendation]:
    """Store results in one transaction; identical results (same hash) reuse the existing row.

    Returns rows aligned with `entries`. A concurrent identical insert is resolved by re-reading.
    """
    if not entries:
        return []
    records = [(issue_id, build_record(u, i, r, scorer, now)) for issue_id, u, i, r in entries]
    for _ in range(2):
        model_ids = {rec["model_id"] for _, rec in records}
        existing = {
            (r.issue_id, r.ranking_model_version, r.input_hash): r
            for r in db.scalars(select(Recommendation).where(
                Recommendation.user_id == user_id,
                Recommendation.issue_id.in_({iid for iid, _ in records}),
                Recommendation.ranking_model_version.in_(model_ids),
                Recommendation.input_hash.in_({rec["input_hash"] for _, rec in records}),
            ))
        }
        rows = []
        for issue_id, rec in records:
            row = existing.get((issue_id, rec["model_id"], rec["input_hash"]))
            if row is None:
                row = Recommendation(
                    user_id=user_id, issue_id=issue_id, final_score=rec["final_score"], components=rec["components"],
                    ranking_model_version=rec["model_id"], coverage=rec["coverage"],
                    insufficient_information=rec["insufficient_information"], input_hash=rec["input_hash"],
                    data_snapshot=rec["data_snapshot"], created_at=now, **rec["scores"],
                )
                db.add(row)
            rows.append(row)
        try:
            db.commit()
            return rows
        except IntegrityError:
            db.rollback()
    raise AppError("persist_conflict", "Concurrent write conflict while storing matches; please retry", 503)


# ------------------------------------------------------------------ reading


def _component_scores(rec: Recommendation) -> dict[str, float | None]:
    stored = (rec.components or {}).get("components")
    if stored:  # the names used by the scoring method that produced this row (v1 and v2 differ)
        return {n: c.get("score") for n, c in stored.items()}
    return {n: getattr(rec, col) for n, col in SCORE_COLUMNS.items() if n != "repository_health"}


def _summary(rec: Recommendation, repo: Repository) -> StoredMatchSummaryOut:
    stored = rec.components or {}
    model = stored.get("model", {})
    snap_issue = (rec.data_snapshot or {}).get("issue", {})
    return StoredMatchSummaryOut(
        id=rec.id,
        generated_at=rec.created_at,
        model=StoredModelOut(id=rec.ranking_model_version, name=model.get("name"), version=model.get("version"),
                             kind=model.get("kind"), is_ml_prediction=model.get("is_ml_prediction")),
        repository=RepoRefOut(owner=repo.owner, name=repo.name, full_name=f"{repo.owner}/{repo.name}"),
        issue=IssueRefOut(number=snap_issue.get("number", 0), title=snap_issue.get("title", ""),
                          html_url=f"https://github.com/{repo.owner}/{repo.name}/issues/{snap_issue.get('number', 0)}"),
        final_score=rec.final_score,
        coverage=rec.coverage,
        insufficient_information=rec.insufficient_information,
        component_scores=_component_scores(rec),
    )


def _detail(rec: Recommendation, repo: Repository) -> StoredMatchOut:
    stored = rec.components or {}
    return StoredMatchOut(
        **_summary(rec, repo).model_dump(),
        components={n: ComponentOut(**c) for n, c in stored.get("components", {}).items()},
        reasons=stored.get("reasons", []),
        missing_skills=stored.get("missing_skills", []),
        warnings=stored.get("warnings", []),
        scoring_config=stored.get("scoring_config", {}),
        data_snapshot=rec.data_snapshot or {},
        note=HISTORY_NOTE,
    )


def get_match(db: Session, user_id: int, match_id: int) -> StoredMatchOut:
    """404 for both "no such match" and "someone else's match" so ids can't be probed."""
    row = db.execute(
        select(Recommendation, Repository)
        .join(Issue, Recommendation.issue_id == Issue.id)
        .join(Repository, Issue.repository_id == Repository.id)
        .where(Recommendation.id == match_id, Recommendation.user_id == user_id)
    ).first()
    if row is None:
        raise AppError("match_not_found", "Match not found", 404)
    return _detail(*row)


def list_matches(
    db: Session, user_id: int, *, limit: int, offset: int, model_id: str | None = None,
    repository: str | None = None, min_score: float | None = None, include_insufficient: bool = True,
) -> StoredMatchListOut:
    conds = [Recommendation.user_id == user_id]
    if model_id:
        conds.append(Recommendation.ranking_model_version == model_id)
    if repository:
        owner, _, name = repository.partition("/")
        conds += [func.lower(Repository.owner) == owner.lower(), func.lower(Repository.name) == name.lower()]
    if min_score is not None:
        conds.append(Recommendation.final_score >= min_score)
    if not include_insufficient:
        conds.append(Recommendation.insufficient_information.is_(False))
    base = (select(Recommendation, Repository)
            .join(Issue, Recommendation.issue_id == Issue.id)
            .join(Repository, Issue.repository_id == Repository.id).where(*conds))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(base.order_by(Recommendation.created_at.desc(), Recommendation.id.desc())
                      .limit(limit).offset(offset)).all()
    return StoredMatchListOut(items=[_summary(r, repo) for r, repo in rows], total=total, limit=limit, offset=offset)
