import type { ZodType } from "zod";
import {
  difficultySchema,
  errorEnvelopeSchema,
  feedbackSchema,
  healthSchema,
  issueMatchSchema,
  matchSearchSchema,
  meSchema,
  profileSchema,
  readinessSchema,
  savedIssueSchema,
  savedListSchema,
  savedLookupSchema,
  storedMatchListSchema,
  type DifficultyEstimate,
  type FeedbackResult,
  type FeedbackType,
  type IssueMatch,
  type IssueReadiness,
  type MatchSearch,
  type Me,
  type Profile,
  type ProfileInput,
  type RepoHealth,
  type SavedIssue,
  type SavedList,
  type SavedLookup,
  type StoredMatchList,
} from "./schemas";
import { tokenSchema, type TokenPair } from "./authSchemas";
import { BACKEND_PINNED, DEFAULT_BACKEND_URL } from "./buildinfo";
import { isValidIssueRef, type IssueRef } from "./url";

export interface ApiConfig {
  baseUrl: string;
  token: string | null;
  useExperimentalScorer: boolean;
}

export type ApiErrorKind =
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
  | "unknown";

export class ApiError extends Error {
  constructor(
    public readonly kind: ApiErrorKind,
    message: string,
    public readonly status?: number,
    public readonly code?: string,
    public readonly retryAfterSeconds?: number,
    public readonly details?: string[],
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);
const MAX_SERVER_MESSAGE = 300;

/**
 * The backend origin credentials may be sent to. https is required except for localhost development, and the value must
 * be a bare origin (no credentials, path, query or fragment) so a token cannot be steered to another host or path.
 */
export function validateBaseUrl(raw: string): string {
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    throw new ApiError("invalid_config", "The backend URL is not a valid URL.");
  }
  const local = LOCAL_HOSTS.has(url.hostname) && !BACKEND_PINNED; // a production build never talks to localhost
  if (url.protocol !== "https:" && !(url.protocol === "http:" && local)) {
    throw new ApiError("invalid_config", BACKEND_PINNED ? "This build only talks to its configured https backend." : "The backend URL must use https (http is only allowed for localhost).");
  }
  if (BACKEND_PINNED && url.origin !== DEFAULT_BACKEND_URL) {
    throw new ApiError("invalid_config", `This build is pinned to ${DEFAULT_BACKEND_URL} and cannot use another backend.`);
  }
  if (url.username || url.password) throw new ApiError("invalid_config", "The backend URL must not contain credentials.");
  if ((url.pathname !== "/" && url.pathname !== "") || url.search || url.hash) {
    throw new ApiError("invalid_config", BACKEND_PINNED ? "Enter only the backend origin (no path, query or fragment)." : "Enter only the backend origin, for example https://compass.example.com or http://localhost:8000.");
  }
  return url.origin;
}

// ---------------------------------------------------------------------------------------------- low level

export interface SendOptions {
  method: "GET" | "POST" | "PUT" | "DELETE";
  token?: string | null;
  body?: unknown;
  timeoutMs: number;
  fetchImpl?: typeof fetch;
}

/** One HTTP call with the extension's fixed safety settings. Throws ApiError for network failures and timeouts only. */
export async function send(baseUrl: string, path: string, o: SendOptions): Promise<Response> {
  const origin = validateBaseUrl(baseUrl);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), o.timeoutMs);
  try {
    return await (o.fetchImpl ?? fetch)(origin + path, {
      method: o.method,
      headers: {
        Accept: "application/json",
        ...(o.token ? { Authorization: `Bearer ${o.token}` } : {}),
        ...(o.body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: o.body !== undefined ? JSON.stringify(o.body) : undefined,
      signal: controller.signal,
      credentials: "omit", // no cookies: identity comes only from the bearer token
      redirect: "error", // never follow a redirect with credentials attached
      cache: "no-store",
    });
  } catch {
    if (controller.signal.aborted) throw new ApiError("timeout", "The backend did not answer in time.");
    // The underlying error text is not included: it can echo request details.
    throw new ApiError("network", "Could not reach the backend. Is it running and is the URL correct?");
  } finally {
    clearTimeout(timer);
  }
}

export async function parse<T>(response: Response, schema: ZodType<T>): Promise<T> {
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ApiError("malformed", "The backend returned a response that is not valid JSON.", response.status);
  }
  const parsed = schema.safeParse(payload);
  if (!parsed.success) {
    // Field paths only; never payload content.
    const fields = [...new Set(parsed.error.issues.map((i) => i.path.join(".") || "(root)"))].slice(0, 5).join(", ");
    throw new ApiError("malformed", `The backend response had an unexpected shape (${fields}).`, response.status);
  }
  return parsed.data;
}

