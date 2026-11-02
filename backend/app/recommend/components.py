"""The five heuristic components. Pure functions: no database, HTTP or clock access.

Each returns a ComponentScore whose `score` is 0-100, or None when the needed information is
unavailable. "Genuinely low" (a real 0-20) and "unknown" (None) are always kept distinct.
"""

import re
from datetime import datetime, timezone

from app.recommend.scoring_config import ScoringConfig
from app.recommend.types import ComponentScore, IssueFeatures, UserFeatures
from app.recommend.vocabulary import canonical_skill, detect_skills, difficulty_hint

_TOKEN = re.compile(r"[a-z0-9+#.]+")
_MAX_TEXT = 5000  # characters of issue body examined


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_days(ts: datetime, now: datetime) -> float:
    return (_aware(now) - _aware(ts)).total_seconds() / 86400


def _unknown(name: str, weight: float, why: str, status: str = "unknown") -> ComponentScore:
    return ComponentScore(name, weight, None, 0.0, status, (), (why,))


def issue_text(issue: IssueFeatures) -> str:
    parts = [issue.title, (issue.body or "")[:_MAX_TEXT], " ".join(issue.labels),
             " ".join(issue.repo_topics), issue.repo_description or "", issue.repo_language or ""]
    return "\n".join(parts)


# ----------------------------------------------------------------------------- skills


def required_skills(issue: IssueFeatures) -> dict[str, tuple[float, str]]:
    """Technologies the issue appears to involve: {skill: (weight, source)}.

    Keyword detection over issue title/body/labels + repository language/topics. The repository's
    primary language counts double because almost any change to the repository involves it.
    """
    found: dict[str, tuple[float, str]] = {}
    text = "\n".join([issue.title, (issue.body or "")[:_MAX_TEXT], " ".join(issue.labels)])
    for skill in detect_skills(text):
        found[skill] = (1.0, "issue text")
    for topic in issue.repo_topics:
        for skill in detect_skills(topic):
            found.setdefault(skill, (1.0, "repository topic"))
    if issue.repo_language:
        found[canonical_skill(issue.repo_language)] = (2.0, "repository language")
    return found


def skill_component(user: UserFeatures, issue: IssueFeatures, cfg: ScoringConfig) -> tuple[ComponentScore, tuple[str, ...]]:
    name, w = "skill_compatibility", cfg.w_skill
    if not user.skills:
        return _unknown(name, w, "No skills in your profile, so skill compatibility cannot be assessed."), ()
    required = required_skills(issue)
    if not required:
        return _unknown(name, w, "No technologies could be detected for this issue, so required skills are unknown."), ()

    total = sum(weight for weight, _ in required.values())
    credit = sum(weight * user.skills.get(skill, 0.0) for skill, (weight, _) in required.items())
    score = 100.0 * credit / total
    evidence, missing = [], []
    for skill, (weight, source) in sorted(required.items()):
        prof = user.skills.get(skill)
        if prof is not None and prof >= cfg.skill_have_threshold:
            evidence.append(f"Involves {skill} ({source}); your self-reported proficiency is {prof:.2f}.")
        else:
            missing.append(skill)
    confidence = min(cfg.conf_skill_cap, 0.4 + 0.2 * len(required))
    warnings = ["Required skills come from keyword detection and may be incomplete."]
    return ComponentScore(name, w, round(score, 2), confidence, "known", tuple(evidence), tuple(warnings)), tuple(missing)


# ----------------------------------------------------------------------------- interests


def _tokens(text: str) -> set[str]:
    out = set()
    for tok in _TOKEN.findall(text.lower()):
        tok = tok.strip(".")
        if tok:
            out.add(tok)
            if len(tok) > 3 and tok.endswith("s"):
                out.add(tok[:-1])  # crude plural folding
    return out


def interest_component(user: UserFeatures, issue: IssueFeatures, cfg: ScoringConfig) -> ComponentScore:
    name, w = "interest_similarity", cfg.w_interest
    if not user.interests:
        return _unknown(name, w, "No interests in your profile, so interest similarity cannot be assessed.")
    tokens = _tokens(issue_text(issue))
    matched = []
    for interest in user.interests:
        base = [t.rstrip("s") if len(t) > 3 and t.endswith("s") else t for t in _TOKEN.findall(interest.lower())]
        if base and all(t in tokens for t in base):
            matched.append(interest)
    denominator = min(len(user.interests), 3)
    score = 100.0 * min(1.0, len(matched) / denominator)
    evidence = tuple(f"Issue or repository text mentions your interest '{i}'." for i in matched)
    warnings = ("Interest matching is lexical (shared words), not semantic; a 0 may only mean different wording.",)
    return ComponentScore(name, w, round(score, 2), cfg.conf_interest, "known", evidence, warnings)


