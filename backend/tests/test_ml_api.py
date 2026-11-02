"""POST /issues/{o}/{r}/{n}/difficulty: ML estimate kept separate from the deterministic label hint, with fallback."""

import shutil
import sys

import pytest
from sqlalchemy import select

from app.api.deps import get_github_client
from app.core.config import Settings, get_settings
from app.db.models import Issue, IssuePrediction
from tests import intel_data as D
from tests.github_mock import REPO, FakeGitHub, ok, status
from tests.seed import make_user

URL = "/api/v1/issues/o/r/{n}/difficulty"
SECRET = "s" * 48
LONG = "The scheduler hits a deadlock when migration and streaming run together under concurrency. " * 3


@pytest.fixture
def env(client, db_session):
    gh = FakeGitHub({"/repos/o/r": ok(REPO)})
    client.app.dependency_overrides[get_github_client] = lambda: gh.client
    configure(client)  # default: no model configured
    return gh


def configure(client, model_dir=None, allow_synthetic=True):
    s = Settings(auth_secret=SECRET, ml_difficulty_model_dir=str(model_dir) if model_dir else None,
                 ml_allow_synthetic_artifacts=allow_synthetic, _env_file=None)
    client.app.dependency_overrides[get_settings] = lambda: s


@pytest.fixture
def auth(db_session, env):
    return make_user(db_session, 1, "alice")[0]


def add_issue(env, number=1, body=LONG, title="Scheduler deadlock", labels=()):
    env.routes[f"/repos/o/r/issues/{number}"] = ok(D.issue_payload(number, 5, body=body, labels=labels, title=title))


@pytest.fixture(scope="module")
def trained_dir(tmp_path_factory):
    pytest.importorskip("sklearn")
    from compass_ml_training import dataset as ds
    from compass_ml_training.train import train
    from tests.ml_synthetic import synthetic_records
    tmp = tmp_path_factory.mktemp("apimodel")
    records, _ = ds.build_dataset(synthetic_records())
    ds.write_jsonl(tmp / "d.jsonl", records)
    return train(tmp / "d.jsonl", tmp / "models", seed=42, created_at="2025-01-01T00:00:00+00:00")["model_dir"]


@pytest.fixture
def model_copy(trained_dir, tmp_path):
    dest = tmp_path / "m"
    shutil.copytree(trained_dir, dest)
    return dest


# ================================================================ no model: fallback to the deterministic hint


def test_not_configured_falls_back_to_label_hint(client, auth, env, db_session):
    add_issue(env, labels=("good first issue", "bug"))
    r = client.post(URL.format(n=1), headers=auth)
    assert r.status_code == 200
    b = r.json()
    assert b["ml_prediction"]["status"] == "unavailable" and b["ml_prediction"]["reason"] == "not_configured"
    assert b["ml_prediction"]["label"] is None and b["ml_prediction"]["class_scores"] == {}
    hint = b["deterministic_signals"]["difficulty_label_hint"]
    assert hint["level"] == 1 and hint["maintainer_label"] == "good first issue" and "noisy" in hint["note"]
    assert b["fallback"]["used"] is True and "only difficulty indication" in b["fallback"]["description"]
    assert db_session.query(IssuePrediction).count() == 0  # nothing predicted, nothing stored


def test_no_model_and_no_label_means_unknown_not_invented(client, auth, env):
    add_issue(env, labels=("bug",))
    b = client.post(URL.format(n=1), headers=auth).json()
    assert b["deterministic_signals"]["difficulty_label_hint"] is None and b["ml_prediction"]["label"] is None
    assert "unknown" in b["fallback"]["description"] and "No value is invented" in b["fallback"]["description"]


@pytest.mark.parametrize("reason,setup", [
    ("artifact_missing", lambda c, tmp: tmp / "nope"),
])
def test_unusable_model_dir_degrades_gracefully(client, auth, env, tmp_path, reason, setup):
    add_issue(env, labels=("easy",))
    configure(client, setup(client, tmp_path))
    r = client.post(URL.format(n=1), headers=auth)
    assert r.status_code == 200 and r.json()["ml_prediction"]["reason"] == reason
    assert r.json()["deterministic_signals"]["difficulty_label_hint"]["level"] == 2


