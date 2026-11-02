"""Repository activity: summarise GitHub data into stored observations, then score them.

Two layers on purpose:
  * summarize_* functions turn raw GitHub lists into JSON-able *observations* (absolute timestamps and
    counts, collected at one moment). These are what gets stored.
  * compute_health() scores observations at read time against `now`, so ages stay current without
    re-fetching, and the same observations can be re-scored if the formula changes.

Windows: commits/issues/PRs/responses use `window_days` (default 90) ending at collection time.
Nothing is invented: an unavailable signal has status "unavailable" and is scored as unknown.
"""

import statistics
from datetime import datetime, timedelta, timezone

from app.intelligence.config import HEALTH_GROUPS, HEALTH_WEIGHTS, MAINTAINER_ASSOCIATIONS, IntelConfig
from app.intelligence.scoring import Report, Signal, combine, group_score, known, not_applicable, unknown
from app.services.github.models import CommitSummary, Issue, IssueComment, PullRequestSummary, Release

DAY = 86400.0


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return aware(dt).isoformat() if dt else None


def parse(value: str | None) -> datetime | None:
    return aware(datetime.fromisoformat(value)) if value else None


def _days(later: datetime, earlier: datetime) -> float:
    return (aware(later) - aware(earlier)).total_seconds() / DAY


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def unavailable(error: str) -> dict:
    return {"status": "unavailable", "error": error}


# ====================================================================== summarisers (raw -> observations)


