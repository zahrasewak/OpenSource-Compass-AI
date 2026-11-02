"""Recommendation scoring. `Scorer` is the interface; `HeuristicScorer` is the current baseline."""

from app.recommend.heuristic import HeuristicScorer, combine
from app.recommend.scoring_config import ScoringConfig
from app.recommend.types import IssueFeatures, ModelInfo, ScoredIssue, Scorer, UserFeatures

__all__ = ["HeuristicScorer", "ScoringConfig", "Scorer", "UserFeatures", "IssueFeatures", "ScoredIssue", "ModelInfo", "combine"]
