"""Contribution readiness: how well an issue and its repository prepare a newcomer to start.

Kept separate from repository health (maintenance/activity). Everything here is detection by file
path or text pattern: finding test files does NOT mean tests exist in a runnable state or pass, and
a mentioned test command was NOT executed. Execution belongs to the (separate) sandbox feature.
"""

import re
from datetime import datetime

from app.intelligence.activity import _days, aware, parse
from app.intelligence.config import READINESS_WEIGHTS, IntelConfig
from app.intelligence.scoring import Report, Signal, combine, known, unknown
from app.services.github.models import FileText, GitTree, TimelineReference, TreeEntry

G = "readiness"

# ====================================================================== repository documentation (from the file tree)

_VENDORED = ("node_modules/", "vendor/", "third_party/", "third-party/", ".venv/", "venv/", "dist/", "build/")
_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs"}
_TEST_FILE = re.compile(
    r"(^test_.+\.py$|.+_test\.(py|go|rb|rs)$|.+\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$|.+Tests?\.(java|kt|cs|swift)$|.+_spec\.rb$)"
)
_TEST_CONFIG = {
    "pytest.ini": "pytest", "conftest.py": "pytest", "tox.ini": "tox", "noxfile.py": "nox",
    "jest.config.js": "jest", "jest.config.ts": "jest", ".mocharc.json": "mocha", ".mocharc.js": "mocha",
    "phpunit.xml": "phpunit", "phpunit.xml.dist": "phpunit",
}
_CONTRIBUTING_EQUIVALENT = re.compile(r"^(docs?/)?(development|developing|hacking|contributing/[^/]+|how[-_]to[-_]contribute)(\.(md|rst|txt))?$", re.I)
_CI = re.compile(r"^(\.github/workflows/[^/]+\.ya?ml|\.circleci/config\.yml|\.travis\.yml|azure-pipelines\.yml|jenkinsfile|\.gitlab-ci\.yml)$", re.I)
_DEP_BOT = re.compile(r"^(\.github/dependabot\.ya?ml|renovate\.json5?|\.renovaterc(\.json)?)$", re.I)


def is_test_path(path: str) -> bool:
    lower = path.lower()
    if any(v in lower for v in _VENDORED):
        return False
    parts = path.split("/")
    return any(p.lower() in _TEST_DIRS for p in parts[:-1]) or bool(_TEST_FILE.match(parts[-1]))


def analyze_tree(tree: GitTree) -> dict:
    """Detect documentation/test/CI files by path. `complete` is False when GitHub truncated the listing,
    in which case the ABSENCE of a file proves nothing."""
    blobs: list[TreeEntry] = [e for e in tree.entries if e.type == "blob"]
    paths = [e.path for e in blobs]
    lower = {p.lower(): p for p in paths}

    def first(pattern: re.Pattern | None = None, exact: tuple[str, ...] = ()) -> str | None:
        for key, original in lower.items():
            if key in exact or (pattern is not None and pattern.match(original)):
                return original
        return None

    contributing = first(exact=("contributing.md", "contributing.rst", "contributing.txt", "contributing",
                                ".github/contributing.md", ".github/contributing.rst", "docs/contributing.md",
                                "docs/contributing.rst", "docs/contributing.txt"))
    kind = "guide" if contributing else None
    if contributing is None:
        contributing = first(_CONTRIBUTING_EQUIVALENT)
        kind = "equivalent" if contributing else None
    test_paths = [p for p in paths if is_test_path(p)]
    frameworks = sorted({fw for p in paths for name, fw in _TEST_CONFIG.items() if p.split("/")[-1] == name})
    return {
        "tree_status": "ok" if not tree.truncated else "truncated",
        "tree_sha": tree.sha,
        "files_seen": len(paths),
        "contributing": {"path": contributing, "kind": kind} if contributing else None,
        "readme": first(re.compile(r"^readme(\.[a-z]+)?$", re.I)),
        "code_of_conduct": first(re.compile(r"^(\.github/|docs/)?code[_-]of[_-]conduct(\.[a-z]+)?$", re.I)),
        "security_policy": first(re.compile(r"^(\.github/|docs/)?security(\.[a-z]+)?$", re.I)),
        "license_file": first(re.compile(r"^(license|licence|copying)(\.[a-z]+)?$", re.I)),
        "pr_template": first(re.compile(r"^(\.github/|docs/)?pull_request_template(\.[a-z]+)?$", re.I)),
        "issue_templates": any(p.lower().startswith(".github/issue_template") for p in paths),
        "test_files": {"count": len(test_paths), "samples": test_paths[:5], "frameworks_detected": frameworks},
        "ci_files": [p for p in paths if _CI.match(p)][:5],
        "dependency_bot": first(_DEP_BOT),
    }


