"""Model loading, integrity checks, prediction output and fallback behaviour (synthetic artifact; see ml_synthetic)."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.ml

pytest.importorskip("sklearn")

from app.ml import DifficultyModelService, DifficultyPredictor, IssueInput, ModelUnavailable  # noqa: E402
from app.ml.registry import MODEL_FILE, METADATA_FILE, load_artifact, sha256_file  # noqa: E402
from compass_ml_training import dataset as ds  # noqa: E402
from compass_ml_training.train import train  # noqa: E402
from tests.ml_synthetic import synthetic_records  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def artifact(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("art")
    records, _ = ds.build_dataset(synthetic_records())
    ds.write_jsonl(tmp / "d.jsonl", records)
    return Path(train(tmp / "d.jsonl", tmp / "models", seed=42, created_at="2025-01-01T00:00:00+00:00")["model_dir"])


@pytest.fixture
def copy(artifact, tmp_path):
    """A private, editable copy of the artifact."""
    dest = tmp_path / "model"
    shutil.copytree(artifact, dest)
    return dest


def edit_meta(d, **changes):
    meta = json.loads((d / METADATA_FILE).read_text())
    meta.update(changes)
    (d / METADATA_FILE).write_text(json.dumps(meta))


ISSUE = IssueInput(title="Scheduler deadlock under concurrency", body="The scheduler hits a deadlock when migration "
                   "and streaming run together " * 3, author_association="NONE", repo_language="Go")


# ---------------------------------------------------------------- predictions


def test_prediction_shape_and_honest_labelling(artifact):
    objects, meta = load_artifact(artifact, allow_synthetic=True)
    p = DifficultyPredictor(objects, meta).predict(ISSUE)
    assert p.status == "predicted" and p.label in {"beginner", "easy", "intermediate", "advanced", "expert"}
    assert set(p.class_scores) == {"beginner", "easy", "intermediate", "advanced", "expert"}
    assert sum(p.class_scores.values()) == pytest.approx(1.0, abs=1e-3)
    assert p.top_score == max(p.class_scores.values()) and p.label == max(p.class_scores, key=p.class_scores.get)
    ordered = sorted(p.class_scores.values(), reverse=True)
    assert p.margin == pytest.approx(ordered[0] - ordered[1], abs=1e-3)
    assert p.calibrated is False and p.label_source == "weak_labels" and p.model_id == meta["model_id"]
    assert "not probabilities" in p.note and "not verified difficulty" in p.note
    assert p.evaluation_status == "synthetic_not_for_use"


def test_prediction_is_deterministic_and_ignores_labels(artifact):
    objects, meta = load_artifact(artifact, allow_synthetic=True)
    predictor = DifficultyPredictor(objects, meta)
    assert predictor.predict(ISSUE) == predictor.predict(ISSUE)
    with_phrase = ISSUE.model_copy(update={"body": ISSUE.body + " good first issue, easy, trivial"})
    a, b = predictor.predict(with_phrase), predictor.predict(ISSUE)  # difficulty words are scrubbed, not read
    assert a.label == b.label
    assert all(abs(a.class_scores[c] - b.class_scores[c]) < 0.01 for c in b.class_scores)  # only stray punctuation differs


@pytest.mark.parametrize("issue", [IssueInput(title=""), IssueInput(title="ok", body=None), IssueInput(title="   ", body="  ")])
def test_unsupported_input_gets_no_prediction(artifact, issue):
    objects, meta = load_artifact(artifact, allow_synthetic=True)
    p = DifficultyPredictor(objects, meta).predict(issue)
    assert p.status == "unsupported_input" and p.reason == "not_enough_text" and p.label is None and p.class_scores == {}


def test_unknown_language_and_missing_fields_are_handled(artifact):
    objects, meta = load_artifact(artifact, allow_synthetic=True)
    p = DifficultyPredictor(objects, meta).predict(IssueInput(title="Scheduler deadlock", body="x " * 30, repo_language="Brainfuck"))
    assert p.status == "predicted"


# ---------------------------------------------------------------- loading failures (every one is a clear ModelUnavailable)


def expect(reason, d, **kw):
    with pytest.raises(ModelUnavailable) as ei:
        load_artifact(d, **kw)
    assert ei.value.reason == reason, ei.value


def test_synthetic_artifacts_are_refused_by_default(artifact):
    expect("synthetic_artifact_refused", artifact)
    load_artifact(artifact, allow_synthetic=True)


def test_missing_directory_and_files(tmp_path, copy):
    expect("artifact_missing", tmp_path / "nope", allow_synthetic=True)
    (copy / MODEL_FILE).unlink()
    expect("artifact_missing", copy, allow_synthetic=True)


def test_corrupted_model_file_is_detected_by_checksum(copy):
    data = bytearray((copy / MODEL_FILE).read_bytes())
    data[len(data) // 2] ^= 0xFF
    (copy / MODEL_FILE).write_bytes(bytes(data))
    expect("artifact_invalid", copy, allow_synthetic=True)


def test_invalid_metadata(copy):
    (copy / METADATA_FILE).write_text("{not json")
    expect("artifact_invalid", copy, allow_synthetic=True)
    (copy / METADATA_FILE).write_text(json.dumps({"metadata_schema": 99}))
    expect("artifact_invalid", copy, allow_synthetic=True)


def test_feature_schema_and_library_version_mismatch(copy):
    edit_meta(copy, feature_schema_version="difficulty_features_v0")
    expect("feature_schema_mismatch", copy, allow_synthetic=True)


def test_library_version_mismatch(copy):
    meta = json.loads((copy / METADATA_FILE).read_text())
    meta["library_versions"]["scikit-learn"] = "0.20.0"
    (copy / METADATA_FILE).write_text(json.dumps(meta))
    expect("library_version_mismatch", copy, allow_synthetic=True)


def test_model_missing_a_component(copy):
    import joblib
    joblib.dump({"classes": ["a"]}, copy / MODEL_FILE)
    edit_meta(copy, model_sha256=sha256_file(copy / MODEL_FILE))
    expect("artifact_invalid", copy, allow_synthetic=True)


def test_unpicklable_model_file(copy):
    (copy / MODEL_FILE).write_bytes(b"this is not a pickle")
    edit_meta(copy, model_sha256=sha256_file(copy / MODEL_FILE))
    expect("artifact_invalid", copy, allow_synthetic=True)


def test_missing_ml_dependencies_are_reported_not_raised(copy, monkeypatch):
    monkeypatch.setitem(sys.modules, "joblib", None)  # makes `import joblib` raise ImportError
    expect("ml_dependencies_missing", copy, allow_synthetic=True)


# ---------------------------------------------------------------- service: configuration, caching, recovery


def test_service_not_configured():
    with pytest.raises(ModelUnavailable) as ei:
        DifficultyModelService(None).predictor()
    assert ei.value.reason == "not_configured"


def test_service_loads_once_and_keeps_working(copy):
    svc = DifficultyModelService(str(copy), allow_synthetic=True)
    first = svc.predictor()
    shutil.rmtree(copy)  # already loaded: no further file access needed
    assert svc.predictor() is first and first.predict(ISSUE).status == "predicted"


def test_service_caches_failures_briefly_then_recovers(copy):
    now = {"t": 1000.0}
    svc = DifficultyModelService(str(copy), allow_synthetic=True, clock=lambda: now["t"])
    backup = (copy / MODEL_FILE).read_bytes()
    (copy / MODEL_FILE).unlink()
    with pytest.raises(ModelUnavailable):
        svc.predictor()
    (copy / MODEL_FILE).write_bytes(backup)  # fixed on disk...
    now["t"] += 5
    with pytest.raises(ModelUnavailable):  # ...but the failure is remembered for 30 s (no reload storm)
        svc.predictor()
    now["t"] += 30
    assert svc.predictor().predict(ISSUE).status == "predicted"


# ---------------------------------------------------------------- the lightweight install must not need ML packages


def test_importing_the_backend_never_imports_ml_libraries():
    code = ("import sys; sys.path.insert(0, '.'); import app.main, app.ml, app.ml.difficulty; "
            "bad = [m for m in ('sklearn', 'numpy', 'scipy', 'joblib', 'pandas') if m in sys.modules]; "
            "assert not bad, bad; print('clean')")
    out = subprocess.run([sys.executable, "-I", "-c", code], cwd=BACKEND, capture_output=True, text=True)
    assert out.returncode == 0 and "clean" in out.stdout, out.stderr