def summarize_commits(commits: list[CommitSummary], truncated: bool, now: datetime, window_days: int) -> dict:
    start = now - timedelta(days=window_days)
    dated = [c.committed_at for c in commits if c.committed_at]
    in_window = [t for t in dated if aware(t) >= start]
    weeks = {int(_days(now, t) // 7) for t in in_window}
    oldest = min(dated, default=None)
    cut_inside_window = truncated and oldest is not None and aware(oldest) >= start
    history_complete = not truncated
    return {
        "status": "ok",
        "count_window": len(in_window),
        "window_count_is_lower_bound": cut_inside_window,
        "last_commit_at": iso(max(dated, default=None)),
        "active_weeks": len(weeks),
        "weeks_in_window": -(-window_days // 7),
        "distinct_authors": len({c.author for c in commits if c.author and c.committed_at and aware(c.committed_at) >= start}),
        "total_seen": len(commits),
        "history_complete": history_complete,
        # A whole-history view of fewer than 50 commits: a small project, where sparse commits are normal.
        "small_repository": history_complete and len(commits) < 50,
    }


def summarize_releases(releases: list[Release], now: datetime, window_days: int) -> dict:
    published = sorted((aware(r.published_at) for r in releases if not r.draft and r.published_at), reverse=True)
    gaps = [_days(a, b) for a, b in zip(published[:10], published[1:11])]
    return {
        "status": "ok",
        "count_seen": len(published),
        "count_window": sum(1 for t in published if t >= now - timedelta(days=window_days)),
        "last_release_at": iso(published[0]) if published else None,
        "median_interval_days": _median(gaps),
    }


def summarize_issues(issues: list[Issue], truncated: bool, now: datetime, window_days: int) -> dict:
    start = now - timedelta(days=window_days)
    only_issues = [i for i in issues if not i.is_pull_request]
    opened = [i for i in only_issues if aware(i.created_at) >= start]
    closed_in_window = [i for i in only_issues if i.closed_at and aware(i.closed_at) >= start]
    durations = [_days(i.closed_at, i.created_at) for i in opened if i.closed_at]  # type: ignore[arg-type]
    return {
        "status": "ok", "truncated": truncated,
        "opened_window": len(opened), "closed_window": len(closed_in_window),
        "open_seen": sum(1 for i in only_issues if i.state == "open"),
        # Only issues already closed are measured, so this is optimistic for slow projects.
        "median_days_to_close": _median(durations), "closed_sample": len(durations),
    }


def summarize_pulls(prs: list[PullRequestSummary], truncated: bool, now: datetime, window_days: int) -> dict:
    start = now - timedelta(days=window_days)
    merged = [p for p in prs if p.merged_at and aware(p.merged_at) >= start]
    rejected = [p for p in prs if p.closed_at and not p.merged_at and aware(p.closed_at) >= start]
    return {
        "status": "ok", "truncated": truncated,
        "opened_window": sum(1 for p in prs if aware(p.created_at) >= start),
        "merged_window": len(merged), "closed_unmerged_window": len(rejected),
        "open_seen": sum(1 for p in prs if p.state == "open"),
        "median_days_to_merge": _median([_days(p.merged_at, p.created_at) for p in merged]),  # type: ignore[arg-type]
        "merge_rate": round(len(merged) / (len(merged) + len(rejected)), 3) if merged or rejected else None,
    }


def _is_bot(login: str | None) -> bool:
    return bool(login) and login.endswith("[bot]")  # type: ignore[union-attr]


def _response_stats(items: list[tuple[int, str | None, str | None, datetime]], comments: list[IssueComment],
                    now: datetime, cfg: IntelConfig) -> dict:
    """items: (number, author, author_association, created_at) for items opened in the window."""
    by_number: dict[int, list[IssueComment]] = {}
    for c in comments:
        if c.issue_number is not None:
            by_number.setdefault(c.issue_number, []).append(c)
    hours: list[float] = []
    unanswered_old = pending = 0
    for number, author, assoc, created in items:
        if assoc in MAINTAINER_ASSOCIATIONS or _is_bot(author):
            continue  # maintainers' own items and bots are not "community" requests
        replies = [
            aware(c.created_at) for c in by_number.get(number, [])
            if aware(c.created_at) > aware(created) and c.author != author
            and c.author_association in MAINTAINER_ASSOCIATIONS and not _is_bot(c.author)
        ]
        if replies:
            hours.append((min(replies) - aware(created)).total_seconds() / 3600)
        elif _days(now, created) >= cfg.response_grace_days:
            unanswered_old += 1
        else:
            pending += 1
    eligible = len(hours) + unanswered_old
    return {
        "eligible": eligible, "responded": len(hours), "unanswered_old": unanswered_old, "pending": pending,
        "median_hours": _median(hours),
        "response_rate": round(len(hours) / eligible, 3) if eligible else None,
    }


def summarize_responses(issues: list[Issue], prs: list[PullRequestSummary] | None, comments: list[IssueComment],
                        comments_truncated: bool, now: datetime, cfg: IntelConfig) -> dict:
    start = now - timedelta(days=cfg.window_days)
    issue_items = [(i.number, i.author, i.author_association, i.created_at)
                   for i in issues if not i.is_pull_request and aware(i.created_at) >= start]
    out = {"status": "ok", "comments_truncated": comments_truncated,
           "issues": _response_stats(issue_items, comments, now, cfg)}
    if prs is not None:
        pr_items = [(p.number, p.author, p.author_association, p.created_at) for p in prs if aware(p.created_at) >= start]
        out["prs"] = {**_response_stats(pr_items, comments, now, cfg),
                      "note": "Based on PR conversation comments only; formal reviews are not counted, so this "
                              "understates review responsiveness."}
    return out


def summarize_maintenance(repo_archived: bool, license_spdx: str | None, pushed_at: datetime | None,
                          docs: dict | None) -> dict:
    complete = bool(docs) and docs.get("tree_status") == "ok"
    return {
        "status": "ok", "archived": repo_archived, "license_spdx": license_spdx, "pushed_at": iso(pushed_at),
        "tree_complete": complete,
        "has_ci": bool(docs["ci_files"]) if complete else None,
        "has_dependency_updates": bool(docs["dependency_bot"]) if complete else None,
    }


# ====================================================================== scoring (observations -> report)

_COMMIT_RECENCY = ((30, 100.0), (90, 75.0), (180, 45.0), (365, 20.0))
_RELEASE_RECENCY = ((90, 100.0), (180, 75.0), (365, 50.0), (730, 25.0))
_RESPONSE_SPEED_HOURS = ((24, 100.0), (72, 85.0), (168, 65.0), (720, 35.0))


def _band(value: float, bands: tuple, default: float) -> float:
    return next((score for limit, score in bands if value <= limit), default)


def _commit_signal(obs: dict, now: datetime) -> Signal:
    n, g, w = "commit_activity", HEALTH_GROUPS["commit_activity"], HEALTH_WEIGHTS["commit_activity"]
    if obs.get("status") != "ok":
        return unknown(n, g, w, "Commit history could not be retrieved.")
    last = parse(obs["last_commit_at"])
    if last is None:
        return unknown(n, g, w, "The repository has no readable commits.")
    since_last = _days(now, last)
    recency = _band(since_last, _COMMIT_RECENCY, 0.0)
    consistency = 100.0 if obs["window_count_is_lower_bound"] else min(1.0, obs["active_weeks"] / 4) * 100
    count = obs["count_window"]
    confidence = 0.7 if count == 0 else 0.4 if count < 3 else 0.6 if count < 10 else 0.8
    evidence = [f"Last commit {int(since_last)} days ago.",
                f"{count}{'+' if obs['window_count_is_lower_bound'] else ''} commits in the last window, across "
                f"{obs['active_weeks']} of {obs['weeks_in_window']} weeks, by {obs['distinct_authors']} author(s)."]
    warnings = []
    if count == 1:
        warnings.append("Only one commit in the window: a single recent commit does not establish steady maintenance.")
    if obs["small_repository"] and count < 3:
        warnings.append("Small repository with a short history: infrequent commits can be normal; weigh responsiveness too.")
    return known(n, g, w, 0.5 * recency + 0.5 * consistency, confidence, evidence, warnings)


def _release_signal(obs: dict, now: datetime) -> Signal:
    n, g, w = "release_cadence", HEALTH_GROUPS["release_cadence"], HEALTH_WEIGHTS["release_cadence"]
    if obs.get("status") != "ok":
        return unknown(n, g, w, "Releases could not be retrieved.")
    if obs["count_seen"] == 0:
        return not_applicable(n, g, w, "No published GitHub releases; many healthy projects do not use releases.")
    since = _days(now, parse(obs["last_release_at"]))  # type: ignore[arg-type]
    evidence = [f"Latest release {int(since)} days ago.", f"{obs['count_window']} release(s) in the window."]
    if obs["median_interval_days"] is not None:
        evidence.append(f"Median gap between recent releases: {obs['median_interval_days']:.0f} days.")
    confidence = 0.5 if obs["count_seen"] >= 3 else 0.35
    return known(n, g, w, _band(since, _RELEASE_RECENCY, 10.0), confidence, evidence)


def _flow_signal(issues: dict, pulls: dict) -> Signal:
    n, g, w = "issue_pr_flow", HEALTH_GROUPS["issue_pr_flow"], HEALTH_WEIGHTS["issue_pr_flow"]
    parts = [p for p in (issues, pulls) if p.get("status") == "ok"]
    if not parts:
        return unknown(n, g, w, "Issue and pull-request history could not be retrieved.")
    opened = sum(p["opened_window"] for p in parts)
    handled = sum(p.get("closed_window", 0) + p.get("merged_window", 0) + p.get("closed_unmerged_window", 0) for p in parts)
    partial = len(parts) < 2 or any(p.get("truncated") for p in parts)
    warnings = ["Only part of the issue/PR history was retrieved."] if partial else []
    if opened == 0 and handled == 0:
        return not_applicable(n, g, w, "No issue or pull-request traffic in the window, so throughput cannot be judged.")
    ratio = 1.0 if opened == 0 else min(1.0, handled / opened)
    confidence = (0.8 if opened >= 10 else 0.6 if opened >= 3 else 0.4) * (0.75 if partial else 1.0)
    evidence = [f"{opened} issue/PR opened and {handled} closed or merged in the window."]
    return known(n, g, w, ratio * 100, confidence, evidence, warnings)


def _responsiveness_signal(obs: dict, cfg: IntelConfig) -> Signal:
    n, g, w = "maintainer_responsiveness", HEALTH_GROUPS["maintainer_responsiveness"], HEALTH_WEIGHTS["maintainer_responsiveness"]
    if obs.get("status") != "ok":
        return unknown(n, g, w, "Comments could not be retrieved, so maintainer responsiveness is unknown.")
    s = obs["issues"]
    if s["eligible"] == 0:
        return not_applicable(n, g, w, "No community-opened issues old enough to judge in the window.")
    rate = s["response_rate"]
    speed = _band(s["median_hours"], _RESPONSE_SPEED_HOURS, 10.0) if s["median_hours"] is not None else 0.0
    confidence = 0.8 if s["eligible"] >= 8 else 0.6 if s["eligible"] >= 3 else 0.35
    warnings = []
    if obs["comments_truncated"]:
        confidence = max(0.2, confidence - 0.2)
        warnings.append("Comment history was cut short, so some maintainer replies may be missed and the rate understated.")
    median = "n/a" if s["median_hours"] is None else f"{s['median_hours']:.0f}h"
    evidence = [f"Maintainers replied to {s['responded']} of {s['eligible']} community issues (median first reply {median}).",
                f"{s['unanswered_old']} unanswered after {cfg.response_grace_days}+ days; {s['pending']} still within the grace period."]
    pr = obs.get("prs")
    if pr and pr["eligible"]:
        evidence.append(f"Pull requests: {pr['responded']} of {pr['eligible']} got a maintainer comment (reviews not counted).")
    return known(n, g, w, 0.5 * rate * 100 + 0.5 * speed, confidence, evidence, warnings)


def _maintenance_signal(obs: dict) -> Signal:
    n, g, w = "maintenance_indicators", HEALTH_GROUPS["maintenance_indicators"], HEALTH_WEIGHTS["maintenance_indicators"]
    if obs.get("status") != "ok":
        return unknown(n, g, w, "Repository metadata could not be retrieved.")
    if obs["archived"]:
        return known(n, g, w, 0.0, 1.0, ["Repository is archived (read-only)."])
    if not obs["tree_complete"]:
        return unknown(n, g, w, "File listing unavailable or truncated, so CI/dependency-update files are unknown.")
    has_license = bool(obs["license_spdx"]) and obs["license_spdx"] != "NOASSERTION"
    score = (35 if has_license else 0) + (35 if obs["has_ci"] else 0) + (30 if obs["has_dependency_updates"] else 0)
    evidence = [f"License: {obs['license_spdx'] or 'none detected'}.", f"CI configuration: {'found' if obs['has_ci'] else 'not found'}.",
                f"Dependency-update bot config: {'found' if obs['has_dependency_updates'] else 'not found'}."]
    return known(n, g, w, score, 0.5, evidence, ["These are weak hygiene signals; they do not measure code quality."])


def compute_health(obs: dict, now: datetime, cfg: IntelConfig) -> Report:
    """Repository health from stored observations. Heuristic, not an ML prediction."""
    maintenance = obs.get("maintenance", {"status": "unavailable"})
    signals = [
        _commit_signal(obs.get("commits", {}), now),
        _release_signal(obs.get("releases", {}), now),
        _flow_signal(obs.get("issues", {}), obs.get("pulls", {})),
        _responsiveness_signal(obs.get("responses", {}), cfg),
        _maintenance_signal(maintenance),
    ]
    score, coverage, by_name = combine(signals, cfg.unknown_fill)
    groups = {g: group_score(by_name, g, cfg.unknown_fill) for g in ("activity", "responsiveness", "maintenance")}
    warnings = list(dict.fromkeys(w for s in by_name.values() for w in s.warnings))
    strong = [s for s in by_name.values() if s.score is not None and s.confidence >= 0.5 and s.score >= 60]
    notes: list[str] = []

    if maintenance.get("archived"):
        return Report("repository_health", 0.0, "archived", 1.0, by_name, groups, tuple(warnings),
                      ("Archived repositories are read-only; no new contributions can be merged.",))
    if score is None or coverage < cfg.min_coverage:
        label = "insufficient_data"
    elif score >= 70 and len(strong) >= 3:
        label = "healthy"  # needs corroboration from several signals, never a single one
    elif score >= 45:
        label = "moderate"
    else:
        responsive = by_name["maintainer_responsiveness"].score
        commits_obs = obs.get("commits", {})
        recent = commits_obs.get("status") == "ok" and commits_obs["last_commit_at"] and \
            _days(now, parse(commits_obs["last_commit_at"])) <= 365  # type: ignore[arg-type]
        if responsive is not None and responsive >= 70 and recent:
            label = "stable_or_slow"
            notes.append("Few commits but maintainers respond well: this may be a small or mature project rather than a neglected one.")
        else:
            label = "low_activity" if score >= 25 else "inactive"
    if label == "insufficient_data":
        notes.append("Not enough retrievable evidence for a reliable health judgement.")
    return Report("repository_health", score, label, coverage, by_name, groups, tuple(warnings), tuple(notes))