_TEST_COMMANDS = re.compile(
    r"(\bpytest\b|\btox\b|\bnox\b|\bnpm (?:run )?test\b|\byarn test\b|\bpnpm (?:run )?test\b|\bmake (?:test|check)\b|"
    r"\bcargo test\b|\bgo test\b|\bmvn (?:\w+ )?test\b|\bgradlew? test\b|\bbundle exec rspec\b|\brake test\b|"
    r"\bphpunit\b|\bdotnet test\b|\brun(?:ning)? (?:the )?(?:unit )?tests\b)",
    re.I,
)


def analyze_instructions(files: dict[str, FileText | None]) -> dict:
    """Look for documented test commands / contribution pointers in fetched README/CONTRIBUTING text.

    `files` maps a label ("README", "CONTRIBUTING") to its text (None if unavailable/too large).
    """
    commands = []
    available = []
    for label, f in files.items():
        if f is None or f.text is None:
            continue
        available.append(label)
        for m in _TEST_COMMANDS.finditer(f.text):
            snippet = f.text[max(0, m.start() - 20): m.end() + 40].replace("\n", " ").strip()
            commands.append({"source": label, "command": m.group(0), "context": snippet[:100]})
            if len(commands) >= 5:
                break
    readme = files.get("README")
    mentions = bool(readme and readme.text and re.search(r"^#+\s*contribut", readme.text, re.I | re.M))
    return {"instructions_status": "ok" if available else "unavailable", "files_read": available,
            "test_commands_documented": commands, "readme_has_contributing_section": mentions}


# ====================================================================== issue-level analysis

_BUG_TITLE = re.compile(r"\b(bug|error|crash(es|ed)?|exception|fail(s|ed|ure)?|broken|regression|not working)\b", re.I)
_GFI = {"good first issue", "good-first-issue", "good first bug", "first timers only", "beginner friendly", "newcomer", "starter"}
_HELP = {"help wanted", "help-wanted", "contributions welcome", "up for grabs", "pr welcome", "prs welcome"}


def _check_repro(t: str) -> bool:
    return bool(re.search(r"(steps? to reproduce|to reproduce|how to reproduce|reproduc|\brepro\b)", t) or
                len(re.findall(r"^\s*\d+[.)]\s+\S", t, re.M)) >= 2)


def _check_expected(t: str) -> bool:
    return bool(re.search(r"(expected|actual|what (i|you) expect|should (be|have|return|happen|not))", t))


def _check_snippet(t: str) -> bool:
    return bool("```" in t or re.search(r"(traceback|error:|exception|stack trace|^\s{4}\S)", t, re.M))


def _check_environment(t: str) -> bool:
    return bool(re.search(r"(version|\bv?\d+\.\d+(\.\d+)?\b|python \d|node \d|windows|linux|macos|ubuntu|os:)", t))


def _check_acceptance(t: str) -> bool:
    return bool(re.search(r"(acceptance criteria|definition of done|- \[[ x]\]|\bshould\b|\bmust\b|\bneeds? to\b)", t))


def _check_context(t: str) -> bool:
    return bool("http" in t or "```" in t or re.search(r"(for example|e\.g\.|example)", t))


def analyze_issue_quality(title: str, body: str | None, labels: tuple[str, ...]) -> dict:
    """Rubric of lexical checks; results show which were found. Presence of words is not proof of quality."""
    text = (body or "").lower()
    is_bug = any("bug" in l.lower() for l in labels) or bool(_BUG_TITLE.search(title))
    length_ok = len(text.strip()) >= 200
    checks = {"substantive_length": length_ok}
    if is_bug:
        checks.update({"reproduction_steps": _check_repro(text), "expected_vs_actual": _check_expected(text),
                       "code_or_log_snippet": _check_snippet(text), "environment_or_version": _check_environment(text)})
    else:
        checks.update({"acceptance_criteria": _check_acceptance(text), "example_or_context": _check_context(text)})
    return {"rubric": "bug_report" if is_bug else "feature_or_task", "checks": checks,
            "body_chars": len((body or "").strip())}


def classify_linked_prs(refs: list[TimelineReference], repository_full_name: str) -> dict:
    """Group PR cross-references by outcome. A reference is a mention, not proof the PR fixes the issue,
    and none is labelled a duplicate."""
    same = repository_full_name.lower()
    prs: dict[int, dict] = {}
    other = 0
    for r in refs:
        if not r.source_is_pull_request:
            continue
        if (r.source_repository or "").lower() != same:
            other += 1
            continue
        if r.source_merged_at:
            state = "merged"
        elif r.source_state == "open":
            state = "open"
        else:
            state = "closed_unmerged"
        prs[r.source_number] = {"number": r.source_number, "state": state, "html_url": r.source_html_url,
                                "merged_at": r.source_merged_at.isoformat() if r.source_merged_at else None}
    return {"pull_requests": sorted(prs.values(), key=lambda p: p["number"]), "other_repository_references": other}


