"""Training + evaluation pipeline. Uses SYNTHETIC data to exercise the code: scores here prove the pipeline works,
NOT that difficulty prediction works on real issues (no real dataset has been used in this project)."""

import json
from dataclasses import replace

import pytest

pytestmark = pytest.mark.ml

pytest.importorskip("sklearn")

import numpy as np  # noqa: E402

from app.ml.labels import derive_weak_label  # noqa: E402
from app.ml.registry import load_artifact  # noqa: E402
from compass_ml_training import dataset as ds  # noqa: E402
from compass_ml_training.cli import main as cli_main  # noqa: E402
from compass_ml_training.train import C_GRID, evaluate_artifact, train  # noqa: E402
from tests.ml_synthetic import synthetic_records  # noqa: E402

FIXED = "2025-01-01T00:00:00+00:00"


def make_dataset(tmp_path, name="data", reviews=False, **gen):
    raw = synthetic_records(**gen)
    rv = {r.record_id: derive_weak_label(r.labels).difficulty for r in raw} if reviews else None
    records, _ = ds.build_dataset(raw, rv)
    path = tmp_path / f"{name}.jsonl"
    ds.write_jsonl(path, records)
    return path, records


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("trained")
    path, records = make_dataset(tmp)
    return train(path, tmp / "models", seed=42, created_at=FIXED), records, tmp


# ---------------------------------------------------------------- artifacts + metadata


def test_training_writes_all_artifacts_with_complete_metadata(trained):
    result, records, _ = trained
    from pathlib import Path
    d = Path(result["model_dir"])
    assert {p.name for p in d.iterdir()} == {"model.joblib", "metadata.json", "evaluation.json", "report.md"}
    m = json.loads((d / "metadata.json").read_text())
    assert m["model_id"].startswith("ml_difficulty_lr_five_class_") and m["model_id"] == d.name
    assert m["label_scheme"] == "five_class" and m["seed"] == 42 and m["calibrated"] is False
    assert m["feature_schema_version"] == "difficulty_features_v1"
    assert m["label_definitions"]["classes"]["1"] == "beginner" and "weak_label_hint" in m["label_definitions"]["sources"]
    assert m["label_source"] == "weak_labels" and m["trained_on_synthetic"] is True
    assert m["evaluation_status"] == "synthetic_not_for_use"
    assert m["data"]["n_labelled"] == len(records) and m["data"]["version"] == ds.data_version(records)
    assert set(m["data"]["class_distribution"]) == {"beginner", "easy", "intermediate", "advanced", "expert"}
    assert m["library_versions"]["scikit-learn"] and m["hyperparameters"]["C"] in C_GRID
    from app.ml.registry import sha256_file
    assert m["model_sha256"] == sha256_file(d / "model.joblib")
    assert "NOT calibrated" in (d / "report.md").read_text() or "NOT calibrated" in (d / "report.md").read_text().replace("**", "")


def test_evaluation_report_contents(trained):
    rep = trained[0]["evaluation"]
    t = rep["test"]
    assert set(t["model"]["per_class"]["easy"]) == {"precision", "recall", "f1", "support"}
    cm = t["model"]["confusion_matrix"]
    assert cm["labels"] == rep["classes"] and np.array(cm["matrix"]).sum() == t["model"]["n"]
    for key in ("baseline_majority_class", "baseline_stratified_random", "calibration",
                "model_minus_majority_macro_f1", "model_minus_stratified_macro_f1"):
        assert key in t
    assert t["calibration"]["calibrated"] is False and 0 <= t["calibration"]["expected_calibration_error"] <= 1
    assert len(t["model_minus_majority_macro_f1"]["ci95"]) == 2
    assert set(rep["hyperparameter_search_val_macro_f1"]) == {str(c) for c in C_GRID}
    assert t["human_reviewed_subset"]["status"] == "insufficient"  # no human labels in this dataset
    assert any("WEAK" in c for c in rep["conclusions"]) and any("heuristic_v1/v2" in c for c in rep["conclusions"])


