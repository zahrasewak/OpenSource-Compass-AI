"""Dataset record schema and label definitions shared by training and inference.

Pure Python (pydantic only): importing this never pulls in scikit-learn.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RECORD_SCHEMA_VERSION = 1

LabelSource = Literal["weak_label_hint", "llm_annotation", "human_review", "none"]
Confidence = Literal["low", "medium", "high"]
RUBRIC_VERSION = "rubric_v1"  # see docs/LABELING_RUBRIC.md

# Difficulty levels follow the product spec: 1 beginner, 2 easy, 3 intermediate, 4 advanced, 5 expert.
FIVE_CLASS = {1: "beginner", 2: "easy", 3: "intermediate", 4: "advanced", 5: "expert"}
# Coarser scheme for when data cannot support five classes (it usually cannot: see docs/ML.md).
THREE_CLASS = {1: "easy", 2: "easy", 3: "medium", 4: "hard", 5: "hard"}
SCHEMES: dict[str, dict[int, str]] = {"five_class": FIVE_CLASS, "three_class": THREE_CLASS}

LABEL_DEFINITIONS = {
    "weak_label_hint": (
        "Derived from the issue's own maintainer labels (e.g. 'good first issue' -> 1, 'easy' -> 2, 'hard' -> 4). "
        "A noisy human signal, NOT verified difficulty. The labels themselves are never used as model features."
    ),
    "llm_annotation": (
        "Assigned by an AI model that read the issue and applied the written rubric. Provisional: better informed than "
        "a label-derived hint, but NOT human-verified ground truth, and its errors are correlated with any AI model "
        "trained or evaluated on it."
    ),
    "human_review": "Assigned by a person who read the issue (preferred; the only label a result should be called 'verified' on).",
    "none": "No difficulty label available; excluded from supervised training.",
}


class LabelInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    difficulty: int | None = Field(default=None, ge=1, le=5)
    source: LabelSource = "none"
    label_text: str | None = None  # the maintainer label a weak label came from (audit only)
    confidence: Confidence | None = None  # annotator's own uncertainty; "low" labels are kept but not trained on
    annotator: str | None = None  # opaque id, e.g. "human_1" or "llm_claude" (never a real name or GitHub handle)
    rubric_version: str | None = None
    notes: str | None = Field(default=None, max_length=300)  # short rationale or reason an issue was unrateable
    unrateable: bool = False  # question/support/duplicate/not an actionable task: deliberately has no difficulty


class IssueRecord(BaseModel):
    """One public issue. Only fields known when the issue is opened may be used as features.

    Labels, state, comments, assignees and linked PRs are stored for provenance/weak labelling and are
    deliberately NOT features (they are either the label source or only known after the work happens).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = RECORD_SCHEMA_VERSION
    record_id: str
    repo: str  # "owner/name"
    issue_number: int = Field(ge=1)
    html_url: str
    title: str
    body: str | None = None
    labels: list[str] = Field(default_factory=list)
    author_association: str | None = None
    repo_language: str | None = None
    created_at: str | None = None
    fetched_at: str | None = None
    source: Literal["github_api", "database_export", "synthetic_test"] = "github_api"
    label: LabelInfo = Field(default_factory=LabelInfo)


class IssueInput(BaseModel):
    """What the model sees at inference time (no labels, no outcomes). Extra fields are rejected so a label
    can never be passed in by accident."""

    model_config = ConfigDict(extra="forbid")

    title: str
    body: str | None = None
    author_association: str | None = None
    repo_language: str | None = None


def scheme_names(scheme: str) -> list[str]:
    """Ordered class names of a scheme."""
    seen: list[str] = []
    for level in sorted(SCHEMES[scheme]):
        if SCHEMES[scheme][level] not in seen:
            seen.append(SCHEMES[scheme][level])
    return seen