# ====================================================================== scoring


def _guide_signal(docs: dict | None) -> Signal:
    n, w = "contribution_guide", READINESS_WEIGHTS["contribution_guide"]
    if not docs or docs.get("tree_status") not in {"ok", "truncated"}:
        return unknown(n, G, w, "Repository file listing was not collected, so contribution guides are unknown.")
    found, complete = docs.get("contributing"), docs["tree_status"] == "ok"
    if found and found["kind"] == "guide":
        return known(n, G, w, 100, 0.8, [f"Contribution guide found: {found['path']}."])
    if found:
        return known(n, G, w, 75, 0.6, [f"A contribution-style document was found: {found['path']}."],
                     ["Not a standard CONTRIBUTING file; contents were not checked."])
    if docs.get("readme_has_contributing_section"):
        return known(n, G, w, 55, 0.5, ["README has a contributing section."])
    if not complete:
        return unknown(n, G, w, "File listing was truncated, so a missing contribution guide cannot be concluded.")
    return known(n, G, w, 15, 0.6, ["No CONTRIBUTING file or contributing section found."])


def _tests_signal(docs: dict | None) -> Signal:
    n, w = "tests", READINESS_WEIGHTS["tests"]
    if not docs or docs.get("tree_status") not in {"ok", "truncated"}:
        return unknown(n, G, w, "Repository file listing was not collected, so test files are unknown.")
    tests = docs["test_files"]
    commands = docs.get("test_commands_documented") or []
    instr_ok = docs.get("instructions_status") == "ok"
    warnings = ["Tests were detected by file path only; they were not run, so whether they pass is unknown."]
    if tests["count"] == 0:
        if docs["tree_status"] != "ok" and not commands:
            return unknown(n, G, w, "File listing was truncated, so missing tests cannot be concluded.")
        return known(n, G, w, 30 if commands else 10, 0.6,
                     ["No test files detected by path." + (" A test command is mentioned in the docs." if commands else "")])
    score = 60.0 + (40.0 if commands else 0.0)
    evidence = [f"{tests['count']} test file(s) detected by path (e.g. {', '.join(tests['samples'][:2])})."]
    if tests["frameworks_detected"]:
        evidence.append(f"Test configuration detected: {', '.join(tests['frameworks_detected'])}.")
    if commands:
        evidence.append(f"Docs mention a test command: '{commands[0]['command']}' (in {commands[0]['source']}); not executed.")
    elif instr_ok:
        warnings.append("No test-running command was found in README/CONTRIBUTING.")
    else:
        warnings.append("README/CONTRIBUTING could not be read, so documented test instructions are unknown.")
    return known(n, G, w, score, 0.6 if instr_ok else 0.45, evidence, warnings)


def _description_signal(issue: dict) -> Signal:
    n, w = "issue_description", READINESS_WEIGHTS["issue_description"]
    q = analyze_issue_quality(issue["title"], issue["body"], tuple(issue["labels"]))
    checks = q["checks"]
    passed = [k for k, v in checks.items() if v]
    missing = [k for k, v in checks.items() if not v]
    evidence = [f"Checked as a {q['rubric'].replace('_', ' ')}: found {', '.join(passed) or 'none of the expected elements'}."]
    warnings = [f"Missing: {', '.join(m.replace('_', ' ') for m in missing)}."] if missing else []
    warnings.append("Quality is judged from wording patterns only.")
    return known(n, G, w, 100 * len(passed) / len(checks), 0.6, evidence, warnings)


