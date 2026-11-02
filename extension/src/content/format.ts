const COMPONENT_LABELS: Record<string, string> = {
  skill_compatibility: "Skill compatibility",
  interest_similarity: "Interest similarity",
  difficulty_suitability: "Difficulty suitability",
  repository_activity: "Repository activity",
  repository_health: "Repository health",
  contribution_readiness: "Contribution readiness",
  issue_pr_flow: "Issue and PR flow",
  linked_pull_requests: "Linked pull requests",
};

export function humanize(key: string): string {
  return COMPONENT_LABELS[key] ?? key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

export const words = (s: string) => s.replace(/_/g, " ");

export function statusText(status: string): string {
  switch (status) {
    case "unknown": return "Unknown";
    case "stale": return "Out of date";
    case "not_applicable": return "Not applicable";
    default: return "";
  }
}

export const round = (n: number) => Math.round(n);

export function ageText(hours: number | null): string {
  if (hours === null) return "at an unknown time";
  if (hours < 1) return "less than an hour ago";
  if (hours < 48) return `${Math.round(hours)} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

/** Why the optional ML model gave no estimate (codes come from the backend). */
export function mlReasonText(reason: string | null | undefined): string {
  switch (reason) {
    case "not_configured": return "no ML model is configured on this backend";
    case "artifact_missing": return "the configured model files are missing";
    case "artifact_invalid": return "the configured model failed its integrity check";
    case "ml_dependencies_missing": return "the backend does not have the optional ML packages installed";
    case "feature_schema_mismatch":
    case "library_version_mismatch": return "the model is incompatible with this backend version";
    case "synthetic_artifact_refused": return "the model was trained only on synthetic test data";
    case "exploratory_artifact_refused": return "the model was trained on too little data to be approved for use";
    case "not_enough_text": return "this issue has too little text";
    default: return "the model is not available";
  }
}

export const LEVEL_NAMES = ["", "beginner", "easy", "intermediate", "advanced", "expert"];
