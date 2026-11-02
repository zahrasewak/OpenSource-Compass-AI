import { ApiError, validateBaseUrl, type Api } from "../shared/api";
import { adminRequestSchema, requestSchema, type AdminRequest, type ErrorInfo, type Request, type Response } from "../shared/messages";
import type { SessionState, Settings } from "../shared/session";
import { parseIssueUrl, type IssueRef } from "../shared/url";
import type { ResponseCache } from "./cache";

export interface SenderLike {
  id?: string;
  /** URL of the frame that sent the message (a github.com page for the content script, chrome-extension://… for our own pages). */
  url?: string;
  tab?: { url?: string };
}

/** The part of SessionManager the handler needs (an interface so tests can supply a fake). */
export interface SessionLike {
  getState(): Promise<SessionState>;
  getSettings(): Promise<Settings>;
  saveSettings(s: Settings): Promise<void>;
  connect(code: string, keepSignedIn: boolean): Promise<SessionState>;
  signOut(): Promise<void>;
}

export interface HandlerDeps {
  api: Api;
  session: SessionLike;
  cache: ResponseCache;
  extensionId: string;
  openOptions: () => void;
}

/** How long answers are reused before asking the backend again. Health/readiness are the expensive (GitHub-backed) ones. */
export const TTL = { match: 5 * 60_000, difficulty: 5 * 60_000, health: 10 * 60_000, readiness: 10 * 60_000, matchId: 30 * 60_000 } as const;

const reject = (message: string): Response => ({ ok: false, error: { kind: "rejected", message } });

function toErrorInfo(err: unknown): ErrorInfo {
  if (err instanceof ApiError) {
    return { kind: err.kind, message: err.message, status: err.status, retryAfterSeconds: err.retryAfterSeconds, details: err.details };
  }
  // Never forward arbitrary error text (it could contain request details).
  return { kind: "unknown", message: "Unexpected extension error." };
}

const sameIssue = (a: IssueRef, b: IssueRef) =>
  a.number === b.number && a.owner.toLowerCase() === b.owner.toLowerCase() && a.repo.toLowerCase() === b.repo.toLowerCase();

const refKey = (r: IssueRef) => `${r.owner.toLowerCase()}/${r.repo.toLowerCase()}#${r.number}`;
const repoKey = (r: IssueRef) => `${r.owner.toLowerCase()}/${r.repo.toLowerCase()}`;

/**
 * Handle one message. Tokens are used here (through the session manager) and nowhere else.
 *
 * Two audiences, told apart by the sender and never by anything in the message:
 *  - the extension's own pages (options): account, profile and saved-issue management
 *  - a tab showing a GitHub issue page (content script): analysis for that one issue, save/unsave, feedback
 * Message bodies are validated against the schema for the audience, never trusted.
 */
export async function handleMessage(raw: unknown, sender: SenderLike, deps: HandlerDeps): Promise<Response> {
  if (sender.id !== deps.extensionId) return reject("Messages are only accepted from this extension.");
  if (sender.url?.startsWith(`chrome-extension://${deps.extensionId}/`)) return handleAdmin(raw, deps);

  const tabIssue = sender.tab?.url ? parseIssueUrl(sender.tab.url) : null;
  if (!tabIssue) return reject("Messages are only accepted from a GitHub issue page.");

  const parsed = requestSchema.safeParse(raw);
  if (!parsed.success) return reject("Invalid message.");
  const msg = parsed.data;
  if ("ref" in msg && !sameIssue(msg.ref, tabIssue)) return reject("The message does not match the issue on this page.");

  try {
    return { ok: true, data: await handlePanel(msg, deps) };
  } catch (err) {
    return { ok: false, error: toErrorInfo(err) };
  }
}

