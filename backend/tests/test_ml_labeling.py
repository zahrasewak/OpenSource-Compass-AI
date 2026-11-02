"""Labeling workflow: annotation import, provenance, precedence, pilot sampling, agreement. No scikit-learn needed."""

import csv
import json
from pathlib import Path

import pytest

from app.ml.schema import RUBRIC_VERSION, IssueRecord, LabelInfo
from compass_ml_training import agreement as ag
from compass_ml_training import dataset as ds
from compass_ml_training.cli import main as cli_main
from tests.ml_synthetic import synthetic_records

ROOT = Path(__file__).resolve().parents[2]


def rec(n=1, repo="o/r", labels=(), title=None, body=None, **kw):
    return IssueRecord(record_id=ds.make_record_id(repo, n), repo=repo, issue_number=n, html_url=f"https://github.com/{repo}/issues/{n}",
                       title=title or f"Title {repo} {n}", body=body or f"Body text number {n} " * 5, labels=list(labels), **kw)


def write_csv(path, rows, header=("record_id", "difficulty_1_to_5", "confidence", "annotator", "notes")):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


# ---------------------------------------------------------------- annotation import validation


def test_import_valid_annotations(tmp_path):
    path = write_csv(tmp_path / "a.csv", [
        ["r1", "3", "high", "llm_1", "needs internals"], ["r2", "SKIP", "", "llm_1", "spam"], ["r3", "", "", "", ""],
        ["r4", "1", "", "llm_1", "x" * 500]])
    out = ds.import_annotations(path, "llm")
    assert set(out) == {"r1", "r2", "r4"}  # blank rows are "not yet annotated"
    assert out["r1"] == ds.Annotation(3, "llm", "llm_1", "high", "needs internals")
    assert out["r2"].unrateable and out["r2"].difficulty is None and out["r2"].confidence == "medium"  # default confidence
    assert len(out["r4"].notes) == 300


@pytest.mark.parametrize("row,message", [
    (["r1", "6", "high", "a", ""], "must be 1-5"), (["r1", "0", "high", "a", ""], "must be 1-5"),
    (["r1", "hard", "high", "a", ""], "must be 1-5"), (["r1", "3", "very", "a", ""], "confidence must be"),
    (["r1", "3", "high", "", ""], "annotator is required"), (["", "3", "high", "a", ""], "missing record_id"),
])
def test_invalid_annotations_are_rejected_not_repaired(tmp_path, row, message):
    with pytest.raises(ValueError, match=message):
        ds.import_annotations(write_csv(tmp_path / "a.csv", [row]), "human")


def test_duplicate_and_bad_kind(tmp_path):
    path = write_csv(tmp_path / "a.csv", [["r1", "2", "high", "a", ""], ["r1", "3", "high", "a", ""]])
    with pytest.raises(ValueError, match="annotated twice"):
        ds.import_annotations(path, "human")
    with pytest.raises(ValueError, match="kind"):
        ds.import_annotations(path, "robot")


# ---------------------------------------------------------------- provenance + precedence in the built dataset


def test_label_precedence_human_over_llm_over_weak():
    a, b, c, d = (rec(i, labels=["easy"]) for i in range(1, 5))  # all carry a weak hint (level 2)
    ann = {a.record_id: ds.Annotation(5, "human", "human_1", "high", "deep"),
           b.record_id: ds.Annotation(3, "llm", "llm_1", "medium", "mid")}
    records, report = ds.build_dataset([a, b, c, d], annotations=ann)
    by = {r.record_id: r.label for r in records}
    assert (by[a.record_id].source, by[a.record_id].difficulty, by[a.record_id].annotator) == ("human_review", 5, "human_1")
    assert (by[b.record_id].source, by[b.record_id].difficulty) == ("llm_annotation", 3)
    assert (by[c.record_id].source, by[c.record_id].difficulty) == ("weak_label_hint", 2)
    assert by[a.record_id].rubric_version == RUBRIC_VERSION and by[a.record_id].notes == "deep"
    assert report["unknown_review_ids"] == []


def test_unrateable_issues_are_recorded_not_lost_or_guessed():
    r = rec(1, labels=["good first issue"])  # a maintainer label must NOT turn a skipped issue back into a level
    records, _ = ds.build_dataset([r], annotations={r.record_id: ds.Annotation(None, "human", "h1", "high", "spam", unrateable=True)})
    label = records[0].label
    assert label.difficulty is None and label.unrateable and label.source == "human_review" and label.notes == "spam"
    assert ds.describe(records)["unrateable"] == 1 and ds.describe(records)["n_labelled"] == 0


def test_unknown_annotation_ids_are_reported():
    _, report = ds.build_dataset([rec(1)], annotations={"ghost": ds.Annotation(2, "llm", "a")})
    assert report["unknown_review_ids"] == ["ghost"]


