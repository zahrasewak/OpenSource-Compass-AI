"""Tunable parameters of the heuristic baseline. All values are untuned defaults, not optimised."""

from dataclasses import dataclass

from app.core.config import Settings


@dataclass(frozen=True)
class ScoringConfig:
    w_skill: float = 0.35
    w_interest: float = 0.25
    w_difficulty: float = 0.20
    w_activity: float = 0.15
    w_readiness: float = 0.05
    unknown_fill: float = 25.0  # score used in place of missing/low-confidence evidence
    min_coverage: float = 0.4  # evidence-backed weight share below which an item is "insufficient"
    max_data_age_days: int = 7  # stored data older than this is stale
    skill_have_threshold: float = 0.3  # proficiency below this counts the skill as "missing"

    # Confidence assigned to each component when it has *some* evidence (0..1). Lexical/label-only
    # evidence is deliberately capped well below 1.
    conf_interest: float = 0.6
    conf_difficulty: float = 0.4  # label hint only; no trained difficulty model exists yet
    conf_activity: float = 0.7  # last-push date only; commit rate / maintainer response not analysed
    conf_readiness: float = 0.5  # linked PRs, contribution guide and tests are not checked yet
    conf_skill_cap: float = 0.85

    @classmethod
    def from_settings(cls, s: Settings) -> "ScoringConfig":
        return cls(
            w_skill=s.weight_skill, w_interest=s.weight_interest, w_difficulty=s.weight_difficulty,
            w_activity=s.weight_activity, w_readiness=s.weight_readiness,
            unknown_fill=s.reco_unknown_fill, min_coverage=s.reco_min_coverage,
            max_data_age_days=s.reco_max_data_age_days,
        )

    def weights(self) -> dict[str, float]:
        return {
            "skill_compatibility": self.w_skill,
            "interest_similarity": self.w_interest,
            "difficulty_suitability": self.w_difficulty,
            "repository_activity": self.w_activity,
            "contribution_readiness": self.w_readiness,
        }
