import { describe, expect, it, vi } from "vitest";
import { handleMessage, type HandlerDeps, type SenderLike, type SessionLike } from "../src/background/handler";
import { ResponseCache } from "../src/background/cache";
import { ApiError, type Api } from "../src/shared/api";
import type { SessionState, Settings } from "../src/shared/session";
import { ISSUE, issueMatch, lookup, matchSearch, profileOut, savedIssue, storedMatches } from "./fixtures";

const EXT = "ext-id";
const TAB = { url: "https://github.com/acme/ml-api/issues/7#issuecomment-1" };
const TOKEN = "SECRET.TOKEN.VALUE";
const OPTIONS_URL = `chrome-extension://${EXT}/options.html`;

const STATE: SessionState = {
  signedIn: true, sessionEnded: false, keepSignedIn: false, sessionExpiresAt: "2026-02-01T00:00:00.000Z",
  backendUrl: "http://localhost:8000", experimentalScorer: false,
};

function fakeSession(state: Partial<SessionState> = {}, settings: Partial<Settings> = {}) {
  const s = { baseUrl: "http://localhost:8000", useExperimentalScorer: false, keepSignedIn: false, ...settings };
  const session = {
    getState: vi.fn(async () => ({ ...STATE, ...state })),
    getSettings: vi.fn(async () => s),
    saveSettings: vi.fn(async () => undefined),
    connect: vi.fn(async () => ({ ...STATE, ...state })),
    signOut: vi.fn(async () => undefined),
  };
  return session satisfies SessionLike;
}

function deps(over: Partial<Record<keyof Api, unknown>> = {}, state: Partial<SessionState> = {}) {
  const api = {
    getMe: vi.fn(), getDifficulty: vi.fn(async () => ({ d: 1 })), getReadiness: vi.fn(async () => ({ r: 1 })), getHealth: vi.fn(async () => ({ h: 1 })),
    getIssueMatch: vi.fn(async (_r: unknown, o: { persist: boolean }) => ({ ...issueMatch(), match: { ...issueMatch().match, match_id: o.persist ? 42 : null } })),
    saveIssue: vi.fn(async () => savedIssue()), unsaveIssue: vi.fn(async () => undefined), submitFeedback: vi.fn(async () => ({ f: 1 })),
    lookupSaved: vi.fn(async () => lookup(false)), logoutAll: vi.fn(async () => undefined), getProfile: vi.fn(async () => profileOut()), saveProfile: vi.fn(async () => profileOut()),
    listSaved: vi.fn(async () => ({ items: [], total: 0, limit: 20, offset: 0 })),
    searchMatches: vi.fn(async () => matchSearch()), listStoredMatches: vi.fn(async () => storedMatches()),
    ...over,
  };
  const openOptions = vi.fn();
  const session = fakeSession(state);
  const d: HandlerDeps = { api: api as unknown as Api, session, cache: new ResponseCache(), extensionId: EXT, openOptions };
  return { d, session, api: api as unknown as Record<keyof Api, ReturnType<typeof vi.fn>>, openOptions };
}
const send = (msg: unknown, d: HandlerDeps, sender: SenderLike = { id: EXT, tab: TAB }) => handleMessage(msg, sender, d);
const msg = { type: "match", ref: ISSUE };

