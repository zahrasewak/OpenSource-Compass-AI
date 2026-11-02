"""Collect public issues into raw dataset records using the project's rate-limit-aware GitHub client.

Data source and terms: GitHub's REST API, public repositories only, subject to the GitHub API Terms of Service and
Acceptable Use Policies. Issue text is user-generated content owned by its authors. This tool:
  * redacts e-mail addresses and @mentions, and stores NO usernames (no authors, commenters or assignees);
  * stores only issue title/body/labels and a few non-identifying fields;
  * refuses any repository GitHub does not explicitly report as public;
  * keeps files local (git-ignored); do not redistribute them without checking each repository's terms.

Reproducibility: each run appends to collection_manifest.json (parameters, per-repository result, request count,
rate-limit state, sha256 of every file written). Issues change over time, so the collection timestamp is part of the
dataset's provenance and a later run will not return identical data.
"""

import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.ml.features import redact_personal
from app.ml.schema import IssueRecord
from app.services.github import GitHubClient
from app.services.github.errors import GitHubError, GitHubRateLimitError
from app.services.github.client import API_VERSION
from compass_ml_training.dataset import make_record_id, read_jsonl, write_jsonl

COLLECTOR_VERSION = "collector_v1"
MANIFEST_NAME = "collection_manifest.json"


@dataclass(frozen=True)
class CollectParams:
    max_pages: int = 1  # pages of 100 items of the "all issues, most recently updated first" listing
    label_queries: tuple[str, ...] = ()  # extra single-page queries, e.g. ("good first issue",) to enrich labelled data
    state: str = "all"


def _to_record(repo, issue, fetched_at: str) -> IssueRecord:
    return IssueRecord(
        record_id=make_record_id(repo.full_name, issue.number), repo=repo.full_name, issue_number=issue.number,
        html_url=issue.html_url, title=redact_personal(issue.title), body=redact_personal(issue.body),
        labels=[l.name for l in issue.labels], author_association=issue.author_association,
        repo_language=repo.language, created_at=issue.created_at.isoformat(), fetched_at=fetched_at, source="github_api")


def collect_repository_issues(gh: GitHubClient, owner: str, name: str, params: CollectParams | None = None, *,
                              fetched_at: str | None = None) -> list[IssueRecord]:
    """Issues (pull requests excluded) of one PUBLIC repository, newest-updated first plus optional label queries."""
    params = params or CollectParams()
    repo = gh.get_repository(owner, name)
    if repo.private is not False:
        raise ValueError(f"{owner}/{name} is not confirmed public; refusing to collect")
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    found = {i.number: i for i in gh.list_issues(owner, name, state=params.state, max_pages=params.max_pages).items}
    for label in params.label_queries:
        for issue in gh.list_issues(owner, name, state=params.state, labels=[label], max_pages=1).items:
            found.setdefault(issue.number, issue)
    return [_to_record(repo, found[n], fetched_at) for n in sorted(found)]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect_many(gh: GitHubClient, repos: list[str], out_dir: str | Path, params: CollectParams | None = None, *,
                 refresh: bool = False, now: str | None = None) -> dict:
    """Collect several repositories into out_dir/<owner>__<name>.jsonl (resumable) and log the run.

    Existing repository files are skipped unless refresh=True, so a run interrupted by the unauthenticated rate limit
    (60 requests/hour) can simply be started again later. A rate limit stops the run cleanly; other failures are
    recorded per repository and do not stop it.
    """
    params = params or CollectParams()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    started = now or datetime.now(timezone.utc).isoformat()
    requests_before = gh.requests_made
    results: list[dict] = []
    stopped = False
    for full in repos:
        owner, _, name = full.partition("/")
        target = out / f"{owner}__{name}.jsonl"
        if stopped:
            results.append({"repo": full, "status": "not_attempted_after_rate_limit"})
            continue
        if target.exists() and not refresh:
            results.append({"repo": full, "status": "skipped_existing", "file": target.name, "sha256": _sha(target)})
            continue
        try:
            records = collect_repository_issues(gh, owner, name, params, fetched_at=started)
        except GitHubRateLimitError as exc:
            stopped = True
            results.append({"repo": full, "status": "rate_limited", "retry_after_seconds": exc.retry_after_seconds})
            continue
        except GitHubError as exc:
            results.append({"repo": full, "status": "failed", "error": type(exc).__name__})
            continue
        except ValueError as exc:
            results.append({"repo": full, "status": "refused_not_public", "detail": str(exc)})
            continue
        tmp = target.with_suffix(".tmp")
        write_jsonl(tmp, records)
        os.replace(tmp, target)  # a half-written file is never mistaken for a collected repository
        results.append({"repo": full, "status": "collected", "n_issues": len(records), "file": target.name, "sha256": _sha(target)})
    run = {
        "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(), "collector_version": COLLECTOR_VERSION,
        "github_api_version": API_VERSION, "authenticated": gh.authenticated, "params": {
            "max_pages": params.max_pages, "label_queries": list(params.label_queries), "state": params.state},
        "requests_made": gh.requests_made - requests_before, "rate_limit_at_end": (
            gh.last_rate_limit.model_dump() if gh.last_rate_limit else None),
        "repositories": results, "stopped_by_rate_limit": stopped,
    }
    manifest_path = out / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"manifest_schema": 1, "runs": []}
    manifest["runs"].append(run)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return run


def load_raw_dir(path: str | Path) -> list[IssueRecord]:
    """Merge every per-repository file in a raw directory; identical record ids appear once."""
    merged: dict[str, IssueRecord] = {}
    for f in sorted(Path(path).glob("*.jsonl")):
        for r in read_jsonl(f):
            merged.setdefault(r.record_id, r)
    return list(merged.values())


def read_repo_list(path: str | Path) -> list[str]:
    """One owner/name per line; blank lines and #-comments ignored; each entry validated; duplicates removed."""
    out: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        owner, _, name = line.partition("/")
        if not owner or not name or "/" in name or any(c.isspace() for c in line):
            raise ValueError(f"invalid repository entry {line!r}; expected owner/name")
        if line.lower() not in {x.lower() for x in out}:
            out.append(line)
    return out
