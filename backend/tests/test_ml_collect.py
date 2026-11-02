"""Data collection: rate limits, resumability, privacy, provenance. GitHub is mocked; no network, no credentials."""

import hashlib
import json

import pytest

from compass_ml_training import collect as col
from compass_ml_training import dataset as ds
from tests import intel_data as D
from tests.github_mock import NOW, FakeGitHub, ok, status


def repo_payload(full, private=False, language="Python"):
    owner, name = full.split("/")
    return {"name": name, "full_name": full, "owner": {"login": owner}, "html_url": f"https://github.com/{full}",
            "default_branch": "main", "private": private, "language": language, "description": None, "topics": [],
            "archived": False, "fork": False}


def issues_for(full, numbers=(1, 2, 3), prs=(), body="Crash when loading, contact bob@example.com or @alice. " + "x" * 80):
    out = []
    for n in numbers:
        d = D.issue_payload(n, 10 + n, body=body, labels=("bug",), title=f"{full} issue {n}", user="secretusername")
        d["html_url"] = f"https://github.com/{full}/issues/{n}"
        out.append(d)
    out += [D.issue_payload(n, 5, pr=True) for n in prs]
    return out


def routes_for(full, **kw):
    label_issues = kw.pop("label_issues", None)

    def listing(request):
        labels = request.url.params.get("labels")
        if labels and label_issues is not None:
            return ok(label_issues)
        return ok(issues_for(full, **kw))

    return {f"/repos/{full}": ok(repo_payload(full)), f"/repos/{full}/issues": listing}


@pytest.fixture
def gh_factory():
    def make(routes):
        fake = FakeGitHub(routes)
        return fake, fake.client
    return make


# ---------------------------------------------------------------- one repository


def test_collects_public_issues_with_provenance_and_no_personal_data(gh_factory):
    fake, gh = gh_factory(routes_for("a/one", prs=(90,)))
    recs = col.collect_repository_issues(gh, "a", "one", fetched_at="2026-01-01T00:00:00+00:00")
    assert [r.issue_number for r in recs] == [1, 2, 3]  # the pull request is excluded
    r = recs[0]
    assert (r.repo, r.source, r.repo_language, r.fetched_at) == ("a/one", "github_api", "Python", "2026-01-01T00:00:00+00:00")
    assert r.html_url == "https://github.com/a/one/issues/1" and r.created_at and r.record_id == ds.make_record_id("a/one", 1)
    assert "bob@example.com" not in r.body and "@alice" not in r.body and "user@example.invalid" in r.body
    blob = json.dumps([x.model_dump() for x in recs])
    assert "secretusername" not in blob  # authors are never stored (only an association category)


def test_label_queries_are_merged_without_duplicates(gh_factory):
    extra = [D.issue_payload(2, 12, labels=("good first issue",), title="dup"), D.issue_payload(50, 3, labels=("good first issue",), title="gfi")]
    fake, gh = gh_factory(routes_for("a/one", numbers=(1, 2), label_issues=extra))
    recs = col.collect_repository_issues(gh, "a", "one", col.CollectParams(label_queries=("good first issue",)))
    assert [r.issue_number for r in recs] == [1, 2, 50] and len({r.record_id for r in recs}) == 3
    assert gh.requests_made == 3  # repository + issue page + one label query: requests are accounted for


def test_private_or_unknown_visibility_refused(gh_factory):
    for payload in ({**repo_payload("a/one"), "private": True}, {k: v for k, v in repo_payload("a/one").items() if k != "private"}):
        fake, gh = gh_factory({"/repos/a/one": ok(payload)})
        with pytest.raises(ValueError, match="not confirmed public"):
            col.collect_repository_issues(gh, "a", "one")
        assert fake.calls["/repos/a/one/issues"] == 0  # no issue data was even requested


# ---------------------------------------------------------------- many repositories: resumable, rate limited, audited


def test_collect_many_writes_files_and_manifest(gh_factory, tmp_path):
    fake, gh = gh_factory({**routes_for("a/one"), **routes_for("b/two", numbers=(7, 8))})
    run = col.collect_many(gh, ["a/one", "b/two"], tmp_path, now="2026-01-01T00:00:00+00:00")
    assert [r["status"] for r in run["repositories"]] == ["collected", "collected"]
    f = tmp_path / "a__one.jsonl"
    assert run["repositories"][0]["sha256"] == hashlib.sha256(f.read_bytes()).hexdigest()
    assert run["repositories"][0]["n_issues"] == 3 and run["repositories"][1]["n_issues"] == 2
    assert run["requests_made"] == 4 and run["authenticated"] == gh.authenticated and run["stopped_by_rate_limit"] is False
    assert "ghp_" not in (tmp_path / "collection_manifest.json").read_text()  # auth state is recorded, never the token
    assert run["collector_version"] == "collector_v1" and run["github_api_version"] and run["params"]["max_pages"] == 1
    manifest = json.loads((tmp_path / "collection_manifest.json").read_text())
    assert len(manifest["runs"]) == 1 and manifest["runs"][0]["started_at"] == "2026-01-01T00:00:00+00:00"
    assert {r.fetched_at for r in ds.read_jsonl(f)} == {"2026-01-01T00:00:00+00:00"}


