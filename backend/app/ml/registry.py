"""Model artifact layout, metadata and safe loading.

An artifact directory holds:
  model.joblib     the fitted scikit-learn objects (a dict; no custom classes)
  metadata.json    model id, versions, label definitions, data provenance, metrics summary, sha256 of model.joblib
  evaluation.json  full evaluation report
  report.md        human-readable report

SECURITY: joblib files are pickles; loading one executes code. The sha256 check detects accidental corruption
and swapping of the model file alone, but NOT an attacker who can rewrite both files. Only load artifacts you
trained yourself or obtained from a source you trust, and keep the model directory write-protected.
"""

import hashlib
import json
import sys
from pathlib import Path

from app.ml.features import FEATURE_SCHEMA_VERSION

MODEL_FILE = "model.joblib"
METADATA_FILE = "metadata.json"
METADATA_SCHEMA = 1


class ModelUnavailable(Exception):
    """The model cannot be used. `reason` is a stable machine-readable code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def library_versions() -> dict[str, str]:
    import joblib, numpy, scipy, sklearn  # noqa: E401 (lazy: only when ML dependencies are installed)

    return {"python": ".".join(map(str, sys.version_info[:3])), "scikit-learn": sklearn.__version__,
            "numpy": numpy.__version__, "scipy": scipy.__version__, "joblib": joblib.__version__}


def _minor(version: str) -> str:
    return ".".join(version.split(".")[:2])


def read_metadata(model_dir: str | Path) -> dict:
    path = Path(model_dir) / METADATA_FILE
    if not path.is_file():
        raise ModelUnavailable("artifact_missing", f"{METADATA_FILE} not found")
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModelUnavailable("artifact_invalid", f"unreadable metadata ({type(exc).__name__})") from exc
    if not isinstance(meta, dict) or meta.get("metadata_schema") != METADATA_SCHEMA:
        raise ModelUnavailable("artifact_invalid", "unsupported metadata schema")
    return meta


def load_artifact(model_dir: str | Path, *, allow_synthetic: bool = False, allow_exploratory: bool = False) -> tuple[dict, dict]:
    """Validate and load (fitted_objects, metadata). Raises ModelUnavailable with a precise reason."""
    model_dir = Path(model_dir)
    meta = read_metadata(model_dir)
    model_path = model_dir / MODEL_FILE
    if not model_path.is_file():
        raise ModelUnavailable("artifact_missing", f"{MODEL_FILE} not found")
    if meta.get("trained_on_synthetic") and not allow_synthetic:
        raise ModelUnavailable("synthetic_artifact_refused", "artifact was trained on synthetic test data")
    if meta.get("exploratory") and not allow_exploratory:
        raise ModelUnavailable("exploratory_artifact_refused",
                               "artifact was trained below the data-sufficiency thresholds and is not approved for serving")
    if meta.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
        raise ModelUnavailable("feature_schema_mismatch",
                               f"artifact {meta.get('feature_schema_version')!r} vs code {FEATURE_SCHEMA_VERSION!r}")
    if sha256_file(model_path) != meta.get("model_sha256"):
        raise ModelUnavailable("artifact_invalid", "model file does not match its recorded sha256")
    try:
        import joblib  # noqa: F401
        versions = library_versions()
    except ImportError as exc:
        raise ModelUnavailable("ml_dependencies_missing", "install requirements-ml.txt") from exc
    trained = meta.get("library_versions", {})
    if _minor(trained.get("scikit-learn", "")) != _minor(versions["scikit-learn"]):
        raise ModelUnavailable("library_version_mismatch",
                               f"trained with scikit-learn {trained.get('scikit-learn')}, running {versions['scikit-learn']}")
    try:
        import joblib

        objects = joblib.load(model_path)
    except Exception as exc:  # noqa: BLE001 - any unpickling failure means the artifact cannot be used
        raise ModelUnavailable("artifact_invalid", f"model could not be loaded ({type(exc).__name__})") from exc
    for key in ("tfidf", "dictvec", "scaler", "clf", "classes"):
        if key not in objects:
            raise ModelUnavailable("artifact_invalid", f"model missing component {key!r}")
    return objects, meta
