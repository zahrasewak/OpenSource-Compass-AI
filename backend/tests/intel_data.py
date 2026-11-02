"""Builders for repository-intelligence tests: mocked GitHub payloads and stored snapshots.
All timestamps are relative to the real clock so data looks 'recent' to the code under test."""

import base64
from datetime import datetime, timedelta, timezone

from app.db.models import IssueFeature, RepositorySnapshot
from app.intelligence import activity as act
from app.intelligence import readiness as rd
from app.intelligence.config import ANALYSIS_VERSION, READINESS_FEATURE_VERSION, IntelConfig
from app.services.github.models import (
    CommitSummary, FileText, GitTree, Issue, IssueComment, PullRequestSummary, Release,
)
from tests.github_mock import REPO, ok

HEAD = "a" * 40
CFG = IntelConfig()


def now() -> datetime:
    return datetime.now(timezone.utc)


def ago(days=0, hours=0) -> datetime:
    return now() - timedelta(days=days, hours=hours)


def iso_ago(days=0, hours=0) -> str:
    return ago(days, hours).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ GitHub payloads (for FakeGitHub routes)


def commit_payload(days, author="dev"):
    return {"sha": HEAD, "author": {"login": author}, "commit": {"committer": {"date": iso_ago(days)}}}


def release_payload(days, tag="v1"):
    return {"tag_name": tag, "published_at": iso_ago(days), "draft": False, "prerelease": False}


def issue_payload(number, created_days, closed_days=None, assoc="NONE", user="user1", pr=False, body=None, labels=(), title=None):
    d = {"number": number, "title": title or f"issue {number}", "body": body, "state": "closed" if closed_days is not None else "open",
         "html_url": f"https://github.com/o/r/issues/{number}", "labels": [{"name": l} for l in labels],
         "user": {"login": user}, "author_association": assoc, "assignees": [], "comments": 0,
         "created_at": iso_ago(created_days), "updated_at": iso_ago(0), "closed_at": iso_ago(closed_days) if closed_days is not None else None}
    if pr:
        d["pull_request"] = {"url": "x"}
    return d


def pr_payload(number, created_days, merged_days=None, closed_days=None, assoc="NONE"):
    closed = merged_days if merged_days is not None else closed_days
    return {"number": number, "title": "pr", "state": "closed" if closed is not None else "open", "user": {"login": "contrib"},
            "author_association": assoc, "created_at": iso_ago(created_days),
            "closed_at": iso_ago(closed) if closed is not None else None,
            "merged_at": iso_ago(merged_days) if merged_days is not None else None}


def comment_payload(cid, number, days, hours=0, user="maint", assoc="MEMBER"):
    return {"id": cid, "user": {"login": user}, "author_association": assoc, "body": "thanks",
            "created_at": iso_ago(days, hours), "issue_url": f"https://api.github.com/repos/o/r/issues/{number}"}


def tree_payload(paths, truncated=False, sha="t" * 40):
    return {"sha": sha, "truncated": truncated, "tree": [{"path": p, "type": "blob"} for p in paths]}


def file_payload(text):
    raw = text.encode()
    return {"type": "file", "size": len(raw), "encoding": "base64", "content": base64.b64encode(raw).decode()}


def timeline_payload(refs):
    """refs: (number, state, merged_days_ago|None, repo='o/r', is_pr=True)"""
    events = [{"event": "labeled", "created_at": iso_ago(5)}]
    for ref in refs:
        number, state, merged, *rest = ref
        repo = rest[0] if rest else "o/r"
        is_pr = rest[1] if len(rest) > 1 else True
        source = {"number": number, "state": state, "html_url": f"https://github.com/{repo}/pull/{number}",
                  "repository": {"full_name": repo}}
        if is_pr:
            source["pull_request"] = {"merged_at": iso_ago(merged) if merged is not None else None}
        events.append({"event": "cross-referenced", "created_at": iso_ago(2), "source": {"type": "issue", "issue": source}})
    return events


GOOD_PATHS = ["README.md", "CONTRIBUTING.md", "LICENSE", "tests/test_core.py", ".github/workflows/ci.yml",
              ".github/dependabot.yml", ".github/PULL_REQUEST_TEMPLATE.md", "CODE_OF_CONDUCT.md", "src/app.py"]
README = "# Project\n## Contributing\nSee CONTRIBUTING.md. Run the tests with `pytest`.\n"
GOOD_BODY = ("Steps to reproduce:\n1. open the app\n2. click save\n\nExpected: file saved\nActual: crash\n\n```\nTraceback\n```\n"
             "Version 2.3.1 on Ubuntu. " + "details " * 30)


