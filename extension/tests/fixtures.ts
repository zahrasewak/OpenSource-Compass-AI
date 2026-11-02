import { vi } from "vitest";
import type { AdminRequest, Request } from "../src/shared/messages";
import type { IssueRef } from "../src/shared/url";

export const ISSUE: IssueRef = { owner: "acme", repo: "ml-api", number: 7 };
export const GOOD_TOKEN_PAYLOAD = (expSeconds: number) =>
  `${btoa(JSON.stringify({ alg: "HS256" }))}.${btoa(JSON.stringify({ sub: "1", exp: expSeconds }))}.sig`;

const component = (score: number | null, weight: number, status = "known") => ({
  score, status, weight, confidence: score === null ? 0 : 0.7, effective_score: score ?? 25,
  evidence: score === null ? [] : ["Some evidence line."], warnings: [],
});

export function issueMatch(over: Record<string, unknown> = {}) {
  return {
    model: { name: "heuristic_baseline", version: "v1", kind: "heuristic", is_ml_prediction: false, description: "d", score_note: "Heuristic baseline, not an ML prediction." },
    weights: { skill_compatibility: 0.35 },
    ranking_reliability: { status: "ok", reasons: [] },
    match: {
      match_id: null, generated_at: null,
      repository: { owner: "acme", name: "ml-api", full_name: "acme/ml-api" },
      issue: { number: 7, title: "Parser crashes on load", html_url: "https://github.com/acme/ml-api/issues/7" },
      final_score: 63.4, coverage: 0.58, insufficient_information: false,
      components: {
        skill_compatibility: component(80, 0.35), interest_similarity: component(null, 0.25, "unknown"),
        difficulty_suitability: component(70, 0.2), repository_activity: component(100, 0.15), contribution_readiness: component(60, 0.05),
      },
      reasons: ["Involves python (repository language); your self-reported proficiency is 0.90."],
      missing_skills: ["fastapi"], warnings: ["Required skills come from keyword detection and may be incomplete."],
      ...(over.match as object),
    },
    ...Object.fromEntries(Object.entries(over).filter(([k]) => k !== "match")),
  };
}

export function difficulty(over: Record<string, unknown> = {}) {
  return {
    issue: { owner: "acme", repo: "ml-api", number: 7, title: "Parser crashes on load", html_url: "https://github.com/acme/ml-api/issues/7" },
    deterministic_signals: { difficulty_label_hint: { level: 2, maintainer_label: "easy", source: "maintainer_label", note: "noisy" } },
    ml_prediction: { status: "unavailable", reason: "not_configured", model: null, label: null, class_scores: {}, calibrated: false },
    fallback: { used: true, description: "No ML prediction; the maintainer-label hint (a noisy human signal) is the only difficulty indication." },
    ...over,
  };
}

export const predicted = () => ({
  status: "predicted", reason: null,
  model: { id: "ml_difficulty_lr_three_class_abc_s42", kind: "ml", label_scheme: "three_class", label_source: "llm_annotation", evaluation_status: "exploratory_below_gate" },
  label: "easy", class_scores: { easy: 0.62, medium: 0.28, hard: 0.1 }, top_score: 0.62, margin: 0.34, calibrated: false,
});

const signal = (score: number | null, status = "known") => ({
  group: "g", weight: 0.2, score, confidence: 0.6, status, effective_score: score ?? 25, evidence: ["An evidence line."], warnings: [],
});

export function report(kind: string, over: Record<string, unknown> = {}) {
  return {
    kind, score: 72, label: kind === "repository_health" ? "healthy" : "well_prepared", coverage: 0.7,
    group_scores: {}, warnings: ["A warning."], notes: ["Detected files were not executed."],
    signals: { a_signal: signal(80), b_signal: signal(null, "unknown") }, ...over,
  };
}

const scoring = { kind: "heuristic", is_ml_prediction: false, version: "intel_v1", note: "Heuristic summary. Not an ML prediction." };
const repo = { owner: "acme", name: "ml-api", full_name: "acme/ml-api" };