def test_synthetic_run_learns_the_planted_signal_and_beats_baselines(trained):
    """Sanity of the learning code on separable data. Says nothing about real-world accuracy."""
    t = trained[0]["evaluation"]["test"]
    assert t["model"]["macro_f1"] > t["baseline_majority_class"]["macro_f1"] + 0.2
    assert t["model"]["macro_f1"] > t["baseline_stratified_random"]["macro_f1"] + 0.2
    assert t["model_minus_majority_macro_f1"]["a_better_with_95pct_confidence"] is True
    assert t["model_minus_stratified_macro_f1"]["a_better_with_95pct_confidence"] is True
    assert trained[0]["metadata"]["metrics_summary"]["beats_baselines_95ci"] is True


def test_evaluation_uses_unseen_repositories(trained):
    result, records, _ = trained
    rep = result["evaluation"]
    train_repos = set(result["metadata"]["data"]["train_repos"])
    split = ds.split_by_repo([r for r in records if r.label.difficulty], 42)
    test_repos = {r.repo for r in split["test"]}
    assert test_repos and not (test_repos & train_repos)
    assert rep["split"]["test"]["n_repos"] == len(test_repos)


# ---------------------------------------------------------------- reproducibility


def test_same_seed_gives_identical_models_and_reports(tmp_path):
    path, _ = make_dataset(tmp_path)
    a = train(path, tmp_path / "a", seed=7, created_at=FIXED)
    b = train(path, tmp_path / "b", seed=7, created_at=FIXED)
    assert a["evaluation"] == b["evaluation"]
    oa, _ = load_artifact(a["model_dir"], allow_synthetic=True)
    ob, _ = load_artifact(b["model_dir"], allow_synthetic=True)
    assert np.array_equal(oa["clf"].coef_, ob["clf"].coef_) and list(oa["classes"]) == list(ob["classes"])
    assert oa["tfidf"].vocabulary_ == ob["tfidf"].vocabulary_


def test_different_seed_changes_split_and_model_id(tmp_path):
    path, _ = make_dataset(tmp_path)
    a = train(path, tmp_path / "a", seed=1, created_at=FIXED)
    b = train(path, tmp_path / "b", seed=2, created_at=FIXED)
    assert a["metadata"]["model_id"] != b["metadata"]["model_id"]
    assert a["metadata"]["data"]["train_repos"] != b["metadata"]["data"]["train_repos"]


def test_existing_model_dir_is_not_overwritten(tmp_path):
    path, _ = make_dataset(tmp_path)
    train(path, tmp_path / "m", seed=3, created_at=FIXED)
    with pytest.raises(FileExistsError):
        train(path, tmp_path / "m", seed=3, created_at=FIXED)
    train(path, tmp_path / "m", seed=3, created_at=FIXED, overwrite=True)


# ---------------------------------------------------------------- leakage + insufficient data


def test_difficulty_words_in_text_are_not_a_shortcut(tmp_path):
    """The only information in these issues is the label phrase written into the text (e.g. 'maintainers say: hard').
    Scrubbing must remove it (from text AND from numeric features like body length), so the model must not beat
    chance. (It can still out-score the majority-class baseline: that baseline's macro-F1 is structurally tiny.)"""
    path, _ = make_dataset(tmp_path, informative=False, leak_labels_in_text=True)
    result = train(path, tmp_path / "m", seed=42, created_at=FIXED)
    vocab = " ".join(load_artifact(result["model_dir"], allow_synthetic=True)[0]["tfidf"].vocabulary_)
    for word in ("easy", "hard", "expert", "medium", "good first", "beginner"):
        assert word not in vocab
    test = result["evaluation"]["test"]
    assert test["model_minus_stratified_macro_f1"]["a_better_with_95pct_confidence"] is False
    assert result["metadata"]["metrics_summary"]["beats_baselines_95ci"] is False
    assert "does NOT beat both" in " ".join(result["evaluation"]["conclusions"])


def test_insufficient_data_is_refused_with_reasons_and_writes_nothing(tmp_path):
    path, _ = make_dataset(tmp_path, n_repos=3, per_repo=15)
    with pytest.raises(ds.InsufficientDataError) as ei:
        train(path, tmp_path / "models", seed=1)
    text = " ".join(ei.value.problems)
    assert "[five_class]" in text and "[three_class]" in text and "labelled issues" in text and "repositories" in text
    assert not (tmp_path / "models").exists()


