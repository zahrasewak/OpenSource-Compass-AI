import { z } from "zod";

/**
 * Runtime validators for every backend response the extension uses. They mirror the FastAPI response models in
 * backend/app/schemas; unknown extra fields are ignored, missing or mistyped required fields are rejected so a
 * malformed or unexpected response becomes a clear error instead of a broken panel.
 */

const repoRef = z.object({ owner: z.string(), name: z.string(), full_name: z.string() });

export const componentSchema = z.object({
  score: z.number().nullable(), // null = unknown (never 0)
  status: z.string(),
  weight: z.number(),
  confidence: z.number(),
  effective_score: z.number(),
  evidence: z.array(z.string()),
  warnings: z.array(z.string()),
});
export type Component = z.infer<typeof componentSchema>;

export const matchCoreSchema = z.object({
  match_id: z.number().int().nullable(),
  generated_at: z.string().nullable(),
  repository: repoRef,
  issue: z.object({ number: z.number().int(), title: z.string(), html_url: z.string() }),
  final_score: z.number(),
  coverage: z.number(),
  insufficient_information: z.boolean(),
  components: z.record(z.string(), componentSchema),
  reasons: z.array(z.string()),
  missing_skills: z.array(z.string()),
  warnings: z.array(z.string()),
});

export const issueMatchSchema = z.object({
  model: z.object({
    name: z.string(),
    version: z.string(),
    kind: z.string(),
    is_ml_prediction: z.boolean(),
    description: z.string(),
    score_note: z.string(),
  }),
  weights: z.record(z.string(), z.number()),
  ranking_reliability: z.object({ status: z.string(), reasons: z.array(z.string()) }),
  match: matchCoreSchema,
});
export type IssueMatch = z.infer<typeof issueMatchSchema>;

/** POST /matches/search: ranked stored issues for the caller. Items are the same shape as one issue's match, plus a rank. */
export const matchSearchSchema = z.object({
  model: z.object({ name: z.string(), version: z.string(), kind: z.string(), is_ml_prediction: z.boolean() }),
  ranking_reliability: z.object({ status: z.string(), reasons: z.array(z.string()) }),
  total: z.number().int(),
  limit: z.number().int(),
  offset: z.number().int(),
  candidates_considered: z.number().int(),
  candidates_truncated: z.boolean(),
  items: z.array(matchCoreSchema.extend({ rank: z.number().int() })),
  upstream_errors: z.array(z.object({ repository: z.string(), code: z.string(), message: z.string() })),
});
export type MatchSearch = z.infer<typeof matchSearchSchema>;
export type MatchItem = MatchSearch["items"][number];

/** GET /matches: the caller's stored matches (newest first). Used to show a score next to a saved issue when one was computed. */
export const storedMatchListSchema = z.object({
  items: z.array(z.object({
    id: z.number().int(),
    generated_at: z.string(),
    model: z.object({ id: z.string() }),
    repository: repoRef,
    issue: z.object({ number: z.number().int(), title: z.string(), html_url: z.string() }),
    final_score: z.number(),
    coverage: z.number().nullable(),
    insufficient_information: z.boolean(),
  })),
  total: z.number().int(),
  limit: z.number().int(),
  offset: z.number().int(),
});
export type StoredMatchList = z.infer<typeof storedMatchListSchema>;

export const difficultySchema = z.object({
  issue: z.object({ owner: z.string(), repo: z.string(), number: z.number().int(), title: z.string(), html_url: z.string() }),
  deterministic_signals: z.object({
    difficulty_label_hint: z
      .object({ level: z.number().int(), maintainer_label: z.string(), source: z.string(), note: z.string() })
      .nullable(),
  }),
  ml_prediction: z.object({
    status: z.string(), // predicted | unsupported_input | unavailable
    reason: z.string().nullable().optional(),
    model: z
      .object({ id: z.string(), kind: z.string(), label_scheme: z.string(), label_source: z.string(), evaluation_status: z.string() })
      .nullable()
      .optional(),
    label: z.string().nullable().optional(),
    class_scores: z.record(z.string(), z.number()).default({}),
    top_score: z.number().nullable().optional(),
    margin: z.number().nullable().optional(),
    calibrated: z.boolean().default(false),
    prediction_id: z.number().int().nullable().optional(),
    generated_at: z.string().nullable().optional(),
    note: z.string().optional(),
  }),
  fallback: z.object({ used: z.boolean(), description: z.string() }),
});
export type DifficultyEstimate = z.infer<typeof difficultySchema>;

const signalSchema = z.object({
  group: z.string(),
  weight: z.number(),
  score: z.number().nullable(),
  confidence: z.number(),
  status: z.string(), // known | unknown | stale | not_applicable
  effective_score: z.number().nullable(),
  evidence: z.array(z.string()),
  warnings: z.array(z.string()),
});
export type Signal = z.infer<typeof signalSchema>;

const reportSchema = z.object({
  kind: z.string(),
  score: z.number().nullable(),
  label: z.string(),
  coverage: z.number(),
  group_scores: z.record(z.string(), z.number().nullable()),
  warnings: z.array(z.string()),
  notes: z.array(z.string()),
  signals: z.record(z.string(), signalSchema),
});
export type Report = z.infer<typeof reportSchema>;