export function health(over: Record<string, unknown> = {}) {
  return {
    repository: repo, scoring, report: report("repository_health"),
    freshness: { collected_at: "2026-01-01T00:00:00Z", age_hours: 3, window_days: 90, head_sha: null, complete: true, stale: false, errors: {}, refresh_error: null },
    ...over,
  };
}

export function readiness(over: Record<string, unknown> = {}) {
  return {
    repository: repo, issue: { number: 7, title: "t", state: "open", html_url: "https://github.com/acme/ml-api/issues/7" }, scoring,
    report: report("issue_readiness"),
    freshness: { repository_snapshot: null, linked_prs_collected_at: null },
    linked_pull_requests: { pull_requests: [{ number: 50, state: "open", html_url: "https://github.com/acme/ml-api/pull/50" }] },
    upstream_errors: {}, ...over,
  };
}

export const savedIssue = (over: Record<string, unknown> = {}) => ({
  id: 11, saved_at: "2026-01-01T00:00:00Z", created: true, owner: "acme", repo: "ml-api", number: 7, title: "t", state: "open",
  html_url: "https://github.com/acme/ml-api/issues/7", ...over,
});

export const feedbackResult = (over: Record<string, unknown> = {}) => ({
  id: 3, type: "relevance", value: "relevant", category: "explicit_feedback", source: "user", match_id: null, owner: "acme", repo: "ml-api",
  number: 7, created_at: "2026-01-01T00:00:00Z", duplicate: false, note: null, ...over,
});

export const status = (over: Record<string, unknown> = {}) => ({
  signedIn: true, sessionEnded: false, keepSignedIn: false, sessionExpiresAt: "2026-02-01T00:00:00.000Z", backendUrl: "http://localhost:8000",
  experimentalScorer: false, ...over,
});

export const lookup = (saved: boolean, over: Record<string, unknown> = {}) => ({ saved, saved_issue: saved ? savedIssue() : null, ...over });

export const profileOut = (over: Record<string, unknown> = {}) => ({
  skills: [{ name: "python", category: "language", proficiency: 0.9, source: "self_reported" }],
  interests: ["machine learning"], contribution_goals: ["learn testing"], preferred_task_types: ["bug_fixing"],
  preferred_difficulty: 2, experience_level: "intermediate", updated_at: "2026-01-01T00:00:00Z", ...over,
});

export const ok = (data: unknown) => ({ ok: true, data });
export const fail = (kind: string, message = "Something failed.", extra: Record<string, unknown> = {}) => ({ ok: false, error: { kind, message, ...extra } });

type AnyRequest = Request | AdminRequest;
type Handler = (msg: any) => unknown | Promise<unknown>;

/** A fake background worker: routes by message type. Unlisted types fail loudly so tests notice unexpected calls. */
export function fakeSender(handlers: Partial<Record<AnyRequest["type"], Handler>>) {
  return vi.fn(async (msg: AnyRequest) => {
    const h = handlers[msg.type];
    if (!h) throw new Error(`unexpected message ${msg.type}`);
    return h(msg);
  });
}

export const defaultHandlers = (): Partial<Record<AnyRequest["type"], Handler>> => ({
  status: () => ok(status()),
  match: () => ok(issueMatch()),
  difficulty: () => ok(difficulty()),
  health: () => ok(health()),
  readiness: () => ok(readiness()),
  save: () => ok(savedIssue()),
  unsave: () => ok(null),
  feedback: () => ok(feedbackResult()),
  openOptions: () => ok(null),
  lookupSaved: () => ok(lookup(false)),
});

export const searchItem = (over: Record<string, unknown> = {}) => {
  const m = issueMatch().match;
  return { ...m, rank: 1, ...over };
};

export const matchSearch = (items: unknown[] = [searchItem()], over: Record<string, unknown> = {}) => ({
  model: { name: "heuristic_baseline", version: "v1", kind: "heuristic", is_ml_prediction: false },
  ranking_reliability: { status: "ok", reasons: [] },
  total: items.length, limit: 10, offset: 0, candidates_considered: items.length, candidates_truncated: false,
  items, upstream_errors: [], ...over,
});

export const storedMatches = (items: unknown[] = []) => ({ items, total: items.length, limit: 100, offset: 0 });
