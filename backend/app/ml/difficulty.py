"""Difficulty classifier inference.

This is a weakly supervised text classifier, NOT a measure of true difficulty and NOT part of the
heuristic scorers. `class_scores` are raw softmax-style outputs of a logistic regression that has not been
calibrated: they are not probabilities and must not be shown as such.

scikit-learn is imported lazily, only when an artifact is loaded, so the lightweight backend never needs it.
"""

import threading
import time
from dataclasses import dataclass, field

from app.ml.features import FEATURE_SCHEMA_VERSION, dense_features, scrub_text, text_for_model
from app.ml.registry import ModelUnavailable, load_artifact
from app.ml.schema import IssueInput

MIN_TEXT_CHARS = 15
FAILURE_RETRY_SECONDS = 30.0


def transform(objects: dict, texts: list[str], dicts: list[dict]):
    """Shared by training and inference so both build features identically."""
    from scipy.sparse import hstack

    text_part = objects["tfidf"].transform(texts)
    dense_part = objects["scaler"].transform(objects["dictvec"].transform(dicts))
    return hstack([text_part, dense_part]).tocsr()


@dataclass(frozen=True)
class DifficultyPrediction:
    status: str  # "predicted" | "unsupported_input"
    model_id: str
    label_scheme: str
    label: str | None = None
    class_scores: dict[str, float] = field(default_factory=dict)
    top_score: float | None = None
    margin: float | None = None  # top score minus runner-up: small = the model could not separate the classes
    calibrated: bool = False
    label_source: str = "weak_labels"
    evaluation_status: str = "not_evaluated"
    reason: str | None = None
    note: str = ("Estimates which difficulty class maintainers' labels most resemble for issues like this one. "
                 "It was trained on provisional, label-derived targets; it is not verified difficulty and the "
                 "scores are uncalibrated, not probabilities.")


class DifficultyPredictor:
    def __init__(self, objects: dict, metadata: dict) -> None:
        self._objects, self.metadata = objects, metadata
        self.model_id: str = metadata["model_id"]
        self.label_scheme: str = metadata["label_scheme"]

    def predict(self, issue: IssueInput) -> DifficultyPrediction:
        base = dict(model_id=self.model_id, label_scheme=self.label_scheme,
                    label_source=self.metadata.get("label_source", "weak_labels"),
                    evaluation_status=self.metadata.get("evaluation_status", "not_evaluated"))
        if len(scrub_text(issue.title)) + len(scrub_text(issue.body)) < MIN_TEXT_CHARS:
            return DifficultyPrediction(status="unsupported_input", reason="not_enough_text", **base)
        X = transform(self._objects, [text_for_model(issue)], [dense_features(issue)])
        scores = self._objects["clf"].predict_proba(X)[0]
        classes = [str(c) for c in self._objects["classes"]]
        ranked = sorted(zip(classes, scores), key=lambda cs: (-cs[1], cs[0]))
        margin = float(ranked[0][1] - ranked[1][1]) if len(ranked) > 1 else float(ranked[0][1])
        return DifficultyPrediction(
            status="predicted", label=ranked[0][0], class_scores={c: round(float(s), 4) for c, s in zip(classes, scores)},
            top_score=round(float(ranked[0][1]), 4), margin=round(margin, 4), **base)


class DifficultyModelService:
    """Loads the configured artifact lazily and remembers failures briefly, so a broken or missing model costs one
    failed load per interval instead of one per request. Thread-safe."""

    def __init__(self, model_dir: str | None, allow_synthetic: bool = False, clock=time.monotonic,
                 allow_exploratory: bool = False) -> None:
        self._dir, self._allow_synth, self._clock = model_dir, allow_synthetic, clock
        self._allow_explore = allow_exploratory
        self._predictor: DifficultyPredictor | None = None
        self._failure: tuple[ModelUnavailable, float] | None = None
        self._lock = threading.Lock()

    def predictor(self) -> DifficultyPredictor:
        """The loaded predictor, or ModelUnavailable (with a stable `reason`)."""
        with self._lock:
            if self._predictor is not None:
                return self._predictor
            if not self._dir:
                raise ModelUnavailable("not_configured", "COMPASS_ML_DIFFICULTY_MODEL_DIR is not set")
            if self._failure and self._clock() - self._failure[1] < FAILURE_RETRY_SECONDS:
                raise self._failure[0]
            try:
                objects, meta = load_artifact(self._dir, allow_synthetic=self._allow_synth, allow_exploratory=self._allow_explore)
                self._predictor = DifficultyPredictor(objects, meta)
                self._failure = None
                return self._predictor
            except ModelUnavailable as exc:
                self._failure = (exc, self._clock())
                raise

    def feature_schema_version(self) -> str:
        return FEATURE_SCHEMA_VERSION