const scoringNote = z.object({ kind: z.string(), is_ml_prediction: z.boolean(), version: z.string(), note: z.string() });

const freshnessSchema = z.object({
  collected_at: z.string().nullable(),
  age_hours: z.number().nullable(),
  window_days: z.number().nullable(),
  head_sha: z.string().nullable(),
  complete: z.boolean().nullable(),
  stale: z.boolean(),
  errors: z.record(z.string(), z.string()),
  refresh_error: z.string().nullable().optional(),
});
export type Freshness = z.infer<typeof freshnessSchema>;

export const healthSchema = z.object({
  repository: repoRef,
  scoring: scoringNote,
  report: reportSchema,
  freshness: freshnessSchema,
});
export type RepoHealth = z.infer<typeof healthSchema>;

export const readinessSchema = z.object({
  repository: repoRef,
  issue: z.object({ number: z.number().int(), title: z.string(), state: z.string(), html_url: z.string() }),
  scoring: scoringNote,
  report: reportSchema,
  freshness: z.object({ repository_snapshot: freshnessSchema.nullable(), linked_prs_collected_at: z.string().nullable() }),
  linked_pull_requests: z
    .object({
      pull_requests: z.array(
        z.object({ number: z.number().int(), state: z.string(), html_url: z.string().nullable().optional(), merged_at: z.string().nullable().optional() }),
      ),
      other_repository_references: z.number().int().optional(),
      timeline_truncated: z.boolean().optional(),
    })
    .nullable(),
  upstream_errors: z.record(z.string(), z.string()),
});
export type IssueReadiness = z.infer<typeof readinessSchema>;

export const savedIssueSchema = z.object({
  id: z.number().int(),
  saved_at: z.string(),
  created: z.boolean().nullable().optional(),
  owner: z.string(),
  repo: z.string(),
  number: z.number().int(),
  title: z.string(),
  state: z.string(),
  html_url: z.string(),
});
export type SavedIssue = z.infer<typeof savedIssueSchema>;

export const savedListSchema = z.object({
  items: z.array(savedIssueSchema),
  total: z.number().int(),
  limit: z.number().int(),
  offset: z.number().int(),
});
export type SavedList = z.infer<typeof savedListSchema>;

export const savedLookupSchema = z.object({ saved: z.boolean(), saved_issue: savedIssueSchema.nullable().optional() });
export type SavedLookup = z.infer<typeof savedLookupSchema>;

export const SKILL_CATEGORIES = ["language", "framework", "library", "skill"] as const;
export const EXPERIENCE_LEVELS = ["beginner", "intermediate", "advanced"] as const;
export const TASK_TYPES = [
  "bug_fixing", "documentation", "testing", "refactoring", "feature_development",
  "performance_optimization", "dependency_maintenance", "other",
] as const;

export const profileSchema = z.object({
  skills: z.array(z.object({ name: z.string(), category: z.string(), proficiency: z.number(), source: z.string().optional() })),
  interests: z.array(z.string()),
  contribution_goals: z.array(z.string()),
  preferred_task_types: z.array(z.string()),
  preferred_difficulty: z.number().int().nullable(),
  experience_level: z.string().nullable(),
  updated_at: z.string(),
});
export type Profile = z.infer<typeof profileSchema>;

/** What the extension may send when editing a profile (mirrors backend ProfileIn; the server validates again). */
export const profileInputSchema = z.object({
  skills: z.array(z.object({ name: z.string().min(1).max(100), category: z.enum(SKILL_CATEGORIES), proficiency: z.number().min(0).max(1) })).max(100),
  interests: z.array(z.string().min(1).max(100)).max(50),
  contribution_goals: z.array(z.string().min(1).max(100)).max(20),
  preferred_task_types: z.array(z.enum(TASK_TYPES)),
  preferred_difficulty: z.number().int().min(1).max(5).nullable(),
  experience_level: z.enum(EXPERIENCE_LEVELS).nullable(),
});
export type ProfileInput = z.infer<typeof profileInputSchema>;

export const feedbackSchema = z.object({
  id: z.number().int(),
  type: z.string(),
  value: z.string(),
  category: z.string(),
  source: z.string(),
  match_id: z.number().int().nullable(),
  owner: z.string(),
  repo: z.string(),
  number: z.number().int(),
  created_at: z.string(),
  duplicate: z.boolean(),
  note: z.string().nullable().optional(),
});
export type FeedbackResult = z.infer<typeof feedbackSchema>;

export const meSchema = z.object({ id: z.number().int(), github_user_id: z.number().int(), username: z.string() });
export type Me = z.infer<typeof meSchema>;

export const errorEnvelopeSchema = z.object({
  error: z.object({ code: z.string(), message: z.string(), details: z.unknown().optional() }),
});

export const FEEDBACK_TYPES = ["relevance", "difficulty", "interest"] as const;
export type FeedbackType = (typeof FEEDBACK_TYPES)[number];
export const FEEDBACK_VALUES: Record<FeedbackType, readonly string[]> = {
  relevance: ["relevant", "not_relevant"],
  difficulty: ["too_easy", "appropriate", "too_difficult"],
  interest: ["interested", "not_interested"],
};
