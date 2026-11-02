"""Heuristic scorer v2: v1's skill/interest/difficulty components + repository health + contribution readiness.

NOT a trained model and NOT shown to be better than v1: it has not been evaluated against any dataset.
It exists so the richer, measured signals (activity, maintainer responsiveness, contribution docs,
tests detection, linked PRs, description quality) can enter the same ranking pipeline and be compared
with v1 later on identical inputs. v1 code is untouched; v1 results stay valid under "heuristic_v1".

Weights (fixed in code and stored with every result; changing them requires a new version):
  skill 0.30 | interest 0.20 | difficulty 0.15 | repository health 0.20 | contribution readiness 0.15
vs v1:   0.35 |          0.25 |            0.20 |  activity 0.15         |  readiness 0.05
Rationale (a judgement, not a tuned result): health and readiness now rest on several measured signals
instead of one push date and four weak flags, while skill/interest are keyword matches and difficulty is
label-only, so weight moves from the latter group to the former. Nothing validates this split.

Missing data: every component carries a confidence; unknown or stale evidence is pulled toward the
conservative fill (see app.recommend.heuristic.combine), so absent health/readiness data can never raise a score.
"""

from dataclasses import asdict, replace
from datetime import datetime

from app.core.config import Settings
from app.intelligence.activity import _days, parse
from app.intelligence.config import IntelConfig
from app.recommend.components import difficulty_component, interest_component, skill_component
from app.recommend.heuristic import combine
from app.recommend.scoring_config import ScoringConfig
from app.recommend.types import ComponentScore, IssueFeatures, ModelInfo, ScoredIssue, UserFeatures

V2_WEIGHTS = {"w_skill": 0.30, "w_interest": 0.20, "w_difficulty": 0.15, "w_activity": 0.20, "w_readiness": 0.15}

V2_INFO = ModelInfo(
    name="heuristic_baseline",
    version="v2",
    kind="heuristic",
    is_ml_prediction=False,
    description=(
        "Hand-written weighted heuristic: v1 skill/interest/difficulty components plus repository health "
        "(commit/release activity, maintainer responsiveness, maintenance) and contribution readiness "
        "(contribution guide, detected tests, issue description quality, linked PRs, labels). Not a trained "
        "model; scores are not probabilities; not shown to outperform v1."
    ),
)


def v2_config(settings: Settings | None = None) -> ScoringConfig:
    base = ScoringConfig.from_settings(settings) if settings is not None else ScoringConfig()
    return replace(base, **V2_WEIGHTS)


def _unknown(name: str, weight: float, why: str, status: str = "unknown") -> ComponentScore:
    return ComponentScore(name, weight, None, 0.0, status, (), (why,))


class HeuristicScorerV2:
    info = V2_INFO
    uses_intelligence = True  # the matches service attaches stored intelligence reports to IssueFeatures.intel

    def __init__(self, config: ScoringConfig | None = None) -> None:
        self.config = config or v2_config()

    def weights(self) -> dict[str, float]:
        c = self.config
        return {"skill_compatibility": c.w_skill, "interest_similarity": c.w_interest,
                "difficulty_suitability": c.w_difficulty, "repository_health": c.w_activity,
                "contribution_readiness": c.w_readiness}

    def describe_config(self) -> dict:
        return {**asdict(self.config), "formula": "heuristic_v2", "component_weights": self.weights(),
                "intelligence": IntelConfig().describe()}

    # ---------------------------------------------------------------- components from stored reports

    def _health(self, intel: dict | None, now: datetime) -> ComponentScore:
        name, w = "repository_health", self.config.w_activity
        h = (intel or {}).get("health")
        if not h:
            return _unknown(name, w, "Repository health has not been analysed: include the repository in `repositories` "
                                     "or call GET /repositories/{owner}/{repo}/health first.")
        if h.get("stale"):
            return _unknown(name, w, "The stored repository analysis is too old to trust; it was treated as unknown.", "stale")
        r = h["report"]
        if r["label"] == "archived":
            return ComponentScore(name, w, 0.0, 1.0, "known", ("Repository is archived (read-only).",), ())
        if r["score"] is None:
            return _unknown(name, w, "No repository-health signal applies or could be retrieved.")
        age = int(_days(now, parse(h["collected_at"])))  # type: ignore[arg-type]
        lines = [f"Repository health {r['score']:.0f}/100 ({r['label'].replace('_', ' ')}); "
                 f"{r['coverage']:.0%} of the weighting is backed by retrieved data (collected {age} days ago)."]
        known = sorted((s for s in r["signals"].values() if s["status"] == "known" and s["evidence"]),
                       key=lambda s: -s["weight"])
        lines += [s["evidence"][0] for s in known[:3]]
        warnings = [*r["warnings"][:3], *r["notes"][:1]]
        return ComponentScore(name, w, r["score"], max(0.0, min(1.0, r["coverage"])), "known", tuple(lines), tuple(warnings))

    def _readiness(self, intel: dict | None) -> ComponentScore:
        name, w = "contribution_readiness", self.config.w_readiness
        rd = (intel or {}).get("readiness")
        if not rd:
            return _unknown(name, w, "Issue readiness has not been analysed.")
        r = rd["report"]
        if r["label"] == "not_available":
            return ComponentScore(name, w, 0.0, 1.0, "known", ("Issue is not open.",), ())
        if r["score"] is None:
            return _unknown(name, w, "No readiness signal could be computed.")
        lines = [f"Contribution readiness {r['score']:.0f}/100 ({r['label'].replace('_', ' ')}); "
                 f"{r['coverage']:.0%} of the weighting is backed by retrieved data."]
        known = sorted((s for s in r["signals"].values() if s["status"] == "known" and s["evidence"]),
                       key=lambda s: -s["weight"])
        lines += [s["evidence"][0] for s in known[:4]]
        unknown = [n.replace("_", " ") for n, s in r["signals"].items() if s["status"] in {"unknown", "stale"}]
        warnings = [*r["warnings"][:3], *([f"Not checked or out of date: {', '.join(unknown)}."] if unknown else []), *r["notes"][:1]]
        return ComponentScore(name, w, r["score"], max(0.0, min(1.0, r["coverage"])), "known", tuple(lines), tuple(warnings))

    # ---------------------------------------------------------------- scoring

    def score(self, user: UserFeatures, issue: IssueFeatures, now: datetime) -> ScoredIssue:
        cfg = self.config
        skill, missing = skill_component(user, issue, cfg)
        parts = {c.name: c for c in (skill, interest_component(user, issue, cfg), difficulty_component(user, issue, cfg),
                                     self._health(issue.intel, now), self._readiness(issue.intel))}
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
        return ScoredIssue(final_score=final, coverage=coverage, insufficient_information=insufficient,
                           components=components, reasons=tuple(reasons), warnings=tuple(warnings),
                           missing_skills=missing, model=self.info)