def test_cli_exit_code_and_message_when_data_is_insufficient(tmp_path, capsys):
    path, _ = make_dataset(tmp_path, n_repos=3, per_repo=15)
    assert cli_main(["train", "--dataset", str(path), "--out", str(tmp_path / "m")]) == 2
    err = capsys.readouterr().err
    assert "NOT TRAINED" in err and "docs/ML.md" in err and not (tmp_path / "m").exists()


def test_auto_scheme_falls_back_to_three_classes_and_records_why(tmp_path):
    path, _ = make_dataset(tmp_path, probs=(0.3, 0.3, 0.2, 0.18, 0.02))  # too few 'expert'
    result = train(path, tmp_path / "m", seed=42, created_at=FIXED)
    assert result["metadata"]["label_scheme"] == "three_class"
    rejected = " ".join(result["evaluation"]["scheme_rejections"]["five_class"])
    assert "expert" in rejected
    assert result["evaluation"]["classes"] == ["easy", "hard", "medium"]


def test_explicit_scheme_is_not_silently_downgraded(tmp_path):
    path, _ = make_dataset(tmp_path, probs=(0.3, 0.3, 0.2, 0.18, 0.02))
    with pytest.raises(ds.InsufficientDataError):
        train(path, tmp_path / "m", scheme="five_class")


def test_thresholds_can_be_raised(tmp_path):
    path, _ = make_dataset(tmp_path)
    with pytest.raises(ds.InsufficientDataError):
        train(path, tmp_path / "m", thresholds=replace(ds.Thresholds(), min_total=5000))


# ---------------------------------------------------------------- human labels, evaluation on other data


def test_human_reviewed_labels_get_their_own_subset_metrics(tmp_path):
    path, _ = make_dataset(tmp_path, reviews=True)
    result = train(path, tmp_path / "m", seed=42, created_at=FIXED)
    assert result["metadata"]["label_source"] == "human"
    sub = result["evaluation"]["test"]["human_reviewed_subset"]
    assert sub["n"] >= 20 and "macro_f1" in sub


def test_evaluate_artifact_on_unseen_and_seen_repositories(tmp_path):
    path, _ = make_dataset(tmp_path)
    result = train(path, tmp_path / "m", seed=42, created_at=FIXED)
    new_path, _ = make_dataset(tmp_path, name="new", n_repos=5, per_repo=40, seed=99)
    for rec in (r for r in ds.read_jsonl(new_path)):
        pass
    renamed = [r.model_copy(update={"repo": "fresh/" + r.repo.split("/")[1], "record_id": ds.make_record_id("fresh/" + r.repo.split("/")[1], r.issue_number)})
               for r in ds.read_jsonl(new_path)]
    ds.write_jsonl(new_path, renamed)
    unseen = evaluate_artifact(result["model_dir"], new_path, allow_synthetic=True)
    assert unseen["repos_seen_in_training"] == [] and unseen["warning"] is None and unseen["metrics"]["n"] == 200
    seen = evaluate_artifact(result["model_dir"], path, allow_synthetic=True)
    assert seen["repos_seen_in_training"] and "optimistic" in seen["warning"]


def test_cli_build_then_train(tmp_path, capsys):
    from compass_ml_training import dataset as d
    raw = tmp_path / "raw.jsonl"
    d.write_jsonl(raw, synthetic_records())
    assert cli_main(["build", "--raw", str(raw), "--out-dir", str(tmp_path / "data")]) == 0
    man = json.loads((tmp_path / "data" / "manifest.json").read_text())
    assert man["summary"]["n_labelled"] == man["summary"]["n_records"] and "provisional" in " ".join(man["notes"])
    assert cli_main(["train", "--dataset", str(tmp_path / "data" / "dataset.jsonl"), "--out", str(tmp_path / "models")]) == 0
    assert "Trained ml_difficulty_lr_five_class" in capsys.readouterr().out


def test_fast_macro_f1_agrees_with_scikit_learn():
    from sklearn.metrics import f1_score
    from compass_ml_training.evaluate import fast_macro_f1
    rng = np.random.RandomState(0)
    for _ in range(25):
        t, p = rng.randint(0, 4, 60), rng.randint(0, 4, 60)
        assert fast_macro_f1(t, p, 4) == pytest.approx(f1_score(t, p, labels=[0, 1, 2, 3], average="macro", zero_division=0))
