"""Shared structures for explainable reports (repository health, issue readiness).

Statuses of a signal:
  known           - computed from retrieved data (score 0-100; 0 means genuinely low)
  unknown         - data could not be retrieved / was never collected; pulled toward the fill value
  stale           - data exists but is too old to trust; treated like unknown
  not_applicable  - data was retrieved and shows the signal does not apply (e.g. a project that publishes
                    no releases); excluded from the score and the remaining weights are renormalised
These reports are heuristic summaries. They are not ML predictions and not calibrated probabilities.
"""

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Signal:
    name: str
    group: str
    weight: float
    score: float | None
    confidence: float
    status: str
    evidence: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    effective_score: float | None = None  # value used in the report score (None for not_applicable)


def known(name, group, weight, score, confidence, evidence=(), warnings=()) -> Signal:
    return Signal(name, group, weight, round(score, 2), confidence, "known", tuple(evidence), tuple(warnings))


def unknown(name, group, weight, why, status="unknown") -> Signal:
    return Signal(name, group, weight, None, 0.0, status, (), (why,))


def not_applicable(name, group, weight, why) -> Signal:
    return Signal(name, group, weight, None, 0.0, "not_applicable", (why,), ())


@dataclass(frozen=True)
class Report:
    kind: str  # "repository_health" | "issue_readiness"
    score: float | None  # 0-100; None when no signal applies
    label: str
    coverage: float  # 0-1: share of applicable weight backed by real evidence
    signals: dict[str, Signal]
    group_scores: dict[str, float | None] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "score": self.score, "label": self.label, "coverage": self.coverage,
            "group_scores": self.group_scores, "warnings": list(self.warnings), "notes": list(self.notes),
            "signals": {
                n: {"group": s.group, "weight": s.weight, "score": s.score, "confidence": s.confidence,
                    "status": s.status, "effective_score": s.effective_score,
                    "evidence": list(s.evidence), "warnings": list(s.warnings)}
                for n, s in self.signals.items()
            },
        }


def combine(signals: list[Signal], fill: float) -> tuple[float | None, float, dict[str, Signal]]:
    """Weighted score over *applicable* signals.

    effective = confidence * score + (1 - confidence) * fill      (known)
    effective = fill                                               (unknown / stale)
    score     = sum(w * effective) / sum(w over applicable signals)
    coverage  = sum(w * confidence) / sum(w over applicable signals)
    """
    applicable = [s for s in signals if s.status != "not_applicable"]
    total = sum(s.weight for s in applicable)
    out: dict[str, Signal] = {}
    for s in signals:
        if s.status == "not_applicable":
            out[s.name] = s
            continue
        if s.score is None:
            eff = fill
        else:
            eff = s.confidence * s.score + (1 - s.confidence) * fill
        out[s.name] = replace(s, effective_score=round(eff, 2))
    if total <= 0:
        return None, 0.0, out
    score = sum(s.weight * out[s.name].effective_score for s in applicable) / total  # type: ignore[operator]
    coverage = sum(s.weight * (s.confidence if s.score is not None else 0.0) for s in applicable) / total
    return round(score, 2), round(coverage, 3), out


def group_score(signals: dict[str, Signal], group: str, fill: float) -> float | None:
    members = [s for s in signals.values() if s.group == group]
    score, _, _ = combine(members, fill)
    return score