def _prs_signal(linked: dict | None, now: datetime, cfg: IntelConfig) -> Signal:
    n, w = "linked_pull_requests", READINESS_WEIGHTS["linked_pull_requests"]
    if not linked:
        return unknown(n, G, w, "Linked pull requests have not been checked for this issue.")
    collected = parse(linked["collected_at"])
    if collected is None or _days(now, collected) > cfg.linked_pr_max_age_days:
        return unknown(n, G, w, "Linked-pull-request data is out of date.", status="stale")
    prs = linked["pull_requests"]
    note = ["Based on cross-references (mentions), which can miss manually linked PRs and do not prove a fix."]
    if linked.get("timeline_truncated"):
        note.append("The timeline was truncated; later references may be missing.")
    if not prs:
        return known(n, G, w, 100, 0.6, ["No pull requests reference this issue."], note)
    by_state = {s: [p for p in prs if p["state"] == s] for s in ("open", "merged", "closed_unmerged")}
    desc = ", ".join(f"{len(v)} {k.replace('_', ' ')}" for k, v in by_state.items() if v)
    if by_state["open"]:
        return known(n, G, w, 25, 0.6, [f"Referenced by pull request(s): {desc}. Someone may already be working on it."], note)
    if by_state["merged"]:
        return known(n, G, w, 40, 0.6, [f"Referenced by pull request(s): {desc}. The issue is still open, so it may be partly or already addressed."], note)
    return known(n, G, w, 70, 0.6, [f"Referenced only by closed, unmerged pull request(s): {desc}."],
                 note + ["Check why they were closed (rejected approach, abandoned, or superseded)."])


def _labels_signal(labels: tuple[str, ...]) -> Signal:
    n, w = "labels", READINESS_WEIGHTS["labels"]
    low = {l.lower() for l in labels}
    if low & _GFI:
        return known(n, G, w, 100, 0.5, [f"Labelled as beginner-friendly ({sorted(low & _GFI)[0]})."], ["Labels are noisy human signals."])
    if low & _HELP:
        return known(n, G, w, 75, 0.5, [f"Maintainers invite contributions ({sorted(low & _HELP)[0]})."], ["Labels are noisy human signals."])
    return unknown(n, G, w, "No contribution-invitation label; absence of a label is not evidence the issue is unsuitable.")


def _availability_signal(issue: dict, now: datetime, cfg: IntelConfig) -> Signal:
    n, w = "availability", READINESS_WEIGHTS["availability"]
    if _days(now, parse(issue["fetched_at"])) > cfg.issue_max_age_days:  # type: ignore[arg-type]
        return unknown(n, G, w, "Issue data is out of date; it may have closed or been assigned.", status="stale")
    if issue["state"] != "open":
        return known(n, G, w, 0.0, 1.0, ["Issue is not open."])
    if issue["assignees"]:
        return known(n, G, w, 30, 0.7, [f"Open but assigned to {', '.join(issue['assignees'])}."])
    return known(n, G, w, 100, 0.6, ["Open with no assignee (as of the last sync)."], ["Someone may be working on it without being assigned."])


def _other_docs_signal(docs: dict | None) -> Signal:
    n, w = "other_documentation", READINESS_WEIGHTS["other_documentation"]
    if not docs or docs.get("tree_status") not in {"ok", "truncated"}:
        return unknown(n, G, w, "Repository file listing was not collected.")
    points = [("README", 40, docs["readme"]), ("PR template", 20, docs["pr_template"]),
              ("issue templates", 15, docs["issue_templates"]), ("code of conduct", 15, docs["code_of_conduct"]),
              ("security policy", 10, docs["security_policy"])]
    score = sum(p for _, p, present in points if present)
    found = [name for name, _, present in points if present]
    if docs["tree_status"] != "ok" and score < 100:
        return unknown(n, G, w, "File listing was truncated, so missing documents cannot be concluded.")
    return known(n, G, w, score, 0.5, [f"Found: {', '.join(found) or 'none of README, templates, code of conduct, security policy'}."])


def compute_readiness(issue: dict, docs: dict | None, linked: dict | None, now: datetime, cfg: IntelConfig) -> Report:
    """Issue readiness. `issue` needs title, body, labels, state, assignees, fetched_at (ISO). Heuristic, not ML."""
    signals = [
        _guide_signal(docs), _tests_signal(docs), _description_signal(issue), _prs_signal(linked, now, cfg),
        _labels_signal(tuple(issue["labels"])), _availability_signal(issue, now, cfg), _other_docs_signal(docs),
    ]
    score, coverage, by_name = combine(signals, cfg.unknown_fill)
    warnings = list(dict.fromkeys(w for s in by_name.values() for w in s.warnings))
    avail = by_name["availability"]
    if avail.status == "known" and avail.score == 0.0:
        return Report("issue_readiness", 0.0, "not_available", 1.0, by_name, {}, tuple(warnings), ("The issue is not open.",))
    if score is None or coverage < cfg.min_coverage:
        label = "insufficient_data"
    elif score >= 65:  # confidence caps (0.5-0.8) mean even a perfect issue scores ~72, so 75+ is unreachable
        label = "well_prepared"
    elif score >= 45:
        label = "partially_prepared"
    else:
        label = "poorly_prepared"
    notes = ["Detected files and mentioned commands were not executed; this does not show that tests pass or that the project builds."]
    return Report("issue_readiness", score, label, coverage, by_name, {}, tuple(warnings), tuple(notes))