export async function toApiError(response: Response, authMessage = "Your session is invalid or has expired."): Promise<ApiError> {
  let code: string | undefined;
  let message: string | undefined;
  let details: string[] | undefined;
  try {
    const parsed = errorEnvelopeSchema.safeParse(await response.json());
    if (parsed.success) {
      code = parsed.data.error.code;
      message = parsed.data.error.message.slice(0, MAX_SERVER_MESSAGE);
      details = describeDetails(parsed.data.error.details);
    }
  } catch {
    /* non-JSON error body: fall back to the status below */
  }
  const s = response.status;
  if (s === 401) return new ApiError("auth", authMessage, s, code);
  if (s === 403) return new ApiError("forbidden", message ?? "Access was denied.", s, code);
  if (s === 404) return new ApiError("not_found", message ?? "Not found.", s, code);
  if (s === 422) return new ApiError("invalid_request", message ?? "The request was rejected as invalid.", s, code, undefined, details);
  if (s === 429) {
    const header = Number(response.headers.get("retry-after"));
    return new ApiError("rate_limited", message ?? "Rate limited. Try again later.", s, code, Number.isFinite(header) && header > 0 ? header : undefined);
  }
  if (s >= 500) return new ApiError("server", message ?? "The backend or GitHub is temporarily unavailable.", s, code);
  return new ApiError("unknown", message ?? `Unexpected response (${s}).`, s, code);
}

/** FastAPI validation details -> short "field: message" strings (these describe the user's own input, never secrets). */
function describeDetails(details: unknown): string[] | undefined {
  if (!Array.isArray(details)) return undefined;
  const out = details.slice(0, 5).flatMap((d) => {
    if (typeof d !== "object" || d === null) return [];
    const { loc, msg } = d as { loc?: unknown; msg?: unknown };
    if (typeof msg !== "string") return [];
    const where = Array.isArray(loc) ? loc.filter((p) => p !== "body").join(".") : "";
    return [`${where ? where + ": " : ""}${msg}`.slice(0, 200)];
  });
  return out.length ? out : undefined;
}

// ---------------------------------------------------------------------------------------------- authenticated client

export interface ApiOptions {
  getConfig: () => Promise<ApiConfig>;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  /** Called once if a request is answered 401. Return true after obtaining a new token to retry exactly once. */
  onUnauthorized?: (usedToken: string) => Promise<boolean>;
}

const SLOW_TIMEOUT_MS = 90_000; // repository health may make ~10-16 GitHub requests on first use

export interface MatchFilters {
  language?: string;
  topic?: string;
  preferredDifficulty?: number;
  /** owner/name; makes the backend look the repository up on GitHub first (uses GitHub quota). */
  repository?: string;
  limit: number;
  offset: number;
}