describe("who may talk to the background worker", () => {
  it("rejects other extensions and non-tab senders", async () => {
    const { d, api } = deps();
    for (const sender of [{ id: "other", tab: TAB }, { id: EXT }, { tab: TAB }, { id: EXT, tab: {} }]) {
      expect(await send(msg, d, sender)).toMatchObject({ ok: false, error: { kind: "rejected" } });
    }
    expect(api.getIssueMatch).not.toHaveBeenCalled();
  });

  it("rejects senders that are not on a GitHub issue page", async () => {
    const { d } = deps();
    for (const url of ["https://github.com/acme/ml-api/pull/7", "https://github.com/acme/ml-api", "https://evil.example/acme/ml-api/issues/7", "http://github.com/acme/ml-api/issues/7"]) {
      expect(await send(msg, d, { id: EXT, tab: { url } })).toMatchObject({ ok: false, error: { kind: "rejected" } });
    }
  });

  it("rejects a message that names a different issue than the page shows", async () => {
    const { d, api } = deps();
    const r = await send({ type: "health", ref: { owner: "other", repo: "repo", number: 1 } }, d);
    expect(r).toMatchObject({ ok: false, error: { kind: "rejected" } });
    expect(api.getHealth).not.toHaveBeenCalled();
    // owner/repo matching is case-insensitive like GitHub itself
    expect((await send({ type: "health", ref: { owner: "ACME", repo: "ML-API", number: 7 } }, d)).ok).toBe(true);
  });

  it.each([
    null, undefined, "match", 5, {}, { type: "nope" }, { type: "match" }, { type: "match", ref: { owner: "acme", repo: "ml-api", number: "7" } },
    { type: "match", ref: { owner: "acme", repo: "..", number: 7 } }, { type: "unsave", savedId: -1 }, { type: "unsave", savedId: "1" },
    { type: "feedback", ref: ISSUE, feedbackType: "relevance", value: "too_easy" },     // value does not belong to the type
    { type: "feedback", ref: ISSUE, feedbackType: "merged", value: "yes" },             // system-only types cannot be submitted
    { type: "feedback", ref: ISSUE, feedbackType: "contributed", value: "yes" },           // self-reported events are not offered
  ])("rejects malformed message %#", async (bad) => {
    const { d } = deps();
    expect(await send(bad, d)).toMatchObject({ ok: false, error: { kind: "rejected", message: "Invalid message." } });
  });
});

describe("handling messages", () => {
  it("ignores identity fields smuggled into a message (identity comes only from the bearer token)", async () => {
    const { d, api } = deps();
    const r = await send({ type: "feedback", ref: ISSUE, feedbackType: "relevance", value: "relevant", userId: 5, user_id: 5 }, d);
    expect(r.ok).toBe(true);
    expect(JSON.stringify(api.submitFeedback.mock.calls)).not.toContain("5,");
    expect(api.submitFeedback).toHaveBeenCalledWith(ISSUE, "relevance", "relevant", 42);
  });

  it("status reports the session state and has no place to put a token", async () => {
    const { d } = deps();
    expect(await send({ type: "status" }, d)).toEqual({ ok: true, data: STATE });
    const { d: d2 } = deps({}, { signedIn: false, sessionEnded: true });
    expect(await send({ type: "status" }, d2)).toMatchObject({ ok: true, data: { signedIn: false, sessionEnded: true } });
  });

  it("match is a display-only request (nothing is stored)", async () => {
    const { d, api } = deps();
    const r = await send(msg, d);
    expect(r.ok).toBe(true);
    expect(api.getIssueMatch).toHaveBeenCalledWith(ISSUE, { persist: false });
  });

  it("routes difficulty, health, readiness, unsave and openOptions", async () => {
    const { d, api, openOptions } = deps();
    expect(await send({ type: "difficulty", ref: ISSUE }, d)).toEqual({ ok: true, data: { d: 1 } });
    expect(await send({ type: "health", ref: ISSUE }, d)).toEqual({ ok: true, data: { h: 1 } });
    expect(await send({ type: "readiness", ref: ISSUE }, d)).toEqual({ ok: true, data: { r: 1 } });
    expect(await send({ type: "unsave", savedId: 11 }, d)).toEqual({ ok: true, data: null });
    expect(api.unsaveIssue).toHaveBeenCalledWith(11);
    await send({ type: "openOptions" }, d);
    expect(openOptions).toHaveBeenCalledOnce();
  });

  it("stores the match when the user acts, and links the save to it", async () => {
    const { d, api } = deps();
    await send({ type: "save", ref: ISSUE }, d);
    expect(api.getIssueMatch).toHaveBeenCalledWith(ISSUE, { persist: true });
    expect(api.saveIssue).toHaveBeenCalledWith(ISSUE, 42);
  });

  it("links feedback to the stored match", async () => {
    const { d, api } = deps();
    await send({ type: "feedback", ref: ISSUE, feedbackType: "difficulty", value: "too_easy" }, d);
    expect(api.submitFeedback).toHaveBeenCalledWith(ISSUE, "difficulty", "too_easy", 42);
  });

  it("proceeds without a match link if storing the match fails for a non-auth reason", async () => {
    const { d, api } = deps({ getIssueMatch: vi.fn(async () => { throw new ApiError("server", "down", 503); }) });
    const r = await send({ type: "feedback", ref: ISSUE, feedbackType: "relevance", value: "relevant" }, d);
    expect(r.ok).toBe(true);
    expect(api.submitFeedback).toHaveBeenCalledWith(ISSUE, "relevance", "relevant", null);
  });

  it("does not hide an expired session behind the match-link fallback", async () => {
    const { d, api } = deps({ getIssueMatch: vi.fn(async () => { throw new ApiError("auth", "expired", 401); }) });
    expect(await send({ type: "save", ref: ISSUE }, d)).toMatchObject({ ok: false, error: { kind: "auth" } });
    expect(api.saveIssue).not.toHaveBeenCalled();
  });
});

