"""Heuristic baseline scorer: weighted sum of five components with explicit missing-data handling.

THIS IS NOT A TRAINED MODEL. Scores are on an arbitrary 0-100 scale; they are not probabilities
and have not been validated against real contribution outcomes.

Missing-data rule (see docs/RECOMMENDATIONS.md):
  effective_i = confidence_i * score_i + (1 - confidence_i) * unknown_fill      (score known)
  effective_i = unknown_fill                                                      (score unknown)
  final       = sum_i weight_i * effective_i
So absent or weak evidence is pulled toward a conservative fill (default 25) rather than being
treated as perfect, and rather than being dropped (which would inflate the remaining components).
With confidence 1 for every component this is exactly sum_i weight_i * score_i.
"""

from dataclasses import asdict, replace
from datetime import datetime

from app.recommend.components import (
    activity_component,
    difficulty_component,
    interest_component,
    readiness_component,
    skill_component,
)
from app.recommend.scoring_config import ScoringConfig
from app.recommend.types import ComponentScore, IssueFeatures, ModelInfo, ScoredIssue, UserFeatures

HEURISTIC_INFO = ModelInfo(
    name="heuristic_baseline",
    version="v1",  # bump whenever scoring behaviour changes; stored results are never rescored
    kind="heuristic",
    is_ml_prediction=False,
    description=(
        "Hand-written weighted heuristic over skills, interests, label-derived difficulty, last-push "
        "recency and basic readiness signals. Not a trained model; scores are not probabilities."
    ),
)


def combine(components: dict[str, ComponentScore], cfg: ScoringConfig) -> tuple[float, float, dict[str, ComponentScore]]:
    """Return (final_score, coverage, components with effective_score filled in)."""
    final = coverage = 0.0
    out: dict[str, ComponentScore] = {}
    for name, c in components.items():
        if c.score is None:
            effective, confidence = cfg.unknown_fill, 0.0
        else:
            confidence = max(0.0, min(1.0, c.confidence))
            effective = confidence * c.score + (1 - confidence) * cfg.unknown_fill
        final += c.weight * effective
        coverage += c.weight * confidence
        out[name] = replace(c, effective_score=round(effective, 4), confidence=confidence)
    return round(final, 4), round(coverage, 4), out


class HeuristicScorer:
    info = HEURISTIC_INFO

    def __init__(self, config: ScoringConfig | None = None) -> None:
        self.config = config or ScoringConfig()

    def describe_config(self) -> dict:
        """Parameters stored with each persisted result so it can be reproduced and audited."""
        return asdict(self.config)

    def score(self, user: UserFeatures, issue: IssueFeatures, now: datetime) -> ScoredIssue:
        cfg = self.config
        skill, missing = skill_component(user, issue, cfg)
        parts = {
            c.name: c
            for c in (
                skill,
                interest_component(user, issue, cfg),
                difficulty_component(user, issue, cfg),
                activity_component(issue, now, cfg),
                readiness_component(issue, now, cfg),
            )
        }
        final, coverage, components = combine(parts, cfg)

        reasons = [e for c in components.values() if c.score is not None and c.score >= 50 for e in c.evidence]
        warnings: list[str] = []
        for c in components.values():
            for w in c.warnings:
                if w not in warnings:
                    warnings.append(w)
        insufficient = coverage < cfg.min_coverage
        if insufficient:
            warnings.insert(0, "Insufficient information to rank this issue reliably for you; treat its score as low-confidence.")
        return ScoredIssue(
            final_score=final,
            coverage=coverage,
            insufficient_information=insufficient,
            components=components,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            missing_skills=missing,
            model=self.info,
        )