export function createApi({ getConfig, fetchImpl, timeoutMs = 30_000, onUnauthorized }: ApiOptions) {
  async function request<T>(
    method: SendOptions["method"],
    path: string,
    schema: ZodType<T> | null,
    body?: unknown,
    timeout = timeoutMs,
  ): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      const config = await getConfig();
      if (!config.token) throw new ApiError("not_configured", "You are not signed in.");
      const response = await send(config.baseUrl, path, { method, token: config.token, body, timeoutMs: timeout, fetchImpl });
      if (response.status === 401 && attempt === 0 && onUnauthorized && (await onUnauthorized(config.token))) continue; // one retry, never a loop
      if (!response.ok) throw await toApiError(response);
      return schema === null ? (undefined as T) : parse(response, schema);
    }
  }

  const bad = () => new ApiError("invalid_request", "That is not a valid GitHub issue reference.");
  const issuePath = (ref: IssueRef) => {
    if (!isValidIssueRef(ref)) throw bad();
    return `/api/v1/issues/${encodeURIComponent(ref.owner)}/${encodeURIComponent(ref.repo)}/${ref.number}`;
  };

  return {
    getMe: (): Promise<Me> => request("GET", "/api/v1/auth/me", meSchema),

    async getIssueMatch(ref: IssueRef, opts: { persist: boolean }): Promise<IssueMatch> {
      const config = await getConfig();
      return request("POST", `${issuePath(ref)}/match`, issueMatchSchema, {
        scorer: config.useExperimentalScorer ? "heuristic_v2" : "heuristic_v1",
        persist: opts.persist,
      });
    },
    getDifficulty: (ref: IssueRef): Promise<DifficultyEstimate> => request("POST", `${issuePath(ref)}/difficulty`, difficultySchema),
    getReadiness: (ref: IssueRef): Promise<IssueReadiness> => request("GET", `${issuePath(ref)}/readiness`, readinessSchema, undefined, SLOW_TIMEOUT_MS),
    getHealth(ref: IssueRef): Promise<RepoHealth> {
      if (!isValidIssueRef(ref)) throw bad();
      return request("GET", `/api/v1/repositories/${encodeURIComponent(ref.owner)}/${encodeURIComponent(ref.repo)}/health`, healthSchema, undefined, SLOW_TIMEOUT_MS);
    },

    saveIssue(ref: IssueRef, matchId?: number | null): Promise<SavedIssue> {
      if (!isValidIssueRef(ref)) throw bad();
      return request("POST", "/api/v1/saved-issues", savedIssueSchema, {
        owner: ref.owner, repo: ref.repo, number: ref.number, ...(matchId ? { match_id: matchId } : {}),
      });
    },
    async unsaveIssue(savedId: number): Promise<void> {
      if (!Number.isInteger(savedId) || savedId < 1) throw new ApiError("invalid_request", "Invalid saved-issue id.");
      await request<void>("DELETE", `/api/v1/saved-issues/${savedId}`, null);
    },
    lookupSaved(ref: IssueRef): Promise<SavedLookup> {
      if (!isValidIssueRef(ref)) throw bad();
      return request("GET", `/api/v1/saved-issues/lookup/${encodeURIComponent(ref.owner)}/${encodeURIComponent(ref.repo)}/${ref.number}`, savedLookupSchema);
    },
    /** Rank issues the backend already stores (and, if `repository` is given, that repository after a GitHub sync). */
    async searchMatches(f: MatchFilters): Promise<MatchSearch> {
      const config = await getConfig();
      return request("POST", "/api/v1/matches/search", matchSearchSchema, {
        scorer: config.useExperimentalScorer ? "heuristic_v2" : "heuristic_v1",
        persist: false, // browsing writes nothing; a match is stored when the user saves or sends feedback
        limit: f.limit, offset: f.offset,
        ...(f.language ? { language: f.language } : {}),
        ...(f.topic ? { topic: f.topic } : {}),
        ...(f.preferredDifficulty ? { preferred_difficulty: f.preferredDifficulty } : {}),
        ...(f.repository ? { repositories: [f.repository] } : {}),
      }, f.repository ? SLOW_TIMEOUT_MS : undefined);
    },
    listStoredMatches(limit = 100): Promise<StoredMatchList> {
      if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new ApiError("invalid_request", "Invalid paging.");
      return request("GET", `/api/v1/matches?limit=${limit}`, storedMatchListSchema);
    },
    listSaved(limit = 20, offset = 0): Promise<SavedList> {
      if (!Number.isInteger(limit) || limit < 1 || limit > 100 || !Number.isInteger(offset) || offset < 0) throw new ApiError("invalid_request", "Invalid paging.");
      return request("GET", `/api/v1/saved-issues?limit=${limit}&offset=${offset}`, savedListSchema);
    },

    /** End EVERY session of this account (all browsers and devices). Needs a valid session; the caller also wipes local credentials. */
    async logoutAll(): Promise<void> {
      await request<void>("POST", "/api/v1/auth/logout-all", null);
    },

    submitFeedback(ref: IssueRef, type: FeedbackType, value: string, matchId?: number | null): Promise<FeedbackResult> {
      if (!isValidIssueRef(ref)) throw bad();
      return request("POST", "/api/v1/feedback", feedbackSchema, {
        type, value, ...(matchId ? { match_id: matchId } : { owner: ref.owner, repo: ref.repo, number: ref.number }),
      });
    },

    /** The caller's profile, or null if none exists yet (a normal state, not an error). */
    async getProfile(): Promise<Profile | null> {
      try {
        return await request("GET", "/api/v1/profile", profileSchema);
      } catch (e) {
        if (e instanceof ApiError && e.kind === "not_found" && e.code === "profile_not_found") return null;
        throw e;
      }
    },
    saveProfile: (input: ProfileInput, exists: boolean): Promise<Profile> =>
      request(exists ? "PUT" : "POST", "/api/v1/profile", profileSchema, input),
  };
}

