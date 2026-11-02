import { z } from "zod";
import { FEEDBACK_TYPES, FEEDBACK_VALUES, profileInputSchema } from "./schemas";
import { isValidIssueRef } from "./url";

/**
 * Messages the content script (panel) may send to the background worker, and a separate set only the extension's own
 * pages (the options page) may send. Everything is validated again on receipt, and the handler decides which set applies
 * from the sender, so a message cannot be promoted to the other set.
 */

const issueRef = z
  .object({ owner: z.string().max(100), repo: z.string().max(100), number: z.number().int() })
  .refine(isValidIssueRef, "invalid issue reference");

const feedback = z
  .object({
    type: z.literal("feedback"),
    ref: issueRef,
    feedbackType: z.enum(FEEDBACK_TYPES),
    value: z.string().max(40),
    matchId: z.number().int().positive().nullable().optional(),
  })
  .refine((m) => FEEDBACK_VALUES[m.feedbackType].includes(m.value), "value does not match the feedback type");

export const requestSchema = z.union([
  z.object({ type: z.literal("status") }),
  z.object({ type: z.literal("openOptions") }),
  z.object({ type: z.literal("match"), ref: issueRef }),
  z.object({ type: z.literal("difficulty"), ref: issueRef }),
  z.object({ type: z.literal("health"), ref: issueRef }),
  z.object({ type: z.literal("readiness"), ref: issueRef }),
  z.object({ type: z.literal("save"), ref: issueRef, matchId: z.number().int().positive().nullable().optional() }),
  z.object({ type: z.literal("unsave"), savedId: z.number().int().positive() }),
  z.object({ type: z.literal("lookupSaved"), ref: issueRef }),
  feedback,
]);
export type Request = z.infer<typeof requestSchema>;
export type RequestType = Request["type"];

/** Messages from the extension's own options page only (never accepted from a tab showing github.com). */
export const adminRequestSchema = z.union([
  z.object({ type: z.literal("auth.state") }),
  z.object({ type: z.literal("auth.connect"), code: z.string().trim().min(1).max(200), keepSignedIn: z.boolean() }),
  z.object({ type: z.literal("auth.signOut") }),
  z.object({ type: z.literal("auth.signOutAll") }),
  z.object({ type: z.literal("auth.me") }),
  z.object({ type: z.literal("settings.save"), baseUrl: z.string().max(300), useExperimentalScorer: z.boolean() }),
  z.object({ type: z.literal("profile.get") }),
  z.object({ type: z.literal("profile.save"), profile: profileInputSchema, exists: z.boolean() }),
  z.object({ type: z.literal("saved.list"), limit: z.number().int().min(1).max(100), offset: z.number().int().min(0) }),
  z.object({ type: z.literal("saved.remove"), savedId: z.number().int().positive() }),
  z.object({
    type: z.literal("matches.search"),
    language: z.string().trim().min(1).max(60).optional(),
    topic: z.string().trim().min(1).max(60).optional(),
    preferredDifficulty: z.number().int().min(1).max(5).optional(),
    repository: z.string().regex(/^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}$/).optional(),
    limit: z.number().int().min(1).max(50),
    offset: z.number().int().min(0),
  }),
  z.object({ type: z.literal("matches.stored") }),
  z.object({ type: z.literal("issue.save"), ref: issueRef }),
  z.object({ type: z.literal("issue.difficulty"), ref: issueRef }),
]);
export type AdminRequest = z.infer<typeof adminRequestSchema>;

export type ErrorKind =
  | "not_configured"
  | "auth"
  | "forbidden"
  | "not_found"
  | "invalid_request"
  | "rate_limited"
  | "server"
  | "network"
  | "timeout"
  | "malformed"
  | "invalid_config"
  | "extension"
  | "rejected"
  | "unknown";

export interface ErrorInfo {
  kind: ErrorKind;
  message: string;
  status?: number;
  retryAfterSeconds?: number;
  /** Short "field: message" lines describing the user's own rejected input (validation errors). */
  details?: string[];
}

export type Response<T = unknown> = { ok: true; data: T } | { ok: false; error: ErrorInfo };

export const responseSchema = z.union([
  z.object({ ok: z.literal(true), data: z.unknown() }),
  z.object({
    ok: z.literal(false),
    error: z.object({
      kind: z.string(),
      message: z.string(),
      status: z.number().optional(),
      retryAfterSeconds: z.number().optional(),
      details: z.array(z.string()).optional(),
    }),
  }),
]);

/** What the panel and options page may know about the session. Never contains a token. */
export const statusSchema = z.object({
  signedIn: z.boolean(),
  sessionEnded: z.boolean(),
  keepSignedIn: z.boolean(),
  sessionExpiresAt: z.string().nullable(),
  backendUrl: z.string(),
  experimentalScorer: z.boolean(),
});
export type Status = z.infer<typeof statusSchema>;