# ----------------------------------------------------------------------------- difficulty


def difficulty_component(user: UserFeatures, issue: IssueFeatures, cfg: ScoringConfig) -> ComponentScore:
    name, w = "difficulty_suitability", cfg.w_difficulty
    hint = difficulty_hint(issue.labels)
    if hint is None:
        return _unknown(name, w, "Issue difficulty is unknown: no difficulty label and no trained difficulty model yet.")
    if user.target_difficulty is None:
        return _unknown(name, w, "Your preferred difficulty is unknown (set preferred_difficulty or experience_level).")
    level, label = hint
    distance = abs(level - user.target_difficulty)
    score = max(0.0, 100.0 - 30.0 * distance)
    evidence = (
        f"Label '{label}' suggests difficulty ~{level}/5 (a noisy human label, not measured); "
        f"your target is {user.target_difficulty}/5 (from {user.target_difficulty_source}).",
    )
    warnings = ("Difficulty is inferred from labels only; no trained difficulty model has been applied.",)
    return ComponentScore(name, w, score, cfg.conf_difficulty, "known", evidence, warnings)


# ----------------------------------------------------------------------------- activity

_ACTIVITY_BANDS = ((14, 100.0), (30, 85.0), (90, 65.0), (180, 40.0), (365, 20.0))


def activity_component(issue: IssueFeatures, now: datetime, cfg: ScoringConfig) -> ComponentScore:
    name, w = "repository_activity", cfg.w_activity
    if issue.repo_fetched_at is not None and _age_days(issue.repo_fetched_at, now) > cfg.max_data_age_days:
        age = int(_age_days(issue.repo_fetched_at, now))
        return _unknown(name, w, f"Repository data is {age} days old (stale), so activity is treated as unknown.", "stale")
    if issue.repo_archived:
        return ComponentScore(name, w, 0.0, 1.0, "known", ("Repository is archived (read-only).",), ())
    if issue.repo_pushed_at is None:
        return _unknown(name, w, "No last-push date is recorded for this repository, so activity is unknown.")
    days = max(0.0, _age_days(issue.repo_pushed_at, now))
    score = next((s for limit, s in _ACTIVITY_BANDS if days <= limit), 5.0)
    evidence = (f"Repository last pushed {int(days)} days ago.",)
    warnings = ("Activity reflects the last push date only; commit frequency and maintainer responsiveness are not analysed yet.",)
    return ComponentScore(name, w, score, cfg.conf_activity, "known", evidence, warnings)


# ----------------------------------------------------------------------------- readiness


def readiness_component(issue: IssueFeatures, now: datetime, cfg: ScoringConfig) -> ComponentScore:
    name, w = "contribution_readiness", cfg.w_readiness
    age = _age_days(issue.issue_fetched_at, now)
    if age > cfg.max_data_age_days:
        return _unknown(name, w, f"Issue data is {int(age)} days old (stale); it may have closed or been assigned.", "stale")
    if issue.state != "open":
        return ComponentScore(name, w, 0.0, 1.0, "known", ("Issue is not open.",), ())

    score, evidence, warnings = 40.0, ["Issue is open."], []
    if issue.assignees:
        warnings.append(f"Issue is already assigned to {', '.join(issue.assignees)}.")
    else:
        score += 30.0
        evidence.append("Issue has no assignee (as of the last sync; someone may be working on it anyway).")
    if issue.body and len(issue.body.strip()) >= 80:
        score += 15.0
        evidence.append("Issue has a substantive description.")
    else:
        warnings.append("Issue has little or no description.")
    if issue.repo_archived:
        warnings.append("Repository is archived.")
    else:
        score += 15.0
    warnings.append("Linked pull requests, contribution guidelines and test availability have not been checked.")
    return ComponentScore(name, w, score, cfg.conf_readiness, "known", tuple(evidence), tuple(warnings))
