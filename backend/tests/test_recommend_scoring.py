"""Pure scoring tests: no database, no HTTP. These check the formula and missing-data rules,
NOT that recommendations are good; that needs labelled outcome data (see docs/RECOMMENDATIONS.md)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import Settings
from app.recommend import HeuristicScorer, IssueFeatures, ScoringConfig, UserFeatures, combine
from app.recommend.components import (
    activity_component,
    difficulty_component,
    interest_component,
    readiness_component,
    required_skills,
    skill_component,
)
from app.recommend.types import ComponentScore
from app.recommend.vocabulary import canonical_skill, detect_skills, difficulty_hint

NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)
CFG = ScoringConfig()


def days_ago(n):
    return NOW - timedelta(days=n)


def issue(**kw) -> IssueFeatures:
    base = dict(
        owner="o", repo="r", number=1, title="Fix bug", body=None, labels=(), state="open", assignees=(),
        issue_fetched_at=days_ago(0), repo_language=None, repo_topics=(), repo_description=None,
        repo_archived=False, repo_pushed_at=days_ago(5), repo_fetched_at=days_ago(0),
    )
    base.update(kw)
    return IssueFeatures(**base)


def user(**kw) -> UserFeatures:
    base = dict(has_profile=True, skills={}, interests=(), target_difficulty=None)
    base.update(kw)
    return UserFeatures(**base)


def comp(name, weight, score, conf=1.0):
    return ComponentScore(name, weight, score, conf if score is not None else 0.0, "known" if score is not None else "unknown")


def five(scores, conf=1.0):
    names = list(CFG.weights())
    return {n: comp(n, CFG.weights()[n], s, conf) for n, s in zip(names, scores)}


# ------------------------------------------------------------------ formula


def test_default_weights_match_spec_and_sum_to_one():
    w = CFG.weights()
    assert list(w.values()) == [0.35, 0.25, 0.20, 0.15, 0.05]
    assert sum(w.values()) == pytest.approx(1.0)


def test_weighted_sum_exact_when_all_evidence_is_certain():
    final, coverage, out = combine(five([80, 60, 40, 100, 20]), CFG)
    assert final == pytest.approx(0.35 * 80 + 0.25 * 60 + 0.20 * 40 + 0.15 * 100 + 0.05 * 20)  # 67.0
    assert coverage == pytest.approx(1.0)
    assert out["skill_compatibility"].effective_score == 80


def test_all_perfect_is_100_and_all_zero_known_is_0():
    assert combine(five([100] * 5), CFG)[0] == 100.0
    assert combine(five([0] * 5), CFG)[0] == 0.0


def test_unknown_uses_conservative_fill_not_perfect_and_not_zero():
    final, coverage, out = combine(five([None, 60, 40, 100, 20]), CFG)
    expected = 0.35 * 25 + 0.25 * 60 + 0.20 * 40 + 0.15 * 100 + 0.05 * 20
    assert final == pytest.approx(expected)
    assert coverage == pytest.approx(0.65)
    assert out["skill_compatibility"].effective_score == 25


def test_unknown_ranks_between_genuinely_low_and_genuinely_good():
    low = combine(five([0, 50, 50, 50, 50]), CFG)[0]
    unknown = combine(five([None, 50, 50, 50, 50]), CFG)[0]
    good = combine(five([80, 50, 50, 50, 50]), CFG)[0]
    assert low < unknown < good


def test_unknown_can_never_outscore_the_same_issue_with_that_component_good():
    for s in (30, 60, 100):
        assert combine(five([None, 50, 50, 50, 50]), CFG)[0] < combine(five([s, 50, 50, 50, 50]), CFG)[0]


def test_low_confidence_pulls_score_toward_fill():
    final, _, out = combine(five([100, None, None, None, None], conf=0.5), CFG)
    assert out["skill_compatibility"].effective_score == pytest.approx(62.5)  # 0.5*100 + 0.5*25
    assert final == pytest.approx(0.35 * 62.5 + 0.65 * 25)


def test_all_unknown_equals_fill_and_zero_coverage():
    final, coverage, _ = combine(five([None] * 5), CFG)
    assert final == pytest.approx(25.0) and coverage == 0.0


def test_settings_reject_weights_not_summing_to_one():
    with pytest.raises(ValueError):
        Settings(weight_skill=0.5, _env_file=None)
    assert ScoringConfig.from_settings(Settings(_env_file=None)).weights() == CFG.weights()


# ------------------------------------------------------------------ vocabulary


def test_vocabulary_detection_and_aliases():
    assert detect_skills("Crash when using Node.js with TypeScript and Postgres") == {"node", "typescript", "postgresql"}
    assert "go" not in detect_skills("let's go ahead and fix it")  # ambiguous word not detected
    assert "java" not in detect_skills("JavaScript only")  # whole-word matching
    assert "c++" in detect_skills("segfault in C++ code") and "c#" in detect_skills("a C# bug")
    assert canonical_skill("Node.js") == "node" and canonical_skill("Golang") == "go" and canonical_skill("Foo") == "foo"


def test_difficulty_hint_uses_highest_level_and_normalises():
    assert difficulty_hint(("good first issue",)) == (1, "good first issue")
    assert difficulty_hint(("difficulty: easy",)) == (2, "difficulty: easy")
    assert difficulty_hint(("Good_First_Issue", "hard"))[0] == 4  # conservative: higher wins
    assert difficulty_hint(("help wanted", "bug")) is None  # not a difficulty signal


# ------------------------------------------------------------------ skills


def test_skill_score_weighted_by_language_and_reports_missing():
    iss = issue(repo_language="Python", body="Add a FastAPI endpoint")
    assert required_skills(iss) == {"python": (2.0, "repository language"), "fastapi": (1.0, "issue text")}
    c, missing = skill_component(user(skills={"python": 0.8}), iss, CFG)
    assert c.score == pytest.approx(100 * (2 * 0.8) / 3, abs=0.01)
    assert missing == ("fastapi",)
    assert c.status == "known" and c.confidence == pytest.approx(0.8)
    assert any("python" in e and "0.80" in e for e in c.evidence)


def test_no_skills_means_unknown_not_perfect():
    c, missing = skill_component(user(skills={}), issue(repo_language="Python"), CFG)
    assert c.score is None and c.status == "unknown" and c.confidence == 0 and missing == ()


def test_no_detectable_requirements_is_unknown():
    c, _ = skill_component(user(skills={"python": 1.0}), issue(), CFG)
    assert c.score is None


def test_lacking_every_required_skill_is_a_genuine_zero_not_unknown():
    c, missing = skill_component(user(skills={"cobol": 1.0}), issue(repo_language="Rust"), CFG)
    assert c.score == 0.0 and c.status == "known" and missing == ("rust",)


def test_user_skill_aliases_are_canonicalised_by_loader_contract():
    c, _ = skill_component(user(skills={canonical_skill("Node.js"): 1.0}), issue(body="node.js server crash"), CFG)
    assert c.score == 100.0


def test_go_language_counts_as_requirement():
    assert "go" in required_skills(issue(repo_language="Go"))


# ------------------------------------------------------------------ interests


def test_interest_match_nomatch_and_unknown():
    iss = issue(title="Improve machine learning docs", repo_topics=("web-apis",))
    hit = interest_component(user(interests=("machine learning",)), iss, CFG)
    assert hit.score == 100.0 and hit.status == "known" and hit.evidence
    miss = interest_component(user(interests=("compilers",)), iss, CFG)
    assert miss.score == 0.0 and miss.status == "known"  # known low, lexical caveat in warnings
    assert any("lexical" in w for w in miss.warnings)
    none = interest_component(user(interests=()), iss, CFG)
    assert none.score is None and none.confidence == 0


def test_interest_plural_folding_and_partial_credit():
    iss = issue(title="Add APIs for testing")
    c = interest_component(user(interests=("api", "testing", "rust", "go")), iss, CFG)
    assert c.score == pytest.approx(100 * 2 / 3, abs=0.01)  # 2 of min(4,3)=3 -> capped denominator


# ------------------------------------------------------------------ difficulty


def test_difficulty_distance_scoring():
    iss = issue(labels=("good first issue",))
    assert difficulty_component(user(target_difficulty=1, target_difficulty_source="profile"), iss, CFG).score == 100
    assert difficulty_component(user(target_difficulty=2, target_difficulty_source="profile"), iss, CFG).score == 70
    assert difficulty_component(user(target_difficulty=5, target_difficulty_source="profile"), iss, CFG).score == 0


def test_unknown_difficulty_is_never_an_ideal_match():
    no_label = difficulty_component(user(target_difficulty=3), issue(labels=("bug",)), CFG)
    assert no_label.score is None and no_label.confidence == 0 and "unknown" in no_label.warnings[0].lower()
    no_target = difficulty_component(user(), issue(labels=("easy",)), CFG)
    assert no_target.score is None


def test_difficulty_confidence_is_low_and_labelled_noisy():
    c = difficulty_component(user(target_difficulty=2, target_difficulty_source="profile"), issue(labels=("easy",)), CFG)
    assert c.confidence == CFG.conf_difficulty < 0.5
    assert "noisy" in c.evidence[0] and "no trained difficulty model" in c.warnings[0]


# ------------------------------------------------------------------ activity


@pytest.mark.parametrize("days,expected", [(0, 100), (14, 100), (20, 85), (60, 65), (150, 40), (300, 20), (500, 5)])
def test_activity_bands(days, expected):
    assert activity_component(issue(repo_pushed_at=days_ago(days)), NOW, CFG).score == expected


def test_activity_missing_stale_and_archived():
    assert activity_component(issue(repo_pushed_at=None), NOW, CFG).score is None
    stale = activity_component(issue(repo_fetched_at=days_ago(30)), NOW, CFG)
    assert stale.score is None and stale.status == "stale" and "stale" in stale.warnings[0]
    arch = activity_component(issue(repo_archived=True), NOW, CFG)
    assert arch.score == 0.0 and arch.status == "known"


def test_activity_handles_naive_datetimes():
    naive = issue(repo_pushed_at=datetime(2025, 5, 30), repo_fetched_at=datetime(2025, 6, 1))
    assert activity_component(naive, NOW, CFG).score == 100


# ------------------------------------------------------------------ readiness


def test_readiness_best_case_is_still_low_confidence():
    c = readiness_component(issue(body="x" * 100), NOW, CFG)
    assert c.score == 100.0 and c.confidence == CFG.conf_readiness == 0.5
    assert any("have not been checked" in w for w in c.warnings)
    _, _, out = combine({"contribution_readiness": ComponentScore(**{**c.__dict__, "weight": 1.0})}, CFG)
    assert out["contribution_readiness"].effective_score == pytest.approx(62.5)  # never a perfect 100


def test_readiness_penalises_assignment_missing_body_and_closed():
    assigned = readiness_component(issue(body="x" * 100, assignees=("bob",)), NOW, CFG)
    assert assigned.score == 70.0 and any("assigned to bob" in w for w in assigned.warnings)
    nobody = readiness_component(issue(), NOW, CFG)
    assert nobody.score == 85.0 and any("little or no description" in w for w in nobody.warnings)
    assert readiness_component(issue(state="closed"), NOW, CFG).score == 0.0


def test_readiness_stale_issue_is_unknown():
    c = readiness_component(issue(issue_fetched_at=days_ago(20)), NOW, CFG)
    assert c.score is None and c.status == "stale"


# ------------------------------------------------------------------ whole scorer


def test_scorer_declares_it_is_a_heuristic_not_ml():
    info = HeuristicScorer().info
    assert (info.name, info.kind, info.is_ml_prediction) == ("heuristic_baseline", "heuristic", False)
    assert "probabilit" in info.description


def test_no_profile_means_insufficient_and_no_skill_credit():
    res = HeuristicScorer().score(UserFeatures(has_profile=False), issue(repo_language="Python"), NOW)
    assert res.components["skill_compatibility"].score is None
    assert res.components["skill_compatibility"].effective_score == 25
    assert res.insufficient_information and res.coverage < CFG.min_coverage
    assert res.warnings[0].startswith("Insufficient information")


def test_full_profile_is_sufficient_and_final_matches_formula():
    u = user(skills={"python": 0.9}, interests=("machine learning",), target_difficulty=2, target_difficulty_source="profile")
    iss = issue(repo_language="Python", title="Machine learning loader bug", labels=("good first issue",), body="y" * 120)
    res = HeuristicScorer().score(u, iss, NOW)
    assert not res.insufficient_information
    recomputed = sum(c.weight * c.effective_score for c in res.components.values())
    assert res.final_score == pytest.approx(recomputed, abs=1e-3)
    assert res.reasons and all("fabricated" not in r for r in res.reasons)
    assert 0 <= res.final_score <= 100


def test_scoring_is_deterministic():
    u = user(skills={"python": 0.5}, interests=("ml",))
    iss = issue(repo_language="Python")
    a = HeuristicScorer().score(u, iss, NOW)
    b = HeuristicScorer().score(u, iss, NOW)
    assert a == b


def test_stronger_skill_match_scores_higher_all_else_equal():
    iss = issue(repo_language="Python", body="y" * 100)
    strong = HeuristicScorer().score(user(skills={"python": 0.9}), iss, NOW)
    weak = HeuristicScorer().score(user(skills={"python": 0.2}), iss, NOW)
    none = HeuristicScorer().score(user(skills={"rust": 0.9}), iss, NOW)
    assert strong.final_score > weak.final_score > none.final_score
    assert none.missing_skills == ("python",)
