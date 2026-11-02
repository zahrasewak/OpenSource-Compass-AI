"""Feature extraction, labelling and dataset building. No scikit-learn needed."""

import csv

import pytest
from pydantic import ValidationError

from app.ml.features import (
    FEATURE_SCHEMA_VERSION, MAX_BODY_CHARS, NUMERIC_FEATURES, contains_difficulty_phrase, dense_features,
    redact_personal, scrub_text, text_for_model,
)
from app.ml.labels import apply_labels, derive_weak_label
from app.ml.schema import IssueInput, IssueRecord, LabelInfo, scheme_names
from compass_ml_training import dataset as ds
from tests.ml_synthetic import synthetic_records


def rec(n=1, repo="o/r", title="Crash on save", body="It crashes " + "x" * 30, labels=(), **kw):
    return IssueRecord(record_id=ds.make_record_id(repo, n), repo=repo, issue_number=n, html_url="https://github.com/o/r/1",
                       title=title, body=body, labels=list(labels), **kw)


# ---------------------------------------------------------------- features + leakage rules


def test_scrub_removes_personal_data_and_code():
    out = scrub_text("Mail bob@example.com or @alice see https://x.io/a?b=1\n```\nsecret()\n```\nEND")
    assert "bob" not in out and "alice" not in out and "x.io" not in out and "secret" not in out
    assert "email" in out and "mention" in out and "url" in out and "codeblock" in out and out.endswith("end")


@pytest.mark.parametrize("phrase", ["Good First Issue", "good-first-issue", "good_first_issue", "beginner-friendly",
                                    "Easy", "HARD", "expert", "trivial", "low hanging fruit", "intermediate"])
def test_difficulty_phrases_are_scrubbed_from_text(phrase):
    out = text_for_model(IssueInput(title=f"Fix {phrase} bug", body=f"This is {phrase}. Also {phrase}!"))
    assert not contains_difficulty_phrase(out), out


def test_features_never_see_labels_or_outcomes():
    with pytest.raises(ValidationError):
        IssueInput(title="x", labels=["hard"])  # labels cannot be passed in
    with pytest.raises(ValidationError):
        IssueInput(title="x", comments=5)
    assert not {"labels", "state", "comments", "assignees", "pull", "closed", "repo"} & {k for k in dense_features(IssueInput(title="x"))}


def test_dense_features_values_and_schema():
    body = "Steps:\n1. open\n2. click\n- [ ] todo\n```\ncode\n```\nsee https://a.b and https://c.d\nTraceback (most recent call last)"
    f = dense_features(IssueInput(title="Crash", body=body, author_association="MEMBER", repo_language="Node.js"))
    assert set(NUMERIC_FEATURES) <= set(f) and f["lang"] == "node"
    assert f["n_code_blocks"] == pytest.approx(0.6931, abs=1e-3)  # log1p(1)
    assert f["n_urls"] == pytest.approx(1.0986, abs=1e-3)  # log1p(2)
    assert f["n_checkboxes"] > 0 and f["n_numbered_steps"] > 0 and f["has_traceback"] == 1.0 and f["author_is_maintainer"] == 1.0
    assert FEATURE_SCHEMA_VERSION == "difficulty_features_v1"


def test_missing_and_oversized_inputs():
    f = dense_features(IssueInput(title="t"))
    assert f["body_chars"] == 0 and "lang" not in f and f["has_traceback"] == 0.0
    assert text_for_model(IssueInput(title="", body=None)).strip(" .") == ""
    huge = IssueInput(title="t", body="word " * 100_000)
    assert len(scrub_text(huge.body)) <= MAX_BODY_CHARS and dense_features(huge)["body_chars"] <= 9  # log1p(5000)=8.5


def test_redact_personal_keeps_text_readable():
    assert redact_personal("ping @bob at bob@x.org now") == "ping @user at user@example.invalid now"
    assert redact_personal(None) == ""


# ---------------------------------------------------------------- weak labels


@pytest.mark.parametrize("labels,level,text", [
    (["good first issue"], 1, "good first issue"), (["Difficulty: Easy", "bug"], 2, "Difficulty: Easy"),
    (["hard", "good first issue"], 4, "hard"), (["bug", "help wanted"], None, None), ([], None, None),
])
def test_weak_label_derivation(labels, level, text):
    info = derive_weak_label(labels)
    assert info.difficulty == level and info.label_text == text
    assert info.source == ("weak_label_hint" if level else "none")


def test_apply_labels_never_overwrites_a_human_label():
    human = rec(labels=["good first issue"], label=LabelInfo(difficulty=5, source="human_review"))
    assert apply_labels(human).label.difficulty == 5 and apply_labels(human).label.source == "human_review"
    assert apply_labels(rec(labels=["easy"])).label.source == "weak_label_hint"


