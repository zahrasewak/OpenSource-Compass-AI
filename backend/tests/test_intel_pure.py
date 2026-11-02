"""Pure tests of the repository-intelligence analyzers (no database, no HTTP).
They verify the documented calculations and missing-data rules, not that the scores predict anything."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.intelligence import IntelConfig, compute_health, compute_readiness
from app.intelligence import activity as act
from app.intelligence import readiness as rd
from app.intelligence.scoring import combine, known, not_applicable, unknown
from app.services.github.models import (
    CommitSummary, GitTree, FileText, Issue, IssueComment, PullRequestSummary, Release, TimelineReference,
)

NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)
CFG = IntelConfig()


def at(days=0, hours=0):
    return (NOW - timedelta(days=days, hours=hours)).isoformat()


def commit(days, author="dev"):
    return CommitSummary.model_validate({"sha": "a" * 40, "author": {"login": author},
                                         "commit": {"committer": {"date": at(days)}}})


def release(days, draft=False):
    return Release.model_validate({"tag_name": f"v{days}", "published_at": at(days), "draft": draft})


def gh_issue(number, created_days, closed_days=None, assoc="NONE", author="user1", pr=False):
    d = {"number": number, "title": f"issue {number}", "state": "closed" if closed_days is not None else "open",
         "html_url": "https://github.com/o/r/issues/1", "user": {"login": author}, "author_association": assoc,
         "created_at": at(created_days), "updated_at": at(0), "closed_at": at(closed_days) if closed_days is not None else None}
    if pr:
        d["pull_request"] = {}
    return Issue.model_validate(d)


def gh_pr(number, created_days, merged_days=None, closed_days=None, assoc="NONE", author="contrib"):
    state = "open" if merged_days is None and closed_days is None else "closed"
    return PullRequestSummary.model_validate({
        "number": number, "title": "pr", "state": state, "user": {"login": author}, "author_association": assoc,
        "created_at": at(created_days), "closed_at": at(merged_days if merged_days is not None else closed_days) if state == "closed" else None,
        "merged_at": at(merged_days) if merged_days is not None else None})


def comment(number, days=0, hours=0, author="maint", assoc="MEMBER", cid=[0]):
    cid[0] += 1
    return IssueComment.model_validate({"id": cid[0], "user": {"login": author}, "author_association": assoc, "body": "x",
                                        "created_at": at(days, hours), "issue_url": f"https://api.github.com/repos/o/r/issues/{number}"})


def tree(paths, truncated=False):
    return GitTree.model_validate({"sha": "t" * 40, "truncated": truncated,
                                   "tree": [{"path": p, "type": "blob"} for p in paths]})


def docs_for(paths, truncated=False, readme_text=None, contributing_text=None):
    d = rd.analyze_tree(tree(paths, truncated))
    files = {"README": FileText(path="README.md", text=readme_text) if readme_text is not None else None,
             "CONTRIBUTING": FileText(path="CONTRIBUTING.md", text=contributing_text) if contributing_text is not None else None}
    return {**d, **rd.analyze_instructions(files)}


# ============================================================ summarisers


def test_commit_summary_windows_and_weeks():
    commits = [commit(1), commit(2), commit(10), commit(95), commit(200)]
    s = act.summarize_commits(commits, truncated=False, now=NOW, window_days=90)
    assert s["count_window"] == 3 and s["active_weeks"] == 2 and s["weeks_in_window"] == 13
    assert s["last_commit_at"] == at(1) and s["history_complete"] and s["small_repository"]
    assert s["window_count_is_lower_bound"] is False


def test_commit_summary_truncated_inside_window_is_a_lower_bound():
    s = act.summarize_commits([commit(d) for d in range(1, 31)], truncated=True, now=NOW, window_days=90)
    assert s["window_count_is_lower_bound"] is True and not s["history_complete"] and not s["small_repository"]


def test_commit_exactly_outside_window_not_counted():
    s = act.summarize_commits([commit(91)], False, NOW, 90)
    assert s["count_window"] == 0 and s["active_weeks"] == 0


def test_release_cadence_calculation():
    s = act.summarize_releases([release(10), release(40), release(70), release(100), release(5, draft=True)], NOW, 90)
    assert s["count_seen"] == 4 and s["count_window"] == 3  # draft ignored
    assert s["last_release_at"] == at(10) and s["median_interval_days"] == 30.0
    assert act.summarize_releases([], NOW, 90) == {"status": "ok", "count_seen": 0, "count_window": 0,
                                                    "last_release_at": None, "median_interval_days": None}


def test_issue_and_pull_summaries():
    issues = [gh_issue(1, 10, closed_days=5), gh_issue(2, 20), gh_issue(3, 200, closed_days=30), gh_issue(4, 3, pr=True)]
    s = act.summarize_issues(issues, False, NOW, 90)
    assert (s["opened_window"], s["closed_window"], s["open_seen"]) == (2, 2, 1)  # PR entry ignored
    assert s["median_days_to_close"] == 5.0 and s["closed_sample"] == 1
    prs = [gh_pr(1, 30, merged_days=20), gh_pr(2, 30, merged_days=10), gh_pr(3, 40, closed_days=5), gh_pr(4, 2)]
    p = act.summarize_pulls(prs, False, NOW, 90)
    assert (p["opened_window"], p["merged_window"], p["closed_unmerged_window"], p["open_seen"]) == (4, 2, 1, 1)
    assert p["median_days_to_merge"] == 15.0 and p["merge_rate"] == pytest.approx(0.667, abs=0.001)


def test_maintainer_response_time_calculation():
    issues = [
        gh_issue(1, 20), gh_issue(2, 20), gh_issue(3, 20), gh_issue(4, 2),   # community: 2 replied, 1 ignored, 1 pending
        gh_issue(5, 20, assoc="MEMBER", author="maint"),                       # maintainer-opened: skipped
        gh_issue(6, 20),                                                      # only a bot and the author replied
    ]
    comments = [
        comment(1, days=20 - 0, hours=-5) if False else IssueComment.model_validate({
            "id": 1, "user": {"login": "maint"}, "author_association": "MEMBER", "body": "", "created_at": at(20, -5),
            "issue_url": "https://api.github.com/repos/o/r/issues/1"}),
        IssueComment.model_validate({"id": 2, "user": {"login": "owner"}, "author_association": "OWNER", "body": "",
                                     "created_at": at(20, -50), "issue_url": "https://api.github.com/repos/o/r/issues/2"}),
        comment(6, days=19, author="ci[bot]", assoc="NONE"), comment(6, days=19, author="user1", assoc="NONE"),
        comment(3, days=21),  # a comment BEFORE the issue was created must not count
    ]
    r = act.summarize_responses(issues, None, comments, False, NOW, CFG)["issues"]
    assert r["responded"] == 2 and r["unanswered_old"] == 2 and r["pending"] == 1 and r["eligible"] == 4
    assert r["median_hours"] == 27.5 and r["response_rate"] == 0.5


def test_pr_response_notes_that_reviews_are_not_counted():
    out = act.summarize_responses([], [gh_pr(7, 10)], [comment(7, days=9)], False, NOW, CFG)
    assert out["prs"]["responded"] == 1 and "reviews are not counted" in out["prs"]["note"]


# ============================================================ health


def full_observations(**over):
    commits = [commit(1 + 3 * i, f"dev{i % 3}") for i in range(30)]
    issues = [gh_issue(n, 20 + n, closed_days=(10 + n) if n <= 8 else None) for n in range(1, 11)]
    prs = [gh_pr(100 + n, 30, merged_days=20) for n in range(4)] + [gh_pr(110, 5)]
    comments = [IssueComment.model_validate({
        "id": n, "user": {"login": "maint"}, "author_association": "MEMBER", "body": "",
        "created_at": at(20 + n, -10), "issue_url": f"https://api.github.com/repos/o/r/issues/{n}"}) for n in range(1, 11)]
    docs = docs_for([".github/workflows/ci.yml", ".github/dependabot.yml", "LICENSE", "README.md"])
    obs = {
        "commits": act.summarize_commits(commits, False, NOW, 90),
        "releases": act.summarize_releases([release(15), release(60)], NOW, 90),
        "issues": act.summarize_issues(issues, False, NOW, 90),
        "pulls": act.summarize_pulls(prs, False, NOW, 90),
        "responses": act.summarize_responses(issues, prs, comments, False, NOW, CFG),
        "maintenance": act.summarize_maintenance(False, "MIT", NOW - timedelta(days=1), docs),
    }
    obs.update(over)
    return obs


def test_active_repository_is_healthy_and_corroborated():
    rep = compute_health(full_observations(), NOW, CFG)
    assert rep.label == "healthy" and rep.score >= 70 and rep.coverage >= 0.6
    assert rep.signals["maintainer_responsiveness"].score == 100.0
    assert rep.signals["issue_pr_flow"].score == pytest.approx(80.0)
    assert set(rep.group_scores) == {"activity", "responsiveness", "maintenance"}


def test_report_is_json_serialisable_and_labelled_heuristic():
    d = compute_health(full_observations(), NOW, CFG).as_dict()
    json.dumps(d)
    assert d["kind"] == "repository_health" and d["signals"]["commit_activity"]["status"] == "known"


def test_inactive_repository_low_commit_signal_is_a_known_zero_not_unknown():
    obs = {"commits": act.summarize_commits([commit(800), commit(900), commit(1000)], False, NOW, 90),
           "releases": act.summarize_releases([], NOW, 90), "issues": act.summarize_issues([], False, NOW, 90),
           "pulls": act.summarize_pulls([], False, NOW, 90),
           "responses": act.summarize_responses([], [], [], False, NOW, CFG),
           "maintenance": act.summarize_maintenance(False, "MIT", NOW - timedelta(days=800), docs_for(["LICENSE"]))}
    rep = compute_health(obs, NOW, CFG)
    c = rep.signals["commit_activity"]
    assert c.status == "known" and c.score == 0.0
    assert rep.signals["release_cadence"].status == "not_applicable"
    assert rep.signals["issue_pr_flow"].status == "not_applicable"
    assert rep.signals["maintainer_responsiveness"].status == "not_applicable"
    assert rep.label == "inactive"


def test_single_recent_commit_is_not_proof_of_health():
    obs = full_observations(commits=act.summarize_commits([commit(1)], False, NOW, 90))
    c = compute_health(obs, NOW, CFG).signals["commit_activity"]
    assert c.score == 62.5 and c.confidence == 0.4  # recency 100, consistency 25 (1 of >=4 active weeks)
    assert any("single recent commit" in w for w in c.warnings)
    # and on its own it cannot produce a "healthy" verdict
    alone = {"commits": obs["commits"]}
    only = compute_health(alone, NOW, CFG)
    assert only.label == "insufficient_data" and only.label != "healthy"


def test_small_repository_with_sparse_commits_is_not_called_inactive():
    commits = [commit(40), commit(70)] + [commit(150 + 10 * i) for i in range(10)]
    issues = [gh_issue(n, 30 + n, closed_days=20 + n) for n in range(1, 5)]
    comments = [IssueComment.model_validate({
        "id": n, "user": {"login": "maint"}, "author_association": "OWNER", "body": "",
        "created_at": at(30 + n, -3), "issue_url": f"https://api.github.com/repos/o/r/issues/{n}"}) for n in range(1, 5)]
    obs = {"commits": act.summarize_commits(commits, False, NOW, 90),
           "releases": act.summarize_releases([], NOW, 90), "issues": act.summarize_issues(issues, False, NOW, 90),
           "pulls": act.summarize_pulls([], False, NOW, 90),
           "responses": act.summarize_responses(issues, [], comments, False, NOW, CFG),
           "maintenance": {"status": "unavailable"}}
    rep = compute_health(obs, NOW, CFG)
    assert obs["commits"]["small_repository"] is True
    assert rep.label == "moderate" and rep.score == pytest.approx(52.5)
    assert any("Small repository" in w for w in rep.signals["commit_activity"].warnings)


def test_missing_data_is_unknown_not_low_and_never_healthy():
    obs = {k: act.unavailable("github_unavailable") for k in ("commits", "releases", "issues", "pulls", "responses", "maintenance")}
    rep = compute_health(obs, NOW, CFG)
    assert rep.label == "insufficient_data" and rep.coverage == 0.0
    assert all(s.status == "unknown" and s.score is None and s.effective_score == 25.0 for s in rep.signals.values())


def test_one_unavailable_signal_is_pulled_to_fill_rather_than_zero_or_dropped():
    obs = full_observations(commits=act.unavailable("github_timeout"))
    rep = compute_health(obs, NOW, CFG)
    c = rep.signals["commit_activity"]
    assert c.status == "unknown" and c.score is None and c.effective_score == CFG.unknown_fill
    assert rep.score < compute_health(full_observations(), NOW, CFG).score  # missing data did not help


def test_releases_not_applicable_when_none_but_old_release_scores_low():
    rep = compute_health(full_observations(releases=act.summarize_releases([], NOW, 90)), NOW, CFG)
    r = rep.signals["release_cadence"]
    assert r.status == "not_applicable" and r.effective_score is None
    old = compute_health(full_observations(releases=act.summarize_releases([release(800)], NOW, 90)), NOW, CFG)
    assert old.signals["release_cadence"].score == 10.0 and old.signals["release_cadence"].status == "known"


def test_response_signal_formula():
    s = act.summarize_responses(
        [gh_issue(1, 20), gh_issue(2, 20), gh_issue(3, 20)], None,
        [IssueComment.model_validate({"id": 1, "user": {"login": "m"}, "author_association": "MEMBER", "body": "",
                                      "created_at": at(20, -5), "issue_url": "https://x/issues/1"}),
         IssueComment.model_validate({"id": 2, "user": {"login": "m"}, "author_association": "MEMBER", "body": "",
                                      "created_at": at(20, -50), "issue_url": "https://x/issues/2"})],
        False, NOW, CFG)
    sig = act._responsiveness_signal(s, CFG)  # rate 2/3, median 27.5h -> speed band 85
    assert sig.score == pytest.approx(0.5 * 0.667 * 100 + 0.5 * 85, abs=0.01)  # rate is stored rounded to 3 dp
    assert sig.confidence == 0.6  # 3 eligible items


def test_truncated_comments_lower_confidence_and_warn():
    s = act.summarize_responses([gh_issue(n, 20) for n in range(1, 10)], None, [], True, NOW, CFG)
    sig = act._responsiveness_signal(s, CFG)
    assert sig.score == 0.0 and sig.confidence == pytest.approx(0.6) and any("cut short" in w for w in sig.warnings)


def test_no_community_issues_means_responsiveness_not_applicable():
    s = act.summarize_responses([gh_issue(1, 5, assoc="OWNER", author="o")], None, [], False, NOW, CFG)
    assert act._responsiveness_signal(s, CFG).status == "not_applicable"


def test_archived_repository_forces_zero():
    obs = full_observations(maintenance=act.summarize_maintenance(True, "MIT", NOW, None))
    rep = compute_health(obs, NOW, CFG)
    assert rep.label == "archived" and rep.score == 0.0


def test_maintenance_unknown_without_complete_tree():
    m = act.summarize_maintenance(False, "MIT", NOW, docs_for(["README.md"], truncated=True))
    assert act._maintenance_signal(m).status == "unknown"


def test_flow_and_combine_rules():
    flow = act._flow_signal({"status": "ok", "opened_window": 10, "closed_window": 3, "truncated": False},
                            {"status": "ok", "opened_window": 0, "merged_window": 2, "closed_unmerged_window": 0, "truncated": False})
    assert flow.score == pytest.approx(50.0)  # 5 handled / 10 opened
    na = act._flow_signal({"status": "ok", "opened_window": 0, "closed_window": 0, "truncated": False},
                          {"status": "ok", "opened_window": 0, "merged_window": 0, "closed_unmerged_window": 0, "truncated": False})
    assert na.status == "not_applicable"
    score, coverage, out = combine([known("a", "g", 0.5, 80, 1.0), not_applicable("b", "g", 0.3, "n/a"), unknown("c", "g", 0.2, "?")], 25)
    assert score == pytest.approx((0.5 * 80 + 0.2 * 25) / 0.7, abs=0.01) and coverage == pytest.approx(0.5 / 0.7, abs=0.001)


def test_health_is_deterministic():
    a = compute_health(full_observations(), NOW, CFG).as_dict()
    b = compute_health(full_observations(), NOW, CFG).as_dict()
    assert a == b


# ============================================================ repository documentation detection


def test_tree_detection_of_contribution_docs_and_tests():
    d = rd.analyze_tree(tree(["README.md", "CONTRIBUTING.md", "tests/test_a.py", "src/app.py", "pytest.ini",
                              "node_modules/x/test/y.js", ".github/PULL_REQUEST_TEMPLATE.md",
                              ".github/ISSUE_TEMPLATE/bug.yml", "CODE_OF_CONDUCT.md", "SECURITY.md", "LICENSE"]))
    assert d["contributing"] == {"path": "CONTRIBUTING.md", "kind": "guide"} and d["readme"] == "README.md"
    assert d["test_files"]["count"] == 1 and d["test_files"]["frameworks_detected"] == ["pytest"]  # vendored ignored
    assert d["pr_template"] and d["issue_templates"] and d["code_of_conduct"] and d["security_policy"] and d["license_file"]


@pytest.mark.parametrize("path,expected", [
    ("tests/foo.py", True), ("src/test_utils.py", True), ("pkg/foo_test.go", True), ("web/app.test.ts", True),
    ("web/__tests__/a.js", True), ("src/main/FooTest.java", True), ("spec/models/a_spec.rb", True),
    ("src/contest.py", False), ("docs/testing-guide.md", False), ("vendor/x/tests/a.py", False),
    ("third_party/lib/a.test.js", False),
])
def test_test_path_detection(path, expected):
    assert rd.is_test_path(path) is expected


def test_contributing_variants():
    assert rd.analyze_tree(tree([".github/CONTRIBUTING.md"]))["contributing"]["kind"] == "guide"
    assert rd.analyze_tree(tree(["docs/development.md"]))["contributing"]["kind"] == "equivalent"
    assert rd.analyze_tree(tree(["README.md"]))["contributing"] is None


def test_truncated_tree_is_flagged():
    assert rd.analyze_tree(tree(["a.py"], truncated=True))["tree_status"] == "truncated"


def test_documented_test_commands_detected_but_never_executed():
    f = {"README": FileText(path="README.md", text="Use `pytest -q` or npm test."),
         "CONTRIBUTING": None}
    out = rd.analyze_instructions(f)
    assert {c["command"].lower() for c in out["test_commands_documented"]} == {"pytest", "npm test"}
    assert out["files_read"] == ["README"]
    assert rd.analyze_instructions({"README": None, "CONTRIBUTING": None})["instructions_status"] == "unavailable"
    assert rd.analyze_instructions({"README": FileText(path="README.md", text="## Contributing\nhi")})["readme_has_contributing_section"]


# ============================================================ readiness signals


def issue_dict(**kw):
    d = {"title": "Crash on save", "body": "x" * 10, "labels": [], "state": "open", "assignees": [], "fetched_at": at(0)}
    d.update(kw)
    return d


def test_contribution_guide_signal_cases():
    assert rd._guide_signal(docs_for(["CONTRIBUTING.md"])).score == 100
    absent = rd._guide_signal(docs_for(["README.md"]))
    assert absent.status == "known" and absent.score == 15  # confirmed absence is a genuine low
    assert rd._guide_signal(docs_for(["README.md"], truncated=True)).status == "unknown"  # absence not provable
    assert rd._guide_signal(docs_for(["README.md"], readme_text="# Contributing\nsteps")).score == 55
    assert rd._guide_signal(None).status == "unknown"


def test_tests_signal_distinguishes_detection_from_execution():
    both = rd._tests_signal(docs_for(["tests/test_a.py"], readme_text="run pytest"))
    assert both.score == 100 and any("not executed" in e for e in both.evidence)
    assert any("not run" in w for w in both.warnings)
    only = rd._tests_signal(docs_for(["tests/test_a.py"], readme_text="no commands here"))
    assert only.score == 60 and any("No test-running command" in w for w in only.warnings)
    unreadable = rd._tests_signal(docs_for(["tests/test_a.py"]))
    assert unreadable.score == 60 and unreadable.confidence == 0.45
    none = rd._tests_signal(docs_for(["src/a.py"], readme_text="hi"))
    assert none.score == 10 and none.status == "known"
    assert rd._tests_signal(docs_for(["src/a.py"], truncated=True)).status == "unknown"


def test_test_directory_alone_does_not_claim_tests_exist_or_pass():
    sig = rd._tests_signal(docs_for(["tests/README.md"]))  # a tests/ dir counts as detection only
    assert "pass" not in " ".join(sig.evidence).lower() or "not" in " ".join(sig.warnings).lower()
    assert any("were not run" in w for w in sig.warnings)


def test_issue_quality_bug_report_complete_vs_incomplete():
    body = ("Steps to reproduce:\n1. open the app\n2. click save\n\nExpected: file saved\nActual: crash\n\n```\nTraceback (most "
            "recent call last)\n```\nVersion 2.3.1 on Ubuntu. " + "context " * 30)
    full = rd._description_signal(issue_dict(title="Crash on save", body=body))
    assert full.score == 100.0
    thin = rd._description_signal(issue_dict(title="Crash on save", body="it breaks"))
    assert thin.score == 0.0 and thin.status == "known"
    assert "reproduction steps" in thin.warnings[0] and "expected vs actual" in thin.warnings[0]
    empty = rd._description_signal(issue_dict(body=None))
    assert empty.score == 0.0


def test_issue_quality_feature_rubric_uses_acceptance_criteria():
    good = rd._description_signal(issue_dict(title="Add dark mode", body="We should add a toggle. Acceptance criteria:\n- [ ] persists\nSee https://example.com " + "y" * 200))
    assert good.score == 100.0 and "feature or task" in good.evidence[0]
    assert rd.analyze_issue_quality("Add x", "short", ())["rubric"] == "feature_or_task"
    assert rd.analyze_issue_quality("Add x", "short", ("Bug",))["rubric"] == "bug_report"


def ref(number, state="open", merged=None, repo="o/r", pr=True):
    return TimelineReference.model_validate({
        "source_number": number, "source_is_pull_request": pr, "source_state": state, "source_repository": repo,
        "source_merged_at": merged, "source_html_url": f"https://github.com/{repo}/pull/{number}", "created_at": at(1)})


def linked_for(refs, truncated=False, age_days=0):
    return {**rd.classify_linked_prs(refs, "o/r"), "collected_at": at(age_days), "timeline_truncated": truncated}


def test_linked_pr_states_are_distinguished():
    assert rd.classify_linked_prs([ref(1, "open"), ref(2, "closed", merged=at(3)), ref(3, "closed")], "o/r")["pull_requests"] == [
        {"number": 1, "state": "open", "html_url": "https://github.com/o/r/pull/1", "merged_at": None},
        {"number": 2, "state": "merged", "html_url": "https://github.com/o/r/pull/2", "merged_at": at(3)},
        {"number": 3, "state": "closed_unmerged", "html_url": "https://github.com/o/r/pull/3", "merged_at": None}]


@pytest.mark.parametrize("refs,expected_score,fragment", [
    ([], 100, "No pull requests"),
    ([ref(1, "open")], 25, "already be working"),
    ([ref(2, "closed", merged=at(3))], 40, "partly or already addressed"),
    ([ref(3, "closed")], 70, "closed, unmerged"),
    ([ref(1, "open"), ref(3, "closed")], 25, "already be working"),  # open takes precedence
])
def test_linked_pr_signal(refs, expected_score, fragment):
    sig = rd._prs_signal(linked_for(refs), NOW, CFG)
    assert sig.score == expected_score and fragment in " ".join(sig.evidence)
    assert "duplicate" not in " ".join(sig.evidence).lower()  # never auto-labelled a duplicate
    assert any("cross-references" in w for w in sig.warnings)


def test_linked_pr_edge_cases():
    assert rd.classify_linked_prs([ref(9, repo="other/x"), ref(8, pr=False)], "o/r") == {"pull_requests": [], "other_repository_references": 1}
    assert rd._prs_signal(None, NOW, CFG).status == "unknown"
    stale = rd._prs_signal(linked_for([], age_days=10), NOW, CFG)
    assert stale.status == "stale" and stale.score is None
    assert any("truncated" in w for w in rd._prs_signal(linked_for([], truncated=True), NOW, CFG).warnings)


def test_label_signal():
    assert rd._labels_signal(("Good First Issue",)).score == 100
    assert rd._labels_signal(("help wanted", "bug")).score == 75
    none = rd._labels_signal(("bug",))
    assert none.status == "unknown" and none.score is None  # no label is not evidence of unsuitability


def test_availability_signal_and_closed_issue_report():
    assert rd._availability_signal(issue_dict(assignees=["a"]), NOW, CFG).score == 30
    assert rd._availability_signal(issue_dict(fetched_at=at(30)), NOW, CFG).status == "stale"
    rep = compute_readiness(issue_dict(state="closed"), docs_for(["CONTRIBUTING.md"]), linked_for([]), NOW, CFG)
    assert rep.label == "not_available" and rep.score == 0.0


def test_readiness_report_well_prepared_vs_insufficient():
    good_body = ("Steps to reproduce:\n1. a\n2. b\nExpected: x\nActual: y\n```\nTraceback\n```\nversion 1.2.3 " + "z" * 200)
    docs = docs_for(["CONTRIBUTING.md", "README.md", "tests/test_a.py", ".github/PULL_REQUEST_TEMPLATE.md", "CODE_OF_CONDUCT.md",
                     ".github/ISSUE_TEMPLATE/a.yml", "SECURITY.md"], readme_text="Run pytest")
    rep = compute_readiness(issue_dict(body=good_body, labels=["good first issue"]), docs, linked_for([]), NOW, CFG)
    assert rep.label == "well_prepared" and 65 <= rep.score < 75 and rep.coverage >= 0.5  # confidence caps bound the max
    assert rep.kind == "issue_readiness" and "not executed" in rep.notes[0]
    sparse = compute_readiness(issue_dict(body="hmm"), None, None, NOW, CFG)
    assert sparse.label == "insufficient_data" and sparse.coverage < CFG.min_coverage
    unknown_names = {n for n, s in sparse.signals.items() if s.status == "unknown"}
    assert {"contribution_guide", "tests", "linked_pull_requests", "other_documentation"} <= unknown_names


def test_missing_docs_lowers_readiness_versus_present_docs():
    base = dict(issue=issue_dict(body="y" * 250, labels=["help wanted"]), linked=linked_for([]), now=NOW, cfg=CFG)
    with_docs = compute_readiness(docs=docs_for(["CONTRIBUTING.md", "tests/test_a.py", "README.md"], readme_text="pytest"), **base)
    without = compute_readiness(docs=docs_for(["src/a.py"], readme_text="hello"), **base)
    assert with_docs.score > without.score


def test_health_and_readiness_are_separate_reports_with_disjoint_signals():
    h = compute_health(full_observations(), NOW, CFG)
    r = compute_readiness(issue_dict(), docs_for(["README.md"]), None, NOW, CFG)
    assert h.kind != r.kind and not set(h.signals) & set(r.signals)
