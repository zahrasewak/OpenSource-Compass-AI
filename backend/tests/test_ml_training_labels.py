"""Training with annotated labels: sources, low confidence, exploratory flag, loader refusal. SYNTHETIC text only."""

import json
import shutil
from dataclasses import replace

import pytest

pytestmark = pytest.mark.ml
pytest.importorskip("sklearn")

from app.ml import DifficultyModelService, DifficultyPredictor, IssueInput, ModelUnavailable  # noqa: E402
from app.ml.labels import derive_weak_label  # noqa: E402
from app.ml.registry import load_artifact  # noqa: E402
from compass_ml_training import dataset as ds  # noqa: E402
from compass_ml_training.cli import main as cli_main  # noqa: E402
from compass_ml_training.train import train  # noqa: E402
from tests.ml_synthetic import synthetic_records  # noqa: E402

FIXED = "2025-01-01T00:00:00+00:00"


def annotate(raw, kind="llm", every=1, low_every=0, skip_every=0, offset=0):
    """Annotations equal to the planted level, for every `every`-th record (synthetic labels are known by construction)."""
    out = {}
    for n, r in enumerate(sorted(raw, key=lambda r: r.record_id)):
        if n % every != offset:
            continue
        level = derive_weak_label(r.labels).difficulty
        if skip_every and n % skip_every == 0:
            out[r.record_id] = ds.Annotation(None, kind, f"{kind}_1", "high", "skipped", unrateable=True)
        else:
            conf = "low" if (low_every and n % low_every == 0) else "medium"
            out[r.record_id] = ds.Annotation(level, kind, f"{kind}_1", conf, "planted")
    return out


def write(tmp, raw, annotations):
    records, _ = ds.build_dataset(raw, annotations=annotations)
    path = tmp / "d.jsonl"
    ds.write_jsonl(path, records)
    return path, records


def test_training_on_ai_annotations_is_labelled_as_such(tmp_path):
    raw = synthetic_records()
    path, _ = write(tmp_path, raw, annotate(raw, "llm"))
    result = train(path, tmp_path / "m", seed=42, created_at=FIXED)
    meta, rep = result["metadata"], result["evaluation"]
    assert meta["label_source"] == "llm_annotation" and meta["rubric_versions"] == ["rubric_v1"]
    assert meta["label_sources_used"] == ["human_review", "llm_annotation", "weak_label_hint"]
    assert meta["evaluation_status"] == "synthetic_not_for_use"  # synthetic always wins: never claim more
    assert "AI-annotated (provisional)" in " ".join(rep["conclusions"])
    by = rep["test"]["by_label_source"]
    assert by["llm_annotation"]["n"] >= 20 and "macro_f1" in by["llm_annotation"]
    assert by["human_review"]["status"] == "insufficient" and by["weak_label_hint"]["status"] == "insufficient"


def test_low_confidence_and_unrateable_items_are_excluded_and_counted(tmp_path):
    raw = synthetic_records()
    path, records = write(tmp_path, raw, annotate(raw, "llm", low_every=10, skip_every=7))
    result = train(path, tmp_path / "m", seed=42, created_at=FIXED)
    ex = result["evaluation"]["excluded_from_training"]
    assert ex["low_confidence"] > 0 and ex["unrateable"] > 0 and ex["unlabelled"] == 0
    usable = ds.usable_labelled(records, ds.ALL_LABEL_SOURCES)
    assert result["metadata"]["data"]["n_labelled"] == len(usable) < len(records)
    assert all(r.label.confidence != "low" and not r.label.unrateable for r in usable)


def test_label_sources_can_be_restricted(tmp_path):
    raw = synthetic_records()
    path, records = write(tmp_path, raw, annotate(raw, "llm", every=2))  # half AI-annotated, half only weak
    llm_only = [r for r in records if r.label.source == "llm_annotation"]
    result = train(path, tmp_path / "m", seed=42, created_at=FIXED, label_sources=("llm_annotation",))
    assert result["metadata"]["data"]["n_labelled"] == len(llm_only) == 360
    assert result["metadata"]["label_source"] == "llm_annotation" and result["evaluation"]["label_sources_used"] == ["llm_annotation"]
    mixed = train(path, tmp_path / "m2", seed=42, created_at=FIXED)
    assert mixed["metadata"]["label_source"] == "mixed" and mixed["metadata"]["data"]["n_labelled"] == 720