def test_low_confidence_unrateable_and_wrong_source_are_not_trainable():
    base = [rec(i, labels=["bug"]) for i in range(1, 7)]
    ann = {base[0].record_id: ds.Annotation(2, "llm", "l", "high"), base[1].record_id: ds.Annotation(3, "llm", "l", "low"),
           base[2].record_id: ds.Annotation(None, "llm", "l", "high", "q", unrateable=True),
           base[3].record_id: ds.Annotation(4, "human", "h", "medium")}
    records, _ = ds.build_dataset(base, annotations=ann)
    usable = ds.usable_labelled(records, ds.ALL_LABEL_SOURCES)
    assert {r.record_id for r in usable} == {base[0].record_id, base[3].record_id}
    only_human = ds.usable_labelled(records, ("human_review",))
    assert [r.record_id for r in only_human] == [base[3].record_id]
    assert ds.describe(records)["low_confidence_labels"] == 1


def test_maintainer_labels_alone_never_become_verified_labels():
    records, _ = ds.build_dataset([rec(1, labels=["good first issue"])])
    assert records[0].label.source == "weak_label_hint" and records[0].label.difficulty == 1
    assert ds.usable_labelled(records, ("human_review", "llm_annotation")) == []


# ---------------------------------------------------------------- pilot sampling + annotation sheet


def pool():
    return [rec(i, repo=f"o{r}/p{r}", labels=["good first issue"] if i % 3 == 0 else []) for r in range(8) for i in range(1, 21)]


def test_pilot_sample_is_deterministic_and_repository_stratified():
    records = pool()
    a, b = ds.pilot_sample(records, 40, seed=1), ds.pilot_sample(records, 40, seed=1)
    assert [r.record_id for r in a] == [r.record_id for r in b] and len({r.record_id for r in a}) == 40
    counts = {}
    for r in a:
        counts[r.repo] = counts.get(r.repo, 0) + 1
    assert set(counts) == {f"o{i}/p{i}" for i in range(8)} and max(counts.values()) - min(counts.values()) <= 1  # no project dominates
    assert [r.record_id for r in ds.pilot_sample(records, 40, seed=2)] != [r.record_id for r in a]


def test_pilot_sample_skips_excluded_and_human_reviewed():
    records = pool()
    reviewed = records[0].model_copy(update={"label": LabelInfo(difficulty=2, source="human_review")})
    sample = ds.pilot_sample([reviewed, *records[1:]], 1000, seed=1, exclude={records[1].record_id})
    ids = {r.record_id for r in sample}
    assert records[0].record_id not in ids and records[1].record_id not in ids and len(sample) == len(records) - 2


def test_annotation_sheet_hides_maintainer_labels_and_honours_excerpt_length(tmp_path):
    records = [rec(i, labels=["good first issue", "hard"], body="word " * 800) for i in range(1, 6)]
    path = tmp_path / "sheet.csv"
    assert ds.export_review_sample(records, 5, seed=1, path=path, excerpt_chars=100) == 5
    text = path.read_text(encoding="utf-8")
    assert "good first issue" not in text and "hard" not in text.lower().replace("difficulty", "")  # annotators are not anchored
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert list(rows[0]) == ds.ANNOTATION_COLUMNS and all(len(r["body_excerpt"]) <= 100 for r in rows)
    assert all(r["difficulty_1_to_5"] == "" and r["annotator"] == "" for r in rows)  # a blank sheet: nothing pre-filled
    assert ds.DEFAULT_EXCERPT_CHARS == 1500


# ---------------------------------------------------------------- agreement statistics (hand-computed expectations)


def test_kappa_matches_hand_calculation():
    a, b = {"1": 1, "2": 1, "3": 2, "4": 2}, {"1": 1, "2": 2, "3": 2, "4": 2}
    r = ag.agreement(a, b)
    # observed 3/4; chance = (2*1 + 2*3)/16 = 0.5; kappa = (0.75-0.5)/(1-0.5) = 0.5. Quadratic weights give 0.5 here too.
    assert r["exact_agreement"] == 0.75 and r["cohen_kappa"] == 0.5 and r["quadratic_weighted_kappa"] == 0.5
    assert r["within_one_level"] == 1.0 and r["status"] == "small_sample" and r["n_common"] == 4
    assert r["confusion_matrix"]["matrix"][0][:2] == [1, 1]


def test_perfect_chance_and_degenerate_agreement():
    ids = [str(i) for i in range(40)]
    same = {i: 1 + n % 5 for n, i in enumerate(ids)}
    assert ag.agreement(same, dict(same))["cohen_kappa"] == 1.0 and ag.agreement(same, dict(same))["status"] == "ok"
    shifted = {i: 1 + (n + 2) % 5 for n, i in enumerate(ids)}  # systematically different
    assert ag.agreement(same, shifted)["cohen_kappa"] < 0.1
    one_level = {i: 3 for i in ids}
    r = ag.agreement(one_level, dict(one_level))
    assert r["cohen_kappa"] is None and "undefined" in r["interpretation"]  # kappa is meaningless with a single level
    assert ag.agreement({"a": 1}, {"b": 1}) == {"n_common": 0, "n_a": 1, "n_b": 1, "status": "no_overlap"}


