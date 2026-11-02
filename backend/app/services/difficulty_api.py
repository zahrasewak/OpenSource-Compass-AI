"""Difficulty estimate: deterministic label hint + optional ML prediction, kept strictly separate."""

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Issue, IssuePrediction, Repository
from app.ml import DifficultyModelService, IssueInput, ModelUnavailable
from app.ml.features import dense_features, text_for_model
from app.ml.schema import FIVE_CLASS
from app.recommend.vocabulary import difficulty_hint
from app.schemas.ml import (
    DeterministicSignals, DifficultyEstimateOut, Fallback, IssueSummaryOut, LabelHintOut, MlPrediction, ModelRef,
)
from app.services import intelligence as intel
from app.services.github import GitHubClient

MODEL_STATUS = "ml_weak_labels"  # recorded with every stored prediction; never "calibrated"/"verified"
_LEVEL_BY_NAME = {v: k for k, v in FIVE_CLASS.items()}


def _store(db: Session, issue: Issue, model_id: str, pred, issue_input: IssueInput) -> IssuePrediction:
    """Idempotent: an identical input scored by the same model version reuses the stored row."""
    digest = hashlib.sha256(json.dumps([text_for_model(issue_input), dense_features(issue_input)],
                                       sort_keys=True).encode()).hexdigest()
    for row in db.scalars(select(IssuePrediction).where(IssuePrediction.issue_id == issue.id,
                                                        IssuePrediction.model_version == model_id)):
        if (row.explanation or {}).get("input_hash") == digest:
            return row
    row = IssuePrediction(
        issue_id=issue.id, model_version=model_id, model_status=MODEL_STATUS, predicted_category=pred.label,
        predicted_difficulty=_LEVEL_BY_NAME.get(pred.label) if pred.label_scheme == "five_class" else None,
        class_probabilities=pred.class_scores,  # column name predates this stage: these are UNCALIBRATED scores
        confidence=None,  # deliberately empty: no calibrated confidence exists
        explanation={"calibrated": False, "label_source": pred.label_source, "evaluation_status": pred.evaluation_status,
                     "label_scheme": pred.label_scheme, "top_score": pred.top_score, "margin": pred.margin,
                     "input_hash": digest, "note": pred.note},
    )
    db.add(row)
    db.commit()
    return row


def estimate(db: Session, gh: GitHubClient, settings: Settings, service: DifficultyModelService,
             owner: str, repo_name: str, number: int) -> DifficultyEstimateOut:
    repo, issue = intel.find_issue_rows(db, gh, settings, owner, repo_name, number)
    labels = [l.get("name", "") for l in (issue.labels or []) if isinstance(l, dict)]
    hint = difficulty_hint(tuple(labels))
    signals = DeterministicSignals(
        difficulty_label_hint=LabelHintOut(level=hint[0], maintainer_label=hint[1]) if hint else None)

    issue_input = IssueInput(title=issue.title, body=issue.body, author_association=issue.author_association,
                             repo_language=repo.primary_language)
    try:
        pred = service.predictor().predict(issue_input)
    except ModelUnavailable as exc:
        ml = MlPrediction(status="unavailable", reason=exc.reason)
    else:
        model = ModelRef(id=pred.model_id, label_scheme=pred.label_scheme, label_source=pred.label_source,
                         evaluation_status=pred.evaluation_status)
        if pred.status != "predicted":
            ml = MlPrediction(status=pred.status, reason=pred.reason, model=model)
        else:
            row = _store(db, issue, pred.model_id, pred, issue_input)
            ml = MlPrediction(status="predicted", model=model, label=pred.label, class_scores=pred.class_scores,
                              top_score=pred.top_score, margin=pred.margin, prediction_id=row.id,
                              generated_at=row.created_at)

    used = ml.status != "predicted"
    if not used:
        desc = "ML prediction available; the maintainer-label hint is shown separately and not combined with it."
    elif hint:
        desc = "No ML prediction; the maintainer-label hint (a noisy human signal) is the only difficulty indication."
    else:
        desc = "No ML prediction and no difficulty label: difficulty is unknown. No value is invented."
    return DifficultyEstimateOut(
        issue=IssueSummaryOut(owner=repo.owner, repo=repo.name, number=issue.issue_number, title=issue.title,
                              html_url=f"https://github.com/{repo.owner}/{repo.name}/issues/{issue.issue_number}"),
        deterministic_signals=signals, ml_prediction=ml, fallback=Fallback(used=used, description=desc))