def test_rate_limit_stops_cleanly_and_the_run_can_resume(gh_factory, tmp_path):
    reset = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 3600)}
    limited = {"/repos/b/two": status(403, {"message": "API rate limit exceeded"}, **reset)}
    fake, gh = gh_factory({**routes_for("a/one"), **limited, **routes_for("c/three")})
    run = col.collect_many(gh, ["a/one", "b/two", "c/three"], tmp_path)
    assert [r["status"] for r in run["repositories"]] == ["collected", "rate_limited", "not_attempted_after_rate_limit"]
    assert run["stopped_by_rate_limit"] is True and run["repositories"][1]["retry_after_seconds"] >= 3600
    assert fake.calls["/repos/c/three"] == 0 and (tmp_path / "a__one.jsonl").exists()
    assert not (tmp_path / "b__two.jsonl").exists()  # nothing half-written for the interrupted repository

    fake2, gh2 = gh_factory({**routes_for("a/one"), **routes_for("b/two"), **routes_for("c/three")})  # quota is back
    run2 = col.collect_many(gh2, ["a/one", "b/two", "c/three"], tmp_path)
    assert [r["status"] for r in run2["repositories"]] == ["skipped_existing", "collected", "collected"]
    assert fake2.calls["/repos/a/one"] == 0  # already collected: not fetched again
    assert len(json.loads((tmp_path / "collection_manifest.json").read_text())["runs"]) == 2


def test_one_failing_repository_does_not_stop_the_others(gh_factory, tmp_path):
    fake, gh = gh_factory({"/repos/a/bad": status(503), **routes_for("b/ok"),
                           "/repos/c/priv": ok({**repo_payload("c/priv"), "private": True})})
    run = col.collect_many(gh, ["a/bad", "c/priv", "b/ok"], tmp_path)
    assert [r["status"] for r in run["repositories"]] == ["failed", "refused_not_public", "collected"]
    assert sorted(p.name for p in tmp_path.glob("*.jsonl")) == ["b__ok.jsonl"]


def test_refresh_recollects_existing_files(gh_factory, tmp_path):
    fake, gh = gh_factory(routes_for("a/one"))
    col.collect_many(gh, ["a/one"], tmp_path)
    again = col.collect_many(gh, ["a/one"], tmp_path, refresh=True)
    assert again["repositories"][0]["status"] == "collected"


def test_failed_write_leaves_no_partial_file(gh_factory, tmp_path, monkeypatch):
    fake, gh = gh_factory(routes_for("a/one"))
    monkeypatch.setattr(col, "write_jsonl", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        col.collect_many(gh, ["a/one"], tmp_path)
    assert not (tmp_path / "a__one.jsonl").exists()


# ---------------------------------------------------------------- inputs


def test_repo_list_parsing(tmp_path):
    f = tmp_path / "repos.txt"
    f.write_text("# comment\n\npallets/flask  # trailing note\nPallets/Flask\npsf/requests\n")
    assert col.read_repo_list(f) == ["pallets/flask", "psf/requests"]  # blanks, comments, case-duplicates dropped
    for bad in ("justaname", "a/b/c", "a /b", "/x"):
        f.write_text(bad + "\n")
        with pytest.raises(ValueError, match="invalid repository entry"):
            col.read_repo_list(f)


def test_load_raw_dir_merges_files_and_dedupes(gh_factory, tmp_path):
    fake, gh = gh_factory({**routes_for("a/one"), **routes_for("b/two")})
    col.collect_many(gh, ["a/one", "b/two"], tmp_path)
    (tmp_path / "copy.jsonl").write_text((tmp_path / "a__one.jsonl").read_text())  # same ids again
    merged = col.load_raw_dir(tmp_path)
    assert len(merged) == 6 and len({r.record_id for r in merged}) == 6


def test_identical_issue_templates_across_repositories_are_deduplicated_at_build(gh_factory, tmp_path):
    same = "Template body that every repository shares verbatim. " * 3
    fake, gh = gh_factory({**routes_for("a/one", numbers=(1,), body=same), **routes_for("b/two", numbers=(1,), body=same)})
    # titles differ per repo in routes_for, so make them identical to model a copied template
    recs = col.collect_repository_issues(gh, "a", "one") + col.collect_repository_issues(gh, "b", "two")
    recs = [r.model_copy(update={"title": "Bug report"}) for r in recs]
    kept, report = ds.build_dataset(recs)
    assert len(kept) == 1 and report["dropped"] == {"duplicate_text": 1}