def test_missing_ml_dependencies_do_not_break_the_endpoint(client, auth, env, model_copy, monkeypatch):
    add_issue(env, labels=("hard",))
    configure(client, model_copy)
    monkeypatch.setitem(sys.modules, "joblib", None)
    r = client.post(URL.format(n=1), headers=auth)
    assert r.status_code == 200
    assert r.json()["ml_prediction"] == {**r.json()["ml_prediction"], "status": "unavailable", "reason": "ml_dependencies_missing"}
    assert r.json()["deterministic_signals"]["difficulty_label_hint"]["level"] == 4 and r.json()["fallback"]["used"]


def test_synthetic_artifact_refused_unless_explicitly_allowed(client, auth, env, model_copy):
    add_issue(env)
    configure(client, model_copy, allow_synthetic=False)
    assert client.post(URL.format(n=1), headers=auth).json()["ml_prediction"]["reason"] == "synthetic_artifact_refused"


def test_corrupted_model_reports_artifact_invalid(client, auth, env, model_copy):
    add_issue(env)
    (model_copy / "model.joblib").write_bytes(b"corrupt")
    configure(client, model_copy)
    b = client.post(URL.format(n=1), headers=auth).json()
    assert b["ml_prediction"]["status"] == "unavailable" and b["ml_prediction"]["reason"] == "artifact_invalid"


# ================================================================ model available


def test_prediction_is_reported_separately_from_the_hint_and_persisted(client, auth, env, model_copy, db_session):
    add_issue(env, labels=("good first issue",))
    configure(client, model_copy)
    r = client.post(URL.format(n=1), headers=auth)
    assert r.status_code == 200
    b = r.json()
    ml = b["ml_prediction"]
    assert ml["status"] == "predicted" and ml["label"] in {"beginner", "easy", "intermediate", "advanced", "expert"}
    assert ml["calibrated"] is False and "NOT probabilities" in ml["note"] and ml["margin"] is not None
    assert ml["model"]["id"].startswith("ml_difficulty_lr_five_class_") and ml["model"]["kind"] == "ml"
    assert ml["model"]["label_source"] == "weak_labels" and ml["model"]["evaluation_status"] == "synthetic_not_for_use"
    assert sum(ml["class_scores"].values()) == pytest.approx(1.0, abs=1e-3)
    assert b["deterministic_signals"]["difficulty_label_hint"]["level"] == 1  # shown, not merged into the ML result
    assert b["fallback"]["used"] is False and "not combined" in b["fallback"]["description"]

    row = db_session.scalar(select(IssuePrediction))
    assert row.model_version == ml["model"]["id"] and row.model_status == "ml_weak_labels"
    assert row.predicted_category == ml["label"] and row.confidence is None  # no calibrated confidence exists
    assert row.predicted_difficulty in {1, 2, 3, 4, 5} and row.class_probabilities == ml["class_scores"]
    assert row.explanation["calibrated"] is False and row.explanation["label_source"] == "weak_labels"
    assert ml["prediction_id"] == row.id and ml["generated_at"]


def test_repeated_requests_reuse_the_stored_prediction_and_changes_create_a_new_one(client, auth, env, model_copy, db_session):
    add_issue(env)
    configure(client, model_copy)
    first = client.post(URL.format(n=1), headers=auth).json()["ml_prediction"]["prediction_id"]
    assert client.post(URL.format(n=1), headers=auth).json()["ml_prediction"]["prediction_id"] == first
    assert db_session.query(IssuePrediction).count() == 1
    issue = db_session.scalar(select(Issue))
    issue.body = "A completely different description about kernel allocator and lockfree consensus " * 3
    db_session.commit()
    second = client.post(URL.format(n=1), headers=auth).json()["ml_prediction"]["prediction_id"]
    assert second != first and db_session.query(IssuePrediction).count() == 2