def healthy_routes(target=9, target_body=GOOD_BODY, target_labels=("good first issue",), timeline=(), paths=None, extra=None):
    """Complete, coherent mocked GitHub data for repository o/r with a healthy-looking history."""
    issues = [issue_payload(n, 20 + n, closed_days=(10 + n) if n <= 8 else None) for n in range(1, 11)]
    issues = [i for i in issues if i["number"] != target] + [issue_payload(target, 20 + target, body=target_body, labels=target_labels, title="Crash when saving")]
    routes = {
        "/repos/o/r": ok(REPO),
        "/repos/o/r/commits": ok([commit_payload(1 + 3 * i, f"dev{i % 3}") for i in range(30)]),
        "/repos/o/r/releases": ok([release_payload(15, "v2"), release_payload(60, "v1")]),
        "/repos/o/r/issues": ok(issues),
        "/repos/o/r/pulls": ok([pr_payload(100 + n, 30, merged_days=20) for n in range(4)] + [pr_payload(110, 5)]),
        "/repos/o/r/issues/comments": ok([comment_payload(n, n, 20 + n, hours=-10) for n in range(1, 11)]),
        f"/repos/o/r/git/trees/{HEAD}": ok(tree_payload(paths or GOOD_PATHS)),
        "/repos/o/r/contents/README.md": ok(file_payload(README)),
        "/repos/o/r/contents/CONTRIBUTING.md": ok(file_payload("Please add tests. Run `pytest` before opening a PR.")),
        f"/repos/o/r/issues/{target}": ok(issue_payload(target, 20 + target, body=target_body, labels=target_labels, title="Crash when saving")),
        f"/repos/o/r/issues/{target}/timeline": ok(timeline_payload(timeline)),
    }
    routes.update(extra or {})
    return routes


# ------------------------------------------------------------------ stored snapshots (for recommendation tests)


def _cm(days, author="dev"):
    return CommitSummary.model_validate(commit_payload(days, author))


def observations(kind: str, at: datetime | None = None) -> dict:
    at = at or now()
    if kind == "healthy":
        commits = [_cm(1 + 3 * i, f"dev{i % 3}") for i in range(30)]
        issues = [Issue.model_validate(issue_payload(n, 20 + n, closed_days=(10 + n) if n <= 8 else None)) for n in range(1, 11)]
        prs = [PullRequestSummary.model_validate(pr_payload(100 + n, 30, merged_days=20)) for n in range(4)]
        comments = [IssueComment.model_validate(comment_payload(n, n, 20 + n, hours=-10)) for n in range(1, 11)]
        releases = [Release.model_validate(release_payload(15))]
        docs = {**rd.analyze_tree(GitTree.model_validate(tree_payload(GOOD_PATHS))), "instructions_status": "ok"}
        license_ = "MIT"
    elif kind == "inactive":
        commits = [_cm(800), _cm(900)]
        issues, prs, comments, releases = [], [], [], []
        docs = {**rd.analyze_tree(GitTree.model_validate(tree_payload(["LICENSE"]))), "instructions_status": "ok"}
        license_ = "MIT"
    else:
        raise ValueError(kind)
    return {
        "commits": act.summarize_commits(commits, False, at, CFG.window_days),
        "releases": act.summarize_releases(releases, at, CFG.window_days),
        "issues": act.summarize_issues(issues, False, at, CFG.window_days),
        "pulls": act.summarize_pulls(prs, False, at, CFG.window_days),
        "responses": act.summarize_responses(issues, prs, comments, False, at, CFG),
        "maintenance": act.summarize_maintenance(False, license_, at - timedelta(days=1), docs),
    }, docs


def store_snapshot(db, repo, kind="healthy", age_days=0, sha=HEAD, archived=False, complete=True):
    at = ago(age_days)
    obs, docs = observations(kind, at)
    if archived:
        obs["maintenance"] = act.summarize_maintenance(True, "MIT", at, docs)
    data = {"schema": 1, "analysis_version": ANALYSIS_VERSION, "collected_at": act.iso(at), "window_days": CFG.window_days,
            "complete": complete, "errors": {} if complete else {"comments": "github_unavailable"}, "head_sha": sha,
            "default_branch": "main", "observations": obs, "docs": docs}
    snap = RepositorySnapshot(repository_id=repo.id, commit_sha=sha, analysis_version=ANALYSIS_VERSION, collected_at=at, data=data)
    db.add(snap)
    db.commit()
    return snap


def store_linked_prs(db, issue, refs=(), age_days=0):
    from app.services.github.models import TimelineReference
    at = ago(age_days)
    parsed = [TimelineReference.model_validate({
        "source_number": n, "source_is_pull_request": True, "source_state": st, "source_repository": "o/r",
        "source_merged_at": iso_ago(m) if m is not None else None}) for n, st, m in refs]
    data = {"schema": 1, "collected_at": act.iso(at), "timeline_truncated": False, **rd.classify_linked_prs(parsed, "o/r")}
    row = IssueFeature(issue_id=issue.id, feature_version=READINESS_FEATURE_VERSION, structured_features=data,
                       extracted_skills=[], created_at=at)
    db.add(row)
    db.commit()
    return row