describe("errors", () => {
  it("passes API errors through as safe structured errors", async () => {
    const { d } = deps({ getDifficulty: vi.fn(async () => { throw new ApiError("rate_limited", "Slow down", 429, "github_rate_limited", 30); }) });
    expect(await send({ type: "difficulty", ref: ISSUE }, d)).toEqual({
      ok: false, error: { kind: "rate_limited", message: "Slow down", status: 429, retryAfterSeconds: 30 },
    });
  });

  it("never forwards the text of unexpected exceptions", async () => {
    const { d } = deps({ getHealth: vi.fn(async () => { throw new Error(`boom with ${TOKEN}`); }) });
    const r = await send({ type: "health", ref: ISSUE }, d);
    expect(r).toEqual({ ok: false, error: { kind: "unknown", message: "Unexpected extension error." } });
    expect(JSON.stringify(r)).not.toContain(TOKEN);
  });
});

describe("the extension's own pages (options) versus tabs", () => {
  const ADMIN = { id: EXT, url: OPTIONS_URL, tab: { url: OPTIONS_URL } };

  it("accepts account, profile and saved-issue messages only from the extension's own pages", async () => {
    const { d, api, session } = deps();
    for (const m of [{ type: "profile.get" }, { type: "saved.list", limit: 20, offset: 0 }, { type: "auth.signOut" }, { type: "auth.connect", code: "abc", keepSignedIn: false }]) {
      expect(await send(m, d)).toMatchObject({ ok: false, error: { kind: "rejected" } }); // from a github.com issue tab
    }
    // a page claiming to be github.com cannot borrow the options page's identity
    expect(await send({ type: "profile.get" }, d, { id: EXT, url: "https://github.com/acme/ml-api/issues/7", tab: TAB })).toMatchObject({ ok: false });
    expect(await send({ type: "profile.get" }, d, { id: EXT, url: `https://evil.example/chrome-extension://${EXT}/x`, tab: TAB })).toMatchObject({ ok: false });
    expect(api.getProfile).not.toHaveBeenCalled();
    expect(session.signOut).not.toHaveBeenCalled();
    expect(session.connect).not.toHaveBeenCalled();
    expect(await send({ type: "profile.get" }, d, ADMIN)).toMatchObject({ ok: true });
  });

  it("does not let an options page send panel messages for arbitrary issues", async () => {
    const { d, api } = deps();
    expect(await send({ type: "match", ref: ISSUE }, d, ADMIN)).toMatchObject({ ok: false, error: { kind: "rejected", message: "Invalid message." } });
    expect(api.getIssueMatch).not.toHaveBeenCalled();
  });

  it("rejects another extension even if it claims our options URL", async () => {
    const { d } = deps();
    expect(await send({ type: "profile.get" }, d, { id: "other", url: OPTIONS_URL })).toMatchObject({ ok: false, error: { kind: "rejected" } });
  });

  it("signs in, signs out and saves settings through the session manager, clearing cached answers", async () => {
    const { d, session, api } = deps();
    await send(msg, d);
    await send(msg, d);
    expect(api.getIssueMatch).toHaveBeenCalledTimes(1);
    expect(await send({ type: "auth.connect", code: " 12345 ", keepSignedIn: true }, d, ADMIN)).toMatchObject({ ok: true, data: { signedIn: true } });
    expect(session.connect).toHaveBeenCalledWith("12345", true);
    await send(msg, d);
    expect(api.getIssueMatch).toHaveBeenCalledTimes(2); // new session, new answers
    await send({ type: "auth.signOut" }, d, ADMIN);
    expect(session.signOut).toHaveBeenCalled();
    await send(msg, d);
    expect(api.getIssueMatch).toHaveBeenCalledTimes(3);
  });

  it("validates the backend URL before saving settings", async () => {
    const { d, session } = deps();
    for (const bad of ["http://evil.example", "https://u:p@x.example", "https://x.example/path", "javascript:alert(1)"]) {
      expect(await send({ type: "settings.save", baseUrl: bad, useExperimentalScorer: false }, d, ADMIN)).toMatchObject({ ok: false, error: { kind: "invalid_config" } });
    }
    expect(session.saveSettings).not.toHaveBeenCalled();
    expect(await send({ type: "settings.save", baseUrl: "https://compass.example.com", useExperimentalScorer: true }, d, ADMIN)).toMatchObject({ ok: true });
    expect(session.saveSettings).toHaveBeenCalledWith({ baseUrl: "https://compass.example.com", useExperimentalScorer: true, keepSignedIn: false });
  });

  it("validates a profile before it is sent", async () => {
    const { d, api } = deps();
    const good = { skills: [{ name: "python", category: "language", proficiency: 0.9 }], interests: [], contribution_goals: [], preferred_task_types: ["bug_fixing"], preferred_difficulty: 2, experience_level: "intermediate" };
    expect(await send({ type: "profile.save", profile: good, exists: true }, d, ADMIN)).toMatchObject({ ok: true });
    expect(api.saveProfile).toHaveBeenCalledWith(good, true);
    api.saveProfile.mockClear();
    for (const bad of [
      { ...good, skills: [{ name: "x", category: "language", proficiency: 2 }] },
      { ...good, preferred_difficulty: 9 },
      { ...good, experience_level: "wizard" },
    ]) {
      expect(await send({ type: "profile.save", profile: bad, exists: true }, d, ADMIN)).toMatchObject({ ok: false, error: { kind: "rejected" } });
    }
    expect(api.saveProfile).not.toHaveBeenCalled();
    // unknown fields (such as a user id) are stripped by validation, never forwarded
    await send({ type: "profile.save", profile: { ...good, user_id: 5 }, exists: false }, d, ADMIN);
    expect(JSON.stringify(api.saveProfile.mock.calls)).not.toContain("user_id");
  });

  it("forgets cached scores after the profile changes, because they depend on it", async () => {
    const { d, api } = deps();
    await send(msg, d);
    await send({ type: "profile.save", profile: { skills: [], interests: [], contribution_goals: [], preferred_task_types: [], preferred_difficulty: null, experience_level: null }, exists: true }, d, ADMIN);
    await send(msg, d);
    expect(api.getIssueMatch).toHaveBeenCalledTimes(2);
  });

  it("sign out everywhere ends all sessions on the server, then wipes this browser and its cache", async () => {
    const { d, api, session } = deps();
    await send(msg, d);
    expect(await send({ type: "auth.signOutAll" }, d, ADMIN)).toMatchObject({ ok: true, data: { signedIn: true } });
    expect(api.logoutAll).toHaveBeenCalledOnce();
    expect(session.signOut).toHaveBeenCalledOnce();
    await send(msg, d);
    expect(api.getIssueMatch).toHaveBeenCalledTimes(2); // cache cleared
    expect(await send({ type: "auth.signOutAll" }, d)).toMatchObject({ ok: false, error: { kind: "rejected" } }); // never from a github.com tab
    expect(api.logoutAll).toHaveBeenCalledOnce();
  });

  it("still wipes this browser when the server could not end the other sessions, and says so", async () => {
    const { d, api, session } = deps({ logoutAll: vi.fn(async () => { throw new ApiError("network", "Could not reach the backend."); }) });
    const r = await send({ type: "auth.signOutAll" }, d, ADMIN);
    expect(r).toMatchObject({ ok: false, error: { kind: "network" } });
    expect(session.signOut).toHaveBeenCalledOnce(); // local credentials are removed regardless
    expect(api.logoutAll).toHaveBeenCalledOnce();
  });

  it("lists and removes saved issues", async () => {
    const { d, api } = deps();
    expect(await send({ type: "saved.list", limit: 20, offset: 0 }, d, ADMIN)).toMatchObject({ ok: true });
    expect(api.listSaved).toHaveBeenCalledWith(20, 0);
    expect(await send({ type: "saved.list", limit: 1000, offset: 0 }, d, ADMIN)).toMatchObject({ ok: false, error: { kind: "rejected" } });
    expect(await send({ type: "saved.remove", savedId: 4 }, d, ADMIN)).toEqual({ ok: true, data: null });
    expect(api.unsaveIssue).toHaveBeenCalledWith(4);
  });
});

