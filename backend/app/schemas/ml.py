"""Response models for the difficulty estimate endpoint."""

from datetime import datetime

from pydantic import BaseModel


class IssueSummaryOut(BaseModel):
    owner: str
    repo: str
    number: int
    title: str
    html_url: str


class LabelHintOut(BaseModel):
    level: int  # 1-5
    maintainer_label: str
    source: str = "maintainer_label"
    note: str = "A noisy human signal read from the issue's labels, not a measured or predicted difficulty."


class DeterministicSignals(BaseModel):
    difficulty_label_hint: LabelHintOut | None


class ModelRef(BaseModel):
    id: str
    kind: str = "ml"
    label_scheme: str
    label_source: str  # weak_labels | mixed | human
    evaluation_status: str  # evaluated_on_weak_labels | evaluated_on_mixed_labels | not_evaluated | ...


class MlPrediction(BaseModel):
    status: str  # predicted | unsupported_input | unavailable
    reason: str | None = None  # why not predicted (stable code)
    model: ModelRef | None = None
    label: str | None = None
    class_scores: dict[str, float] = {}
    top_score: float | None = None
    margin: float | None = None
    calibrated: bool = False
    prediction_id: int | None = None
    generated_at: datetime | None = None
    note: str = ("Machine-learning estimate trained on provisional label-derived targets. Scores are uncalibrated "
                 "and are NOT probabilities. Independent of, and not combined with, the deterministic signals.")


class Fallback(BaseModel):
    used: bool
    description: str


class DifficultyEstimateOut(BaseModel):
    issue: IssueSummaryOut
    deterministic_signals: DeterministicSignals
    ml_prediction: MlPrediction
    fallback: Fallback
