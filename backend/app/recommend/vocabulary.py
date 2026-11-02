"""Small curated technology vocabulary used for keyword detection.

This is deliberately simple and conservative: ambiguous words ("go", "c", "r", "express") are
only recognised via unambiguous aliases, and detection is by whole-word match. It will miss
skills that are not listed. That is a limitation of the baseline, not evidence of absence.
"""

import re

# canonical name -> aliases that may appear in issue text (canonical itself is always an alias)
_TERMS: dict[str, tuple[str, ...]] = {
    "python": (), "javascript": (), "typescript": (), "java": (), "go": ("golang",), "rust": (),
    "c++": ("cpp",), "c#": ("csharp",), "ruby": (), "php": (), "swift": (), "kotlin": (), "scala": (),
    "dart": (), "elixir": (), "haskell": (), "bash": (), "webassembly": ("wasm",),
    "react": ("reactjs", "react.js"), "vue": ("vue.js", "vuejs"), "angular": (), "svelte": (),
    "node": ("node.js", "nodejs"), "express": ("express.js", "expressjs"),
    "django": (), "flask": (), "fastapi": (), "spring": ("spring boot", "springboot"),
    "rails": ("ruby on rails",), "tailwind": ("tailwindcss",), "webpack": (),
    "docker": (), "kubernetes": ("k8s",), "terraform": (),
    "sql": (), "postgresql": ("postgres",), "mysql": (), "sqlite": (), "redis": (), "mongodb": (),
    "graphql": (), "html": (), "css": (),
    "pytorch": (), "tensorflow": (), "numpy": (), "pandas": (), "scikit-learn": ("sklearn",),
}

# aliases users may type in their profile (superset: includes ambiguous short forms)
_USER_ALIASES = {"js": "javascript", "ts": "typescript", "py": "python", "golang": "go", "node.js": "node",
                 "nodejs": "node", "reactjs": "react", "react.js": "react", "postgres": "postgresql",
                 "k8s": "kubernetes", "cpp": "c++", "csharp": "c#", "sklearn": "scikit-learn",
                 "vue.js": "vue", "express.js": "express"}

_ALIAS_TO_CANON: dict[str, str] = {}
for _canon, _aliases in _TERMS.items():
    for _a in (_canon, *_aliases):
        _ALIAS_TO_CANON[_a] = _canon

# "go" is excluded from text detection (too ambiguous) but is valid as a canonical skill/language.
_DETECTABLE = {a: c for a, c in _ALIAS_TO_CANON.items() if a != "go"}

_PATTERNS = [
    (canon, re.compile(r"(?<![A-Za-z0-9+#])" + re.escape(alias) + r"(?![A-Za-z0-9+#])", re.IGNORECASE))
    for alias, canon in sorted(_DETECTABLE.items())
]


def canonical_skill(name: str) -> str:
    """Normalise a skill/language name ("Node.js" -> "node"); unknown names are just lower-cased."""
    key = name.strip().lower()
    return _USER_ALIASES.get(key) or _ALIAS_TO_CANON.get(key) or key


def detect_skills(text: str) -> set[str]:
    """Canonical technologies mentioned in `text` (whole-word matches only)."""
    return {canon for canon, pattern in _PATTERNS if pattern.search(text)}


# --- difficulty hints from labels. Labels are noisy human signals, not measured difficulty. ---
_LABEL_LEVELS = {
    1: ("good first issue", "beginner", "beginner friendly", "first timers only", "first timer only",
        "good first bug", "newcomer friendly"),
    2: ("easy", "starter", "trivial", "low hanging fruit", "good for beginners"),
    3: ("medium", "intermediate", "moderate"),
    4: ("hard", "advanced", "difficult", "complex"),
    5: ("expert", "very hard"),
}
_LABEL_TO_LEVEL = {label: lvl for lvl, labels in _LABEL_LEVELS.items() for label in labels}
_LABEL_PREFIX = re.compile(r"^(difficulty|level|complexity)\s+")


def normalise_label(label: str) -> str:
    text = re.sub(r"[_\-:/]+", " ", label.lower())
    text = re.sub(r"\s+", " ", text).strip()
    return _LABEL_PREFIX.sub("", text)


def difficulty_hint(labels: tuple[str, ...]) -> tuple[int, str] | None:
    """Highest difficulty level implied by labels (conservative), with the label that implied it."""
    best: tuple[int, str] | None = None
    for label in labels:
        level = _LABEL_TO_LEVEL.get(normalise_label(label))
        if level is not None and (best is None or level > best[0]):
            best = (level, label)
    return best
