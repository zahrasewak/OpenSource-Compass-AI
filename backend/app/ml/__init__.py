"""ML inference package. Imports here must never pull in scikit-learn (see difficulty.transform / registry)."""

from app.ml.difficulty import DifficultyModelService, DifficultyPrediction, DifficultyPredictor
from app.ml.registry import ModelUnavailable
from app.ml.schema import IssueInput, IssueRecord

__all__ = ["DifficultyModelService", "DifficultyPrediction", "DifficultyPredictor", "ModelUnavailable",
           "IssueInput", "IssueRecord"]