describe("request reduction (fewer backend and GitHub calls)", () => {
  it("answers repeat views of the same issue from memory", async () => {
    const { d, api } = deps();
    for (let i = 0; i < 5; i++) await send(msg, d); // navigating away and back, reopening the panel
    expect(api.getIssueMatch).toHaveBeenCalledTimes(1);
    for (let i = 0; i < 3; i++) await send({ type: "difficulty", ref: ISSUE }, d);
    expect(api.getDifficulty).toHaveBeenCalledTimes(1);
  });

  it("shares one repository-health analysis between issues of the same repository", async () => {
    const { d, api } = deps();
    const tab = (n: number) => ({ id: EXT, tab: { url: `https://github.com/acme/ml-api/issues/${n}` } });
    await send({ type: "health", ref: { ...ISSUE, number: 7 } }, d, tab(7));
    await send({ type: "health", ref: { ...ISSUE, number: 8 } }, d, tab(8));
    await send({ type: "health", ref: { owner: "ACME", repo: "ML-API", number: 9 } }, d, { id: EXT, tab: { url: "https://github.com/ACME/ML-API/issues/9" } });
    expect(api.getHealth).toHaveBeenCalledTimes(1);
  });

  it("collapses concurrent identical requests into one", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const { d, api } = deps({ getReadiness: vi.fn(async () => { await gate; return { r: 1 }; }) });
    const calls = Array.from({ length: 4 }, () => send({ type: "readiness", ref: ISSUE }, d));
    release();
    const results = await Promise.all(calls);
    expect(results.every((r) => r.ok)).toBe(true);
    expect(api.getReadiness).toHaveBeenCalledTimes(1);
  });

  it("stores the match once for several actions on one page", async () => {
    const { d, api } = deps();
    await send({ type: "save", ref: ISSUE }, d);
    await send({ type: "feedback", ref: ISSUE, feedbackType: "relevance", value: "relevant" }, d);
    await send({ type: "feedback", ref: ISSUE, feedbackType: "difficulty", value: "appropriate" }, d);
    expect(api.getIssueMatch).toHaveBeenCalledTimes(1);
    expect(api.getIssueMatch).toHaveBeenCalledWith(ISSUE, { persist: true });
  });

  it("does not cache ordinary errors, but does not hammer after a rate limit either", async () => {
    let n = 0;
    const { d, api } = deps({ getDifficulty: vi.fn(async () => { if (++n === 1) throw new ApiError("server", "down", 503); return { d: 1 }; }) });
    expect(await send({ type: "difficulty", ref: ISSUE }, d)).toMatchObject({ ok: false, error: { kind: "server" } });
    expect(await send({ type: "difficulty", ref: ISSUE }, d)).toEqual({ ok: true, data: { d: 1 } }); // an ordinary error is retried on demand
    expect(api.getDifficulty).toHaveBeenCalledTimes(2);

    const limited = deps({ getHealth: vi.fn(async () => { throw new ApiError("rate_limited", "GitHub rate limit reached", 429, "github_rate_limited", 300); }) });
    const first = await send({ type: "health", ref: ISSUE }, limited.d);
    expect(first).toMatchObject({ ok: false, error: { kind: "rate_limited", retryAfterSeconds: 300 } });
    const second = await send({ type: "health", ref: ISSUE }, limited.d);
    const third = await send({ type: "health", ref: ISSUE }, limited.d);
    expect(second).toMatchObject({ ok: false, error: { kind: "rate_limited" } });
    expect(third).toMatchObject({ ok: false, error: { kind: "rate_limited" } });
    expect(limited.api.getHealth).toHaveBeenCalledTimes(1); // no probing while the limit stands
  });

  it("looks up the saved state without caching it (it must reflect saves immediately)", async () => {
    let saved = false;
    const { d, api } = deps({ lookupSaved: vi.fn(async () => lookup(saved)) });
    expect(await send({ type: "lookupSaved", ref: ISSUE }, d)).toMatchObject({ ok: true, data: { saved: false } });
    saved = true;
    expect(await send({ type: "lookupSaved", ref: ISSUE }, d)).toMatchObject({ ok: true, data: { saved: true } });
    expect(api.lookupSaved).toHaveBeenCalledTimes(2);
  });
});