def test_maintainer_labels_do_not_influence_the_model(client, auth, env, model_copy):
    add_issue(env, 1, labels=("good first issue",))
    add_issue(env, 2, labels=("expert", "hard"))
    configure(client, model_copy)
    a = client.post(URL.format(n=1), headers=auth).json()["ml_prediction"]
    b = client.post(URL.format(n=2), headers=auth).json()["ml_prediction"]
    assert a["label"] == b["label"] and a["class_scores"] == b["class_scores"]  # same text, different labels


def test_unsupported_input_returns_no_prediction(client, auth, env, model_copy, db_session):
    add_issue(env, body="hi", title="x")
    configure(client, model_copy)
    ml = client.post(URL.format(n=1), headers=auth).json()["ml_prediction"]
    assert ml["status"] == "unsupported_input" and ml["reason"] == "not_enough_text" and ml["label"] is None
    assert db_session.query(IssuePrediction).count() == 0


def test_heuristic_recommendations_are_unaffected_by_the_model(client, auth, env, model_copy, db_session):
    from tests.seed import PROFILE, seed_issue, seed_repo
    client.post("/api/v1/profile", json=PROFILE, headers=auth)
    repo = seed_repo(db_session, "acme", "x", "Python")
    seed_issue(db_session, repo, 1, "Parser crashes", "y" * 120)
    plain = client.post("/api/v1/matches/search", json={"persist": False}, headers=auth).json()
    configure(client, model_copy)
    with_model = client.post("/api/v1/matches/search", json={"persist": False}, headers=auth).json()
    assert plain == with_model and with_model["model"]["version"] == "v1"
    assert "ml" not in str(with_model["weights"]).lower()


# ================================================================ errors + auth


def test_endpoint_errors_and_auth(client, auth, env):
    assert client.post(URL.format(n=1)).status_code == 401
    assert client.post(URL.format(n=1), headers={"Authorization": "Bearer junk"}).status_code == 401
    for bad in ("0", "abc", "-1"):
        assert client.post(URL.format(n=bad), headers=auth).status_code == 422
    assert client.post(URL.format(n=77), headers=auth).json()["error"]["code"] == "issue_not_found"
    env.routes["/repos/o/r/issues/5"] = ok(D.issue_payload(5, 3, pr=True))
    assert client.post(URL.format(n=5), headers=auth).json()["error"]["code"] == "not_an_issue"
    env.routes["/repos/o/r"] = status(503)  # repository data is fresh in the database, so GitHub is not asked again
    assert client.post(URL.format(n=1), headers=auth).json()["error"]["code"] == "issue_not_found"


@pytest.fixture(scope="module")
def exploratory_dir(tmp_path_factory):
    pytest.importorskip("sklearn")
    from compass_ml_training import dataset as ds
    from compass_ml_training.train import train
    from tests.ml_synthetic import synthetic_records
    tmp = tmp_path_factory.mktemp("explore")
    records, _ = ds.build_dataset(synthetic_records(n_repos=10, per_repo=8, seed=3))
    ds.write_jsonl(tmp / "d.jsonl", records)
    return train(tmp / "d.jsonl", tmp / "models", scheme="three_class", thresholds=ds.Thresholds(40, 5, 6),
                 seed=1, created_at="2025-01-01T00:00:00+00:00")["model_dir"]


def test_exploratory_model_is_not_served_unless_explicitly_allowed(client, auth, env, exploratory_dir, tmp_path):
    add_issue(env, labels=("easy",))
    dest = tmp_path / "e"
    shutil.copytree(exploratory_dir, dest)
    configure(client, dest, allow_synthetic=True)  # synthetic allowed, exploratory not
    b = client.post(URL.format(n=1), headers=auth).json()
    assert b["ml_prediction"]["status"] == "unavailable" and b["ml_prediction"]["reason"] == "exploratory_artifact_refused"
    assert b["deterministic_signals"]["difficulty_label_hint"]["level"] == 2 and b["fallback"]["used"] is True
    s = Settings(auth_secret=SECRET, ml_difficulty_model_dir=str(dest), ml_allow_synthetic_artifacts=True,
                 ml_allow_exploratory_artifacts=True, _env_file=None)
    client.app.dependency_overrides[get_settings] = lambda: s
    assert client.post(URL.format(n=1), headers=auth).json()["ml_prediction"]["status"] == "predicted"