# ---------------------------------------------------------------- dataset building


def test_build_dataset_dedupes_labels_and_reports():
    a = rec(1, title="Same text", body="same body here " * 3, labels=["easy"])
    dup = rec(2, repo="x/y", title="Same text", body="same body here " * 3, labels=["hard"])  # template copy elsewhere
    empty = rec(3, title="   ")
    b = rec(4, title="Other", body="different " * 5, labels=[])
    reviewed = rec(5, title="Reviewed", body="reviewed " * 5, labels=["easy"])
    records, report = ds.build_dataset([a, dup, empty, b, reviewed], reviews={reviewed.record_id: 5, "ghost": 3})
    assert report["dropped"] == {"duplicate_text": 1, "empty_title": 1} and report["unknown_review_ids"] == ["ghost"]
    by_id = {r.record_id: r for r in records}
    assert len(records) == 3
    kept_dup = a if a.record_id in by_id else dup  # of two identical texts the smaller record_id is kept (deterministic)
    assert kept_dup.record_id == min(a.record_id, dup.record_id) and (a.record_id in by_id) != (dup.record_id in by_id)
    assert by_id[kept_dup.record_id].label.source == "weak_label_hint"
    assert by_id[b.record_id].label.difficulty is None and by_id[b.record_id].label.source == "none"
    assert by_id[reviewed.record_id].label == LabelInfo(difficulty=5, source="human_review")  # human beats weak


def test_data_version_is_stable_and_label_sensitive():
    records = ds.build_dataset(synthetic_records(3, 20))[0]
    assert ds.data_version(records) == ds.data_version(list(reversed(records)))
    changed = [records[0].model_copy(update={"label": LabelInfo(difficulty=3, source="human_review")}), *records[1:]]
    assert ds.data_version(changed) != ds.data_version(records)


def test_jsonl_roundtrip(tmp_path):
    records = ds.build_dataset(synthetic_records(2, 10))[0]
    ds.write_jsonl(tmp_path / "d.jsonl", records)
    assert ds.read_jsonl(tmp_path / "d.jsonl") == sorted(records, key=lambda r: r.record_id)
    (tmp_path / "bad.jsonl").write_text('{"nope": 1}\n')
    with pytest.raises(ValueError, match="invalid record"):
        ds.read_jsonl(tmp_path / "bad.jsonl")


# ---------------------------------------------------------------- sufficiency + split


def test_sufficiency_reports_every_shortfall():
    small = ds.build_dataset(synthetic_records(2, 20))[0]
    problems = ds.check_sufficiency(small, "five_class", ds.Thresholds())
    text = " ".join(problems)
    assert "only 40 labelled issues" in text and "only 2 repositories" in text and "class 'expert'" in text
    assert ds.check_sufficiency(ds.build_dataset(synthetic_records())[0], "five_class", ds.Thresholds()) == []
    assert scheme_names("three_class") == ["easy", "medium", "hard"]


def test_split_is_repository_disjoint_and_deterministic():
    records = ds.build_dataset(synthetic_records())[0]
    s1, s2 = ds.split_by_repo(records, 7), ds.split_by_repo(records, 7)
    assert {k: [r.record_id for r in v] for k, v in s1.items()} == {k: [r.record_id for r in v] for k, v in s2.items()}
    repos = {k: {r.repo for r in v} for k, v in s1.items()}
    assert not (repos["train"] & repos["val"] or repos["train"] & repos["test"] or repos["val"] & repos["test"])
    assert sum(len(v) for v in s1.values()) == len(records) and all(s1[k] for k in s1)
    other = ds.split_by_repo(records, 8)
    assert {r.repo for r in other["test"]} != repos["test"] or {r.repo for r in other["val"]} != repos["val"]


# ---------------------------------------------------------------- human review workflow


def test_review_csv_roundtrip(tmp_path):
    records = ds.build_dataset(synthetic_records(2, 10))[0]
    path = tmp_path / "review.csv"
    assert ds.export_review_sample(records, 5, seed=1, path=path) == 5
    rows = list(csv.reader(open(path, encoding="utf-8")))
    col = rows[0].index("difficulty_1_to_5")
    assert len(rows) == 6
    rows[1][col], rows[2][col] = "3", "5"
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)
    assert ds.import_reviews(path) == {rows[1][0]: 3, rows[2][0]: 5}  # blanks skipped
    rows[3][col] = "7"
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)
    with pytest.raises(ValueError, match="invalid difficulty"):
        ds.import_reviews(path)