describe("recommendation messages (options page only)", () => {
  const ADMIN = { id: EXT, url: OPTIONS_URL, tab: { url: OPTIONS_URL } };
  const search = { type: "matches.search", limit: 10, offset: 0 };

  it("are refused from a GitHub tab", async () => {
    const { d, api } = deps();
    for (const m of [search, { type: "matches.stored" }, { type: "issue.save", ref: ISSUE }, { type: "issue.difficulty", ref: ISSUE }]) {
      expect(await send(m, d)).toMatchObject({ ok: false, error: { kind: "rejected" } });
    }
    expect(api.searchMatches).not.toHaveBeenCalled();
    expect(api.saveIssue).not.toHaveBeenCalled();
  });

  it("pass only the validated filters to the API, and ignore anything else in the message", async () => {
    const { d, api } = deps();
    expect(await send({ ...search, language: "Python", topic: "cli", preferredDifficulty: 2, repository: "psf/requests" }, d, ADMIN)).toMatchObject({ ok: true });
    expect(api.searchMatches).toHaveBeenCalledWith({ language: "Python", topic: "cli", preferredDifficulty: 2, repository: "psf/requests", limit: 10, offset: 0 });
  });

  it.each([
    { limit: 0, offset: 0 }, { limit: 51, offset: 0 }, { limit: 10, offset: -1 },
    { limit: 10, offset: 0, preferredDifficulty: 6 }, { limit: 10, offset: 0, repository: "not a repo" }, { limit: 10, offset: 0, repository: "../etc/passwd" },
    { limit: 10, offset: 0, language: "" },
  ])("reject invalid search %j before any request is made", async (bad) => {
    const { d, api } = deps();
    expect(await send({ type: "matches.search", ...bad }, d, ADMIN)).toMatchObject({ ok: false, error: { kind: "rejected" } });
    expect(api.searchMatches).not.toHaveBeenCalled();
  });

  it("does not cache searches (a changed filter must give a fresh ranking)", async () => {
    const { d, api } = deps();
    await send(search, d, ADMIN);
    await send(search, d, ADMIN);
    expect(api.searchMatches).toHaveBeenCalledTimes(2);
  });

  it("saves from the options page the same way the panel does, linking the stored match", async () => {
    const { d, api } = deps();
    expect(await send({ type: "issue.save", ref: ISSUE }, d, ADMIN)).toMatchObject({ ok: true });
    expect(api.getIssueMatch).toHaveBeenCalledWith(ISSUE, { persist: true });
    expect(api.saveIssue).toHaveBeenCalledWith(ISSUE, 42);
  });

  it("shares the difficulty answer with the panel's cache and lists stored matches", async () => {
    const { d, api } = deps();
    await send({ type: "difficulty", ref: ISSUE }, d);
    await send({ type: "issue.difficulty", ref: ISSUE }, d, ADMIN);
    expect(api.getDifficulty).toHaveBeenCalledTimes(1);
    expect(await send({ type: "matches.stored" }, d, ADMIN)).toMatchObject({ ok: true });
    expect(api.listStoredMatches).toHaveBeenCalledWith(100);
  });

  it("reports an API failure as an error instead of throwing", async () => {
    const { d } = deps({ searchMatches: vi.fn(async () => { throw new ApiError("rate_limited", "Slow down.", 429, undefined, 30); }) });
    expect(await send(search, d, ADMIN)).toMatchObject({ ok: false, error: { kind: "rate_limited", retryAfterSeconds: 30 } });
  });
});
