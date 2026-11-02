"""Writes the AI pilot annotations (rubric_v1) into the annotation CSV, keyed by position in the pilot template.

This file IS the record of the AI annotator's judgements for the 80-issue pilot, kept in version control so the
labels and their rationale can be audited and disagreed with. They are provisional (llm_annotation), based only on
the title and the first 600 characters of each issue, and are NOT human-verified ground truth.

    python ml-training/pilot_annotate_llm.py  <template.csv>  <out.csv>
"""

import csv
import sys

ANNOTATOR = "llm_claude_1"
H, M, L = "high", "medium", "low"
SKIP = "skip"

# position (1-based, as in the template) -> (level or 'skip', confidence, rationale)
LABELS = {
    1: (2, M, "display-only change in reporter"), 2: (2, M, "build file edit"),
    3: (3, M, "new lint rule, template exists"), 4: (3, M, "numeric edge cases in pandas internals"),
    5: (3, M, "printer logic for selector syntax"), 6: (3, M, "extends version command; prerelease syntax design"),
    7: (3, M, "compare before write in build pipeline"), 8: (3, M, "gitignore semantics in ignore crate"),
    9: (1, H, "remove unused import"), 10: (3, M, "extend lint to another type; needs lint utilities"),
    11: (3, M, "C++ extension edge case"), 12: (3, L, "feature blocked by maintainer policy, not just implementation"),
    13: (4, M, "storage engine design; proposal needs maintainer decision"),
    14: (SKIP, H, "support request in Russian, no actionable task"), 15: (3, M, "language service completions"),
    16: (2, L, "body empty; likely a warning in the imputer plus tests"), 17: (4, L, "runtime internals on an experimental platform"),
    18: (2, M, "small display change"), 19: (3, M, "new client option and API design"),
    20: (3, L, "design debate about warning behaviour"), 21: (3, M, "refactor of a header-only library"),
    22: (1, H, "documentation wording"), 23: (2, M, "tooling replacement, clearly scoped"),
    24: (3, L, "only the template is visible in the excerpt"), 25: (2, M, "dependency bump in a release branch"),
    26: (2, M, "input validation panic, location evident"), 27: (4, M, "semantic design change across ignore handling"),
    28: (2, M, "add a missing assertion in a test"), 29: (4, L, "lint interaction needs type inference"),
    30: (3, M, "new writer subclass with tests"), 31: (4, M, "TLS context regression, security-sensitive"),
    32: (4, L, "filesystem-specific storage behaviour"), 33: (2, M, "deduplicate identical skills"),
    34: (3, L, "language service feature"), 35: (1, H, "documentation correction"),
    36: (4, M, "runtime internals, re-entrancy"), 37: (4, M, "renderer internals and lifecycle"),
    38: (2, L, "may already be supported; unclear"), 39: (3, L, "plugin interaction, unclear owner"),
    40: (2, M, "cause and fix location given"), 41: (2, L, "old issue, may be obsolete"),
    42: (2, M, "mechanical but needs per-case judgement"), 43: (3, M, "printer parenthesization rules"),
    44: (3, M, "marker parsing in a core library"), 45: (2, M, "bug in one function, location given"),
    46: (2, L, "release infrastructure rather than code"), 47: (SKIP, H, "spam"),
    48: (3, L, "lint-design discussion"), 49: (1, M, "docs cross-reference"),
    50: (SKIP, H, "spam"), 51: (2, M, "flaky test from port reuse, fix evident"),
    52: (SKIP, H, "empty issue template"), 53: (3, M, "semantic classification in language service"),
    54: (2, M, "extend a docs example with code"), 55: (4, M, "scheduler metrics semantics"),
    56: (4, M, "experimental SSR hydration regression"), 57: (2, M, "json.dumps options; small API decision"),
    58: (1, H, "broken link"), 59: (2, M, "static-analysis warnings in C++"),
    60: (4, M, "new CLI capability touching file-access design"), 61: (3, M, "new accessor method with tests and docs"),
    62: (3, L, "embedded-language formatting"), 63: (2, L, "umbrella cleanup, each item small"),
    64: (3, M, "platform-specific failures need a 32-bit environment"), 65: (2, L, "docs requiring platform knowledge"),
    66: (1, H, "outdated docs link"), 67: (4, M, "new lint needing ownership analysis"),
    68: (3, M, "cache-directory logic change"), 69: (2, M, "encode credentials, fix suggested"),
    70: (SKIP, H, "bot-generated dependency dashboard"), 71: (3, L, "unclear whether client or server issue"),
    72: (3, M, "language service behaviour"), 73: (3, M, "bug in two scalers, needs tests"),
    74: (4, M, "concurrency race in runtime"), 75: (3, M, "reactivity option regression"),
    76: (2, M, "default change with compatibility question"), 77: (1, H, "missing docs entry"),
    78: (1, M, "comment tag typo in generated docs"), 79: (2, M, "remove unused public method with docs and tests"),
    80: (2, M, "docs requiring library knowledge"),
}


def main(template: str, out: str) -> None:
    with open(template, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != len(LABELS):
        raise SystemExit(f"template has {len(rows)} rows but {len(LABELS)} annotations are defined")
    for i, row in enumerate(rows, 1):
        level, conf, why = LABELS[i]
        row.update({"difficulty_1_to_5": str(level), "confidence": conf, "annotator": ANNOTATOR, "notes": why})
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