def test_large_disagreements_are_listed_for_adjudication():
    r = ag.agreement({"x": 1, "y": 3, "z": 5}, {"x": 4, "y": 3, "z": 4})
    assert r["large_disagreements"] == [{"record_id": "x", "a": 1, "b": 4}]


def test_weak_labels_vs_annotations():
    good = rec(1, labels=["easy"]).model_copy(update={"label": LabelInfo(difficulty=2, source="llm_annotation")})
    off = rec(2, labels=["good first issue"]).model_copy(update={"label": LabelInfo(difficulty=4, source="human_review")})
    no_hint = rec(3, labels=["bug"]).model_copy(update={"label": LabelInfo(difficulty=3, source="llm_annotation")})
    skipped = rec(4, labels=["easy"]).model_copy(update={"label": LabelInfo(source="llm_annotation", unrateable=True)})
    out = ag.weak_vs_annotation([good, off, no_hint, skipped])
    assert out["n_common"] == 2 and out["n_annotated_total"] == 3 and out["exact_agreement"] == 0.5
    assert "says nothing about the many issues without one" in out["note"]
    assert ag.weak_vs_annotation([rec(1)])["status"] == "no_overlap"


# ---------------------------------------------------------------- CLI: provenance is explicit, clashes are refused


def test_cli_build_with_annotations_agreement_and_clash(tmp_path, capsys):
    raw = tmp_path / "raw.jsonl"
    records = synthetic_records(2, 10)
    ds.write_jsonl(raw, records)
    ids = sorted(r.record_id for r in records)
    llm = write_csv(tmp_path / "llm.csv", [[ids[0], "3", "high", "llm_1", "n"], [ids[1], "skip", "", "llm_1", "spam"]])
    human = write_csv(tmp_path / "human.csv", [[ids[0], "4", "high", "human_1", "n"]])
    assert cli_main(["build", "--raw", str(raw), "--annotations", f"{llm}:llm", "--out-dir", str(tmp_path / "d")]) == 0
    man = json.loads((tmp_path / "d" / "manifest.json").read_text())
    assert man["annotation_files"][0]["kind"] == "llm" and man["annotation_files"][0]["n"] == 2 and len(man["annotation_files"][0]["sha256"]) == 64
    built = {r.record_id: r for r in ds.read_jsonl(tmp_path / "d" / "dataset.jsonl")}
    assert built[ids[0]].label.source == "llm_annotation" and built[ids[1]].label.unrateable
    capsys.readouterr()

    assert cli_main(["build", "--raw", str(raw), "--annotations", f"{llm}:llm", "--annotations", f"{human}:human",
                     "--out-dir", str(tmp_path / "d2")]) == 1
    assert "already annotated" in capsys.readouterr().err  # two annotators on one record must be adjudicated, not overwritten
    assert cli_main(["build", "--raw", str(raw), "--annotations", str(llm), "--out-dir", str(tmp_path / "d3")]) == 1
    capsys.readouterr()
    assert cli_main(["agreement", "--a", f"{llm}:llm", "--b", f"{human}:human"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["n_common"] == 1 and report["status"] == "small_sample" and report["within_one_level"] == 1.0


def test_cli_review_sample_and_weak_check(tmp_path, capsys):
    raw = tmp_path / "raw.jsonl"
    ds.write_jsonl(raw, synthetic_records(3, 12))
    assert cli_main(["build", "--raw", str(raw), "--out-dir", str(tmp_path / "d")]) == 0
    assert cli_main(["review-sample", "--dataset", str(tmp_path / "d" / "dataset.jsonl"), "--n", "10", "--out", str(tmp_path / "s.csv"),
                     "--excerpt-chars", "50"]) == 0
    assert len(list(csv.DictReader(open(tmp_path / "s.csv", encoding="utf-8")))) == 10
    capsys.readouterr()
    assert cli_main(["weak-check", "--dataset", str(tmp_path / "d" / "dataset.jsonl")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "no_overlap"  # nobody has annotated anything yet


# ---------------------------------------------------------------- the committed pilot annotations are well-formed


def test_pilot_annotation_record_is_consistent_and_importable():
    import pilot_annotate_llm as pilot

    assert len(pilot.LABELS) == 80
    for position, (level, conf, why) in pilot.LABELS.items():
        assert level in {1, 2, 3, 4, 5, "skip"} and conf in {"low", "medium", "high"} and 0 < len(why) <= 300, position
    committed = ROOT / "ml-training" / "annotations" / "pilot_llm_rubric_v1.csv"
    ann = ds.import_annotations(committed, "llm")
    assert len(ann) == 80 and {a.annotator for a in ann.values()} == {pilot.ANNOTATOR}
    assert sum(a.unrateable for a in ann.values()) == sum(1 for v in pilot.LABELS.values() if v[0] == "skip")
    assert sorted(a.difficulty for a in ann.values() if a.difficulty) == sorted(v[0] for v in pilot.LABELS.values() if v[0] != "skip")
    text = committed.read_text(encoding="utf-8")
    assert "http" not in text and "@" not in text  # ids and short rationales only: no third-party issue text
