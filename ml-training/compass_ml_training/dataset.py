"""Dataset building: validate, scrub, deduplicate, label, version, check sufficiency, split by repository.

Nothing here invents data. If the data cannot support a model, `check_sufficiency` says so and training stops.
"""

import csv
import hashlib
import json
import random
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.ml.features import scrub_text
from app.ml.labels import apply_labels, class_for
from app.ml.schema import RECORD_SCHEMA_VERSION, RUBRIC_VERSION, SCHEMES, IssueRecord, LabelInfo, scheme_names


class InsufficientDataError(Exception):
    """The dataset cannot support the requested model; `problems` lists exactly why."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("insufficient data: " + "; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Thresholds:
    """Minimum evidence before a model is trained. Deliberately conservative; override only knowingly."""

    min_total: int = 300  # labelled issues
    min_per_class: int = 30
    min_repos: int = 8  # repositories with labelled issues (needed for a repository-disjoint split)
    min_test_classes: int = 2


def make_record_id(repo: str, number: int) -> str:
    return hashlib.sha256(f"{repo.lower()}#{number}".encode()).hexdigest()[:16]


# ------------------------------------------------------------------ IO


def read_jsonl(path: str | Path) -> list[IssueRecord]:
    records = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                try:
                    records.append(IssueRecord.model_validate_json(line))
                except ValueError as exc:
                    raise ValueError(f"{path}:{n}: invalid record ({exc.__class__.__name__})") from exc
    return records


def write_jsonl(path: str | Path, records: list[IssueRecord]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in sorted(records, key=lambda r: r.record_id):
            f.write(r.model_dump_json() + "\n")


# ------------------------------------------------------------------ cleaning + labelling


def _fingerprint(r: IssueRecord) -> str:
    return hashlib.sha256((scrub_text(r.title) + "|" + scrub_text(r.body)).encode()).hexdigest()


@dataclass(frozen=True)
class Annotation:
    """One person's (or model's) judgement of one issue. `difficulty is None` with unrateable=True means
    'this is not an actionable task' (question, support request, discussion...), recorded on purpose."""

    difficulty: int | None
    kind: str  # "human" | "llm"
    annotator: str
    confidence: str = "medium"
    notes: str = ""
    unrateable: bool = False


def build_dataset(records: list[IssueRecord], reviews: dict[str, int] | None = None,
                  annotations: dict[str, Annotation] | None = None) -> tuple[list[IssueRecord], dict]:
    """Clean, deduplicate and label raw records. Returns (records, report).

    * records without a title are dropped; exact duplicates (same scrubbed title+body, e.g. issue templates
      copied across repositories) are dropped so a copy cannot sit in both train and test;
    * label precedence: human annotation/review > llm annotation > weak (maintainer-label) hint;
    * an annotation never silently disappears: unrateable issues keep `label.unrateable=True`.
    """
    reviews = reviews or {}
    annotations = annotations or {}
    dropped = Counter()
    seen: dict[str, str] = {}
    kept: list[IssueRecord] = []
    for r in sorted(records, key=lambda r: r.record_id):
        if not r.title.strip():
            dropped["empty_title"] += 1
            continue
        fp = _fingerprint(r)
        if fp in seen:
            dropped["duplicate_text"] += 1
            continue
        seen[fp] = r.record_id
        ann = annotations.get(r.record_id)
        if ann is not None:
            source = "human_review" if ann.kind == "human" else "llm_annotation"
            r = r.model_copy(update={"label": LabelInfo(
                difficulty=ann.difficulty, source=source if (ann.difficulty or ann.unrateable) else "none",
                confidence=ann.confidence, annotator=ann.annotator, rubric_version=RUBRIC_VERSION,
                notes=(ann.notes or None), unrateable=ann.unrateable,
                label_text=r.label.label_text)})
        elif r.record_id in reviews:
            r = r.model_copy(update={"label": LabelInfo(difficulty=reviews[r.record_id], source="human_review")})
        kept.append(r if (ann is not None and ann.unrateable) else apply_labels(r))
    ids = {r.record_id for r in kept}
    report = {"input_records": len(records), "kept": len(kept), "dropped": dict(dropped),
              "unknown_review_ids": sorted((set(reviews) | set(annotations)) - ids)}
    return kept, report


def data_version(records: list[IssueRecord]) -> str:
    """Stable content hash of ids + labels (changes when the data or its labels change)."""
    h = hashlib.sha256()
    for r in sorted(records, key=lambda r: r.record_id):
        h.update(f"{r.record_id}:{r.label.difficulty}:{r.label.source};".encode())
    return h.hexdigest()


def describe(records: list[IssueRecord], scheme: str | None = None) -> dict:
    labelled = [r for r in records if r.label.difficulty is not None]
    out = {
        "n_records": len(records), "n_labelled": len(labelled), "n_unlabelled": len(records) - len(labelled),
        "n_repos": len({r.repo for r in records}), "n_repos_labelled": len({r.repo for r in labelled}),
        "label_sources": dict(Counter(r.label.source for r in records)),
        "record_sources": dict(Counter(r.source for r in records)),
        "difficulty_levels": {str(k): v for k, v in sorted(Counter(r.label.difficulty for r in labelled).items())},
        "low_confidence_labels": sum(1 for r in labelled if r.label.confidence == "low"),
        "unrateable": sum(1 for r in records if r.label.unrateable),
        "rubric_versions": dict(Counter(r.label.rubric_version for r in records if r.label.rubric_version)),
    }
    if scheme:
        out["class_distribution"] = dict(Counter(class_for(r.label.difficulty, scheme) for r in labelled))
    return out


def manifest(records: list[IssueRecord], build_report: dict, created_at: str, notes: list[str]) -> dict:
    return {"manifest_schema": 1, "record_schema_version": RECORD_SCHEMA_VERSION, "data_version": data_version(records),
            "created_at": created_at, "build": build_report, "summary": describe(records), "notes": notes}


ALL_LABEL_SOURCES = ("human_review", "llm_annotation", "weak_label_hint")


def usable_labelled(records: list[IssueRecord], label_sources) -> list[IssueRecord]:
    """Records that may be trained/evaluated on: a difficulty, from an allowed source, rated (not 'skip'), and not
    marked low-confidence by its annotator. Everything excluded is still counted in the dataset description."""
    return [r for r in records if r.label.difficulty is not None and not r.label.unrateable
            and r.label.source in label_sources and r.label.confidence != "low"]


# ------------------------------------------------------------------ sufficiency gate


def check_sufficiency(labelled: list[IssueRecord], scheme: str, t: Thresholds) -> list[str]:
    """Empty list = enough data to train. Otherwise the concrete shortfalls."""
    problems = []
    counts = Counter(class_for(r.label.difficulty, scheme) for r in labelled)  # type: ignore[arg-type]
    if len(labelled) < t.min_total:
        problems.append(f"only {len(labelled)} labelled issues (need >= {t.min_total})")
    repos = len({r.repo for r in labelled})
    if repos < t.min_repos:
        problems.append(f"only {repos} repositories with labelled issues (need >= {t.min_repos})")
    for name in scheme_names(scheme):
        if counts.get(name, 0) < t.min_per_class:
            problems.append(f"class '{name}' has {counts.get(name, 0)} examples (need >= {t.min_per_class})")
    return problems


# ------------------------------------------------------------------ repository-disjoint split


def split_by_repo(records: list[IssueRecord], seed: int, val_frac: float = 0.15, test_frac: float = 0.20
                  ) -> dict[str, list[IssueRecord]]:
    """Assign whole repositories to train/val/test so no project appears in two splits (tests generalisation
    to unseen repositories). Deterministic for a given seed and record set."""
    by_repo: dict[str, list[IssueRecord]] = {}
    for r in sorted(records, key=lambda r: r.record_id):
        by_repo.setdefault(r.repo, []).append(r)
    repos = sorted(by_repo)
    random.Random(seed).shuffle(repos)
    total = len(records)
    out: dict[str, list[IssueRecord]] = {"train": [], "val": [], "test": []}
    for repo in repos:
        if len(out["test"]) < test_frac * total:
            out["test"].extend(by_repo[repo])
        elif len(out["val"]) < val_frac * total:
            out["val"].extend(by_repo[repo])
        else:
            out["train"].extend(by_repo[repo])
    return out


# ------------------------------------------------------------------ human review workflow


ANNOTATION_COLUMNS = ["record_id", "repo", "html_url", "title", "body_excerpt", "difficulty_1_to_5", "confidence",
                      "annotator", "notes"]


def pilot_sample(records: list[IssueRecord], n: int, seed: int, exclude: set[str] | None = None) -> list[IssueRecord]:
    """Repository-stratified sample (round robin over shuffled repositories) so no project dominates and the
    sample mixes issues with and without maintainer difficulty labels. Deterministic for a given seed."""
    exclude = exclude or set()
    rng = random.Random(seed)
    by_repo: dict[str, list[IssueRecord]] = {}
    for r in sorted(records, key=lambda r: r.record_id):
        if r.record_id not in exclude and r.label.source != "human_review":
            by_repo.setdefault(r.repo, []).append(r)
    for items in by_repo.values():
        rng.shuffle(items)
    repos = sorted(by_repo)
    rng.shuffle(repos)
    out: list[IssueRecord] = []
    while len(out) < n and any(by_repo.values()):
        for repo in repos:
            if by_repo[repo] and len(out) < n:
                out.append(by_repo[repo].pop())
    return out


DEFAULT_EXCERPT_CHARS = 1500  # the 600-character pilot excerpt left 21% of labels low-confidence (docs/LABELING_RUBRIC.md)


def export_review_sample(records: list[IssueRecord], n: int, seed: int, path: str | Path,
                         exclude: set[str] | None = None, excerpt_chars: int = DEFAULT_EXCERPT_CHARS) -> int:
    """Write a CSV of issues for annotation (difficulty 1-5, or 'skip' for non-tasks). The maintainer's own
    difficulty labels are deliberately NOT shown, so annotators are not anchored by them. Returns rows written."""
    rows = pilot_sample(records, n, seed, exclude)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(ANNOTATION_COLUMNS)
        for r in rows:
            w.writerow([r.record_id, r.repo, r.html_url, r.title, re.sub(r"\s+", " ", (r.body or "")[:excerpt_chars]), "", "", "", ""])
    return len(rows)


def import_reviews(path: str | Path) -> dict[str, int]:
    """Read a completed review CSV (levels only). Blank rows are skipped; anything outside 1-5 is an error."""
    out: dict[str, int] = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            raw = (row.get("difficulty_1_to_5") or "").strip()
            if not raw:
                continue
            if raw not in {"1", "2", "3", "4", "5"}:
                raise ValueError(f"invalid difficulty {raw!r} for record {row.get('record_id')}")
            out[row["record_id"]] = int(raw)
    return out


def import_annotations(path: str | Path, kind: str) -> dict[str, Annotation]:
    """Read an annotation CSV. `kind` is 'human' or 'llm' and decides the label provenance.

    Per row: difficulty_1_to_5 is 1-5, or 'skip' (not an actionable task, recorded as unrateable), or blank
    (not yet annotated, ignored). confidence is low|medium|high (blank = medium). annotator is REQUIRED
    on annotated rows so every label can be traced; it must not contain a real name or GitHub handle.
    Anything invalid raises: labels are never guessed or repaired.
    """
    if kind not in {"human", "llm"}:
        raise ValueError("kind must be 'human' or 'llm'")
    out: dict[str, Annotation] = {}
    with open(path, encoding="utf-8", newline="") as f:
        for n, row in enumerate(csv.DictReader(f), 2):
            raw = (row.get("difficulty_1_to_5") or "").strip().lower()
            if not raw:
                continue
            rid = (row.get("record_id") or "").strip()
            where = f"{path}:{n} (record {rid})"
            if not rid:
                raise ValueError(f"{where}: missing record_id")
            if rid in out:
                raise ValueError(f"{where}: record annotated twice in one file")
            annotator = (row.get("annotator") or "").strip()
            if not annotator:
                raise ValueError(f"{where}: annotator is required so the label has provenance")
            confidence = (row.get("confidence") or "medium").strip().lower()
            if confidence not in {"low", "medium", "high"}:
                raise ValueError(f"{where}: confidence must be low, medium or high")
            notes = (row.get("notes") or "").strip()[:300]
            if raw == "skip":
                out[rid] = Annotation(None, kind, annotator, confidence, notes, unrateable=True)
            elif raw in {"1", "2", "3", "4", "5"}:
                out[rid] = Annotation(int(raw), kind, annotator, confidence, notes)
            else:
                raise ValueError(f"{where}: difficulty must be 1-5 or 'skip', got {raw!r}")
    return out
