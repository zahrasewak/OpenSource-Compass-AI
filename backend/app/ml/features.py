"""Feature extraction for the difficulty model (pure Python, shared by training and inference).

Leakage rules enforced here:
  * Maintainer labels are never read. Phrases that name a difficulty ("good first issue", "easy",
    "hard", ...) are also scrubbed from the TEXT, because weak labels come from those same words.
  * Only information available when the issue is opened is used: title, body, author association, repo
    language. Comments, state, assignees, linked PRs, timestamps and repository activity are excluded.
  * Repository identity is not a feature, so a model cannot memorise projects (splits are also by repository).
Personal data is scrubbed: e-mail addresses, @mentions and URLs are replaced by placeholder tokens.
"""

import math
import re

from app.ml.schema import IssueInput
from app.recommend.vocabulary import canonical_skill

FEATURE_SCHEMA_VERSION = "difficulty_features_v1"
NUMERIC_FEATURES = (
    "title_chars", "body_chars", "n_lines", "n_code_blocks", "n_urls", "n_checkboxes",
    "n_numbered_steps", "has_traceback", "author_is_maintainer",
)
MAX_BODY_CHARS = 5000
_MAINTAINER = {"OWNER", "MEMBER", "COLLABORATOR"}

_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_URL = re.compile(r"https?://\S+")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_MENTION = re.compile(r"(?<![\w])@[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_CHECKBOX = re.compile(r"^\s*[-*]\s*\[[ xX]\]", re.M)
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+\S", re.M)
_TRACEBACK = re.compile(r"(traceback \(most recent call last\)|exception in thread|panicked at|segmentation fault)", re.I)

# Every phrase that maps to a difficulty level in the weak-label vocabulary.
_DIFFICULTY_PHRASES = sorted(
    {"good first issue", "good first bug", "first timers only", "first timer only", "beginner friendly", "beginner",
     "newcomer friendly", "easy", "starter", "trivial", "low hanging fruit", "good for beginners", "medium",
     "intermediate", "moderate", "hard", "advanced", "difficult", "complex", "expert", "very hard"},
    key=len, reverse=True,
)
# Words of a phrase may be joined by spaces, hyphens or underscores ("beginner-friendly", "good_first_issue").
_DIFFICULTY_RE = re.compile(
    r"\b(" + "|".join(r"[\s_-]+".join(re.escape(w) for w in p.split()) for p in _DIFFICULTY_PHRASES) + r")\b", re.I)


def strip_difficulty_phrases(text: str) -> str:
    """Remove every phrase that names a difficulty. Applied before ANY feature (text or numeric) is computed, so
    the length/shape of a label phrase cannot leak the label through counts such as body_chars."""
    return _DIFFICULTY_RE.sub(" ", text)


def scrub_text(text: str | None) -> str:
    """Remove personal data and difficulty-naming phrases; keep the technical content."""
    text = strip_difficulty_phrases((text or "")[:MAX_BODY_CHARS])
    text = _CODE_BLOCK.sub(" codeblock ", text)
    text = _EMAIL.sub(" email ", text)
    text = _URL.sub(" url ", text)
    text = _MENTION.sub(" mention ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def text_for_model(issue: IssueInput) -> str:
    return f"{scrub_text(issue.title)} . {scrub_text(issue.body)}".strip()


def dense_features(issue: IssueInput) -> dict[str, float | str]:
    """Numeric (log1p-scaled counts) + categorical features known at creation time."""
    title = strip_difficulty_phrases(issue.title or "")
    body = strip_difficulty_phrases((issue.body or "")[:MAX_BODY_CHARS])
    raw = {
        "title_chars": len(title), "body_chars": len(body), "n_lines": body.count("\n") + 1 if body else 0,
        "n_code_blocks": len(_CODE_BLOCK.findall(body)), "n_urls": len(_URL.findall(body)),
        "n_checkboxes": len(_CHECKBOX.findall(body)), "n_numbered_steps": len(_NUMBERED.findall(body)),
    }
    out: dict[str, float | str] = {k: math.log1p(v) for k, v in raw.items()}
    out["has_traceback"] = 1.0 if _TRACEBACK.search(body) else 0.0
    out["author_is_maintainer"] = 1.0 if issue.author_association in _MAINTAINER else 0.0
    if issue.repo_language:
        out["lang"] = canonical_skill(issue.repo_language)
    return out


def contains_difficulty_phrase(text: str) -> bool:
    """True if a difficulty-naming phrase is present (tests use this to prove scrubbing works)."""
    return bool(_DIFFICULTY_RE.search(text))


def redact_personal(text: str | None) -> str:
    """Light redaction applied when data is collected: e-mail addresses and @mentions only (text stays readable
    for human reviewers). Stronger scrubbing happens again at feature extraction."""
    return _MENTION.sub("@user", _EMAIL.sub("user@example.invalid", text or ""))