def test_restricting_to_a_source_with_too_little_data_is_refused(tmp_path):
    raw = synthetic_records()
    path, _ = write(tmp_path, raw, annotate(raw, "llm", every=10))  # only 72 AI labels
    with pytest.raises(ds.InsufficientDataError):
        train(path, tmp_path / "m", label_sources=("llm_annotation",))
    assert not (tmp_path / "m").exists()


def test_human_labels_get_their_own_metrics(tmp_path):
    raw = synthetic_records()
    path, _ = write(tmp_path, raw, annotate(raw, "human"))
    result = train(path, tmp_path / "m", seed=42, created_at=FIXED)
    assert result["metadata"]["label_source"] == "human"
    assert result["evaluation"]["test"]["by_label_source"]["human_review"]["n"] >= 20


# ---------------------------------------------------------------- exploratory (below the sufficiency gate)


def small_dataset(tmp_path):
    raw = synthetic_records(n_repos=10, per_repo=8, seed=3)
    return write(tmp_path, raw, annotate(raw, "llm"))[0]


LOW = ds.Thresholds(min_total=40, min_per_class=5, min_repos=6)


def test_default_gate_refuses_small_data_but_lowered_thresholds_run_as_exploratory(tmp_path):
    path = small_dataset(tmp_path)
    with pytest.raises(ds.InsufficientDataError):
        train(path, tmp_path / "m")
    result = train(path, tmp_path / "m", scheme="three_class", thresholds=LOW, seed=1, created_at=FIXED)
    meta = result["metadata"]
    assert meta["exploratory"] is True and meta["evaluation_status"] == "synthetic_not_for_use"  # synthetic is checked first
    assert result["evaluation"]["test"]["model"]["n"] < 30  # tiny held-out set: the report says so via n


def test_exploratory_artifacts_are_refused_unless_explicitly_allowed(tmp_path):
    path = small_dataset(tmp_path)
    result = train(path, tmp_path / "m", scheme="three_class", thresholds=LOW, seed=1, created_at=FIXED)
    with pytest.raises(ModelUnavailable) as ei:
        load_artifact(result["model_dir"], allow_synthetic=True)
    assert ei.value.reason == "exploratory_artifact_refused"
    objects, meta = load_artifact(result["model_dir"], allow_synthetic=True, allow_exploratory=True)
    assert DifficultyPredictor(objects, meta).predict(IssueInput(title="Scheduler deadlock", body="x " * 30)).status == "predicted"
    svc = DifficultyModelService(result["model_dir"], allow_synthetic=True)
    with pytest.raises(ModelUnavailable) as ei2:
        svc.predictor()
    assert ei2.value.reason == "exploratory_artifact_refused"
    assert DifficultyModelService(result["model_dir"], allow_synthetic=True, allow_exploratory=True).predictor().model_id == meta["model_id"]


def test_cli_flags_exploratory_runs_and_sources(tmp_path, capsys):
    raw = synthetic_records(n_repos=10, per_repo=8, seed=3)
    path, _ = write(tmp_path, raw, annotate(raw, "llm"))
    code = cli_main(["train", "--dataset", str(path), "--out", str(tmp_path / "m"), "--scheme", "three_class", "--min-total", "40",
                     "--min-per-class", "5", "--min-repos", "6", "--label-sources", "llm_annotation"])
    out = capsys.readouterr().out
    assert code == 0 and "EXPLORATORY" in out and "will not be served by default" in out
    meta = json.loads(next((tmp_path / "m").iterdir(), None).joinpath("metadata.json").read_text())
    assert meta["exploratory"] is True and meta["label_sources_used"] == ["llm_annotation"]


def test_thresholds_at_or_above_defaults_are_not_exploratory(tmp_path):
    raw = synthetic_records()
    path, _ = write(tmp_path, raw, annotate(raw, "llm"))
    meta = train(path, tmp_path / "m", seed=42, created_at=FIXED, thresholds=replace(ds.Thresholds(), min_total=400))["metadata"]
    assert meta["exploratory"] is False