export type Api = ReturnType<typeof createApi>;

// ---------------------------------------------------------------------------------------------- unauthenticated auth calls

const AUTH_TIMEOUT_MS = 15_000;
const PAIRING_CODE_REJECTED = "The pairing code is invalid, expired or already used.";

async function authPost(baseUrl: string, path: string, body: unknown, fetchImpl?: typeof fetch): Promise<Response> {
  return send(baseUrl, path, { method: "POST", body, timeoutMs: AUTH_TIMEOUT_MS, fetchImpl });
}

/** Trade a one-time pairing code for tokens. A rejected code is an `auth` error with a fixed message. */
export async function exchangeCode(
  baseUrl: string, code: string, label: string | null, fetchImpl?: typeof fetch, persistent = false,
): Promise<TokenPair> {
  // `persistent` tells the server the refresh token will be kept on disk, so it applies its tighter session limits.
  const r = await authPost(baseUrl, "/api/v1/auth/exchange", { code: code.trim(), ...(label ? { client_label: label } : {}), persistent }, fetchImpl);
  // The backend answers 422 for a code of the wrong shape (e.g. mistyped/too short) and 401 for a well-formed unknown, used or expired one.
  if (r.status === 401 || r.status === 422) throw new ApiError("auth", PAIRING_CODE_REJECTED, r.status);
  if (!r.ok) throw await toApiError(r);
  return parse(r, tokenSchema);
}

export async function refreshTokens(baseUrl: string, refreshToken: string, fetchImpl?: typeof fetch): Promise<TokenPair> {
  const r = await authPost(baseUrl, "/api/v1/auth/refresh", { refresh_token: refreshToken }, fetchImpl);
  if (!r.ok) throw await toApiError(r, "Your session has ended. Sign in again.");
  return parse(r, tokenSchema);
}

/** Best-effort server-side logout with the refresh token (works even when the access token has expired). */
export async function logoutRemote(baseUrl: string, refreshToken: string, fetchImpl?: typeof fetch): Promise<void> {
  const r = await authPost(baseUrl, "/api/v1/auth/logout", { refresh_token: refreshToken }, fetchImpl);
  if (!r.ok && r.status !== 401) throw await toApiError(r);
}
