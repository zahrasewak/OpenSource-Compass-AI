"""Label derivation. Weak labels come from maintainer labels and are provisional, never ground truth."""

from app.ml.schema import SCHEMES, IssueRecord, LabelInfo
from app.recommend.vocabulary import difficulty_hint


def derive_weak_label(labels: list[str]) -> LabelInfo:
    """Difficulty implied by the issue's labels (highest level wins), or an empty label if none applies."""
    hint = difficulty_hint(tuple(labels))
    if hint is None:
        return LabelInfo()
    level, text = hint
    return LabelInfo(difficulty=level, source="weak_label_hint", label_text=text)


def apply_labels(record: IssueRecord) -> IssueRecord:
    """Give an unlabelled record a weak label. Existing labels (e.g. human review) are never overwritten."""
    if record.label.difficulty is not None:
        return record
    return record.model_copy(update={"label": derive_weak_label(record.labels)})


def class_for(level: int, scheme: str) -> str:
    return SCHEMES[scheme][level]