async function handlePanel(msg: Request, deps: HandlerDeps): Promise<unknown> {
  const { api, cache, session } = deps;
  switch (msg.type) {
    case "status":
      return session.getState();
    case "openOptions":
      deps.openOptions();
      return null;
    case "match": {
      // Display only: viewing a page does not write a match record. One is stored when you act (save / feedback).
      const scorer = (await session.getSettings()).useExperimentalScorer ? "v2" : "v1";
      return cache.get(`match:${scorer}:${refKey(msg.ref)}`, TTL.match, () => api.getIssueMatch(msg.ref, { persist: false }));
    }
    case "difficulty":
      return cache.get(`difficulty:${refKey(msg.ref)}`, TTL.difficulty, () => api.getDifficulty(msg.ref));
    case "health":
      // Keyed by repository: every issue of one repository shares a single analysis.
      return cache.get(`health:${repoKey(msg.ref)}`, TTL.health, () => api.getHealth(msg.ref));
    case "readiness":
      return cache.get(`readiness:${refKey(msg.ref)}`, TTL.readiness, () => api.getReadiness(msg.ref));
    case "lookupSaved":
      // Backend-only (never touches GitHub). Not stored, but concurrent identical calls are shared.
      return cache.get(`saved:${refKey(msg.ref)}`, 0, () => api.lookupSaved(msg.ref));
    case "save":
      return api.saveIssue(msg.ref, msg.matchId ?? (await ensureMatchId(deps, msg.ref)));
    case "unsave":
      await api.unsaveIssue(msg.savedId);
      return null;
    case "feedback":
      return api.submitFeedback(msg.ref, msg.feedbackType, msg.value, msg.matchId ?? (await ensureMatchId(deps, msg.ref)));
  }
}

async function handleAdmin(raw: unknown, deps: HandlerDeps): Promise<Response> {
  const parsed = adminRequestSchema.safeParse(raw);
  if (!parsed.success) return reject("Invalid message.");
  try {
    return { ok: true, data: await runAdmin(parsed.data, deps) };
  } catch (err) {
    return { ok: false, error: toErrorInfo(err) };
  }
}

async function runAdmin(msg: AdminRequest, deps: HandlerDeps): Promise<unknown> {
  const { api, session, cache } = deps;
  switch (msg.type) {
    case "auth.state":
      return session.getState();
    case "auth.connect": {
      const state = await session.connect(msg.code, msg.keepSignedIn);
      cache.clear();
      return state;
    }
    case "auth.signOutAll": {
      // End every session of the account on the server, then ALWAYS wipe this browser, even if the server call failed.
      let failure: unknown = null;
      try {
        await api.logoutAll();
      } catch (e) {
        failure = e;
      }
      await session.signOut();
      cache.clear();
      if (failure) throw failure; // the page then says this browser is signed out but other sessions may remain
      return session.getState();
    }
    case "auth.signOut":
      await session.signOut();
      cache.clear();
      return session.getState();
    case "auth.me":
      return api.getMe();
    case "settings.save": {
      const before = await session.getSettings();
      const baseUrl = validateBaseUrl(msg.baseUrl);
      await session.saveSettings({ ...before, baseUrl, useExperimentalScorer: msg.useExperimentalScorer });
      cache.clear();
      return session.getState();
    }
    case "profile.get":
      return api.getProfile();
    case "profile.save": {
      const saved = await api.saveProfile(msg.profile, msg.exists);
      cache.clear(); // scores depend on the profile, so earlier answers are stale
      return saved;
    }
    case "saved.list":
      return api.listSaved(msg.limit, msg.offset);
    case "saved.remove":
      await api.unsaveIssue(msg.savedId);
      return null;
    case "matches.search":
      // Not cached: the user changes filters and expects a fresh ranking. Backend-only unless a repository is named.
      return api.searchMatches({
        language: msg.language, topic: msg.topic, preferredDifficulty: msg.preferredDifficulty, repository: msg.repository,
        limit: msg.limit, offset: msg.offset,
      });
    case "matches.stored":
      return api.listStoredMatches(100);
    case "issue.save":
      return api.saveIssue(msg.ref, await ensureMatchId(deps, msg.ref));
    case "issue.difficulty":
      return cache.get(`difficulty:${refKey(msg.ref)}`, TTL.difficulty, () => api.getDifficulty(msg.ref));
  }
}

/**
 * Feedback and saves are linked to the stored match so the scoring method and inputs can be evaluated later. The match
 * is stored only now, because the user acted, and the id is remembered so several actions on one page cost one request.
 * If this step fails (other than for authentication) the action proceeds without a link: the backend accepts an issue
 * reference instead.
 */
async function ensureMatchId(deps: HandlerDeps, ref: IssueRef): Promise<number | null> {
  const key = `matchId:${refKey(ref)}`;
  const known = deps.cache.peek<number>(key);
  if (known !== undefined) return known;
  try {
    const id = (await deps.api.getIssueMatch(ref, { persist: true })).match.match_id;
    if (id !== null) deps.cache.put(key, id, TTL.matchId);
    return id;
  } catch (err) {
    if (err instanceof ApiError && (err.kind === "auth" || err.kind === "not_configured" || err.kind === "rate_limited")) throw err;
    return null;
  }
}
