import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, createApi, exchangeCode, logoutRemote, refreshTokens, validateBaseUrl, type ApiConfig } from "../src/shared/api";
import { difficulty, feedbackResult, health, ISSUE, issueMatch, matchSearch, readiness, savedIssue, storedMatches } from "./fixtures";

const TOKEN = "eyJ.SUPER-SECRET-TOKEN.sig";
const config = (over: Partial<ApiConfig> = {}): ApiConfig => ({ baseUrl: "http://localhost:8000", token: TOKEN, useExperimentalScorer: false, ...over });
const json = (body: unknown, init: ResponseInit = {}) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" }, ...init });
const envelope = (code: string, message: string, status: number, headers: Record<string, string> = {}) =>
  json({ error: { code, message, details: null, request_id: "r" } }, { status, headers });

function make(fetchImpl: typeof fetch, cfg = config(), timeoutMs?: number) {
  return createApi({ getConfig: async () => cfg, fetchImpl, timeoutMs });
}

afterEach(() => vi.useRealTimers());

async function errorOf(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (e) {
    return e as ApiError;
  }
  throw new Error("expected the call to reject");
}

describe("requests", () => {
  it("sends the bearer token only in the Authorization header, to the configured origin, without cookies or redirects", async () => {
    const f = vi.fn(async () => json(issueMatch()));
    await make(f as unknown as typeof fetch).getIssueMatch(ISSUE, { persist: false });
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/api/v1/issues/acme/ml-api/7/match");
    expect((init.headers as Record<string, string>).Authorization).toBe(`Bearer ${TOKEN}`);
    expect(init.credentials).toBe("omit");
    expect(init.redirect).toBe("error"); // a redirect must never carry the token elsewhere
    expect(url).not.toContain(TOKEN);
    expect(init.body).not.toContain(TOKEN);
    expect(JSON.parse(init.body as string)).toEqual({ scorer: "heuristic_v1", persist: false });
  });

  it("selects the experimental scorer only when the user enabled it", async () => {
    const f = vi.fn(async () => json(issueMatch()));
    await make(f as unknown as typeof fetch, config({ useExperimentalScorer: true })).getIssueMatch(ISSUE, { persist: true });
    expect(JSON.parse((f.mock.calls[0] as unknown as [string, RequestInit])[1].body as string)).toEqual({ scorer: "heuristic_v2", persist: true });
  });

  it("calls every endpoint with the right method, path and body", async () => {
    const f = vi.fn(async (url: string) =>
      url.endsWith("/difficulty") ? json(difficulty()) : url.endsWith("/readiness") ? json(readiness()) : url.endsWith("/health") ? json(health())
      : url.endsWith("/saved-issues") ? json(savedIssue()) : url.endsWith("/feedback") ? json(feedbackResult()) : new Response(null, { status: 204 }));
    const api = make(f as unknown as typeof fetch);
    await api.getDifficulty(ISSUE);
    await api.getReadiness(ISSUE);
    await api.getHealth(ISSUE);
    await api.saveIssue(ISSUE, 5);
    await api.unsaveIssue(11);
    await api.submitFeedback(ISSUE, "relevance", "relevant", 5);
    await api.submitFeedback(ISSUE, "difficulty", "too_easy", null);
    const calls = f.mock.calls.map((c) => { const [u, i] = c as unknown as [string, RequestInit]; return [i.method, u.replace("http://localhost:8000", ""), i.body ? JSON.parse(i.body as string) : undefined]; });
    expect(calls).toEqual([
      ["POST", "/api/v1/issues/acme/ml-api/7/difficulty", undefined],
      ["GET", "/api/v1/issues/acme/ml-api/7/readiness", undefined],
      ["GET", "/api/v1/repositories/acme/ml-api/health", undefined],
      ["POST", "/api/v1/saved-issues", { owner: "acme", repo: "ml-api", number: 7, match_id: 5 }],
      ["DELETE", "/api/v1/saved-issues/11", undefined],
      ["POST", "/api/v1/feedback", { type: "relevance", value: "relevant", match_id: 5 }],
      ["POST", "/api/v1/feedback", { type: "difficulty", value: "too_easy", owner: "acme", repo: "ml-api", number: 7 }],
    ]);
  });

  it("refuses invalid issue references before any request", async () => {
    const f = vi.fn();
    const api = make(f as unknown as typeof fetch);
    for (const bad of [{ owner: "a/b", repo: "c", number: 1 }, { owner: "a", repo: "..", number: 1 }, { owner: "a", repo: "c", number: 0 }]) {
      await expect(Promise.resolve().then(() => api.getHealth(bad))).rejects.toMatchObject({ kind: "invalid_request" });
    }
    await expect(api.unsaveIssue(0)).rejects.toMatchObject({ kind: "invalid_request" });
    expect(f).not.toHaveBeenCalled();
  });
});

describe("authentication", () => {
  it("does nothing without a token", async () => {
    const f = vi.fn();
    await expect(make(f as unknown as typeof fetch, config({ token: null })).getMe()).rejects.toMatchObject({ kind: "not_configured" });
    expect(f).not.toHaveBeenCalled();
  });

  it("maps 401 to an auth error without echoing server text or the token", async () => {
    const f = vi.fn(async () => envelope("unauthorized", `Invalid token ${TOKEN}`, 401));
    const err = await errorOf(make(f as unknown as typeof fetch).getMe());
    expect(err).toBeInstanceOf(ApiError);
    expect(err.kind).toBe("auth");
    expect(err.message).not.toContain(TOKEN);
    expect(String(err.stack)).not.toContain(TOKEN);
  });
});

describe("error mapping", () => {
  const cases: [number, string][] = [[403, "forbidden"], [404, "not_found"], [422, "invalid_request"], [500, "server"], [502, "server"], [504, "server"], [418, "unknown"]];
  it.each(cases)("maps HTTP %s to %s using the backend's message", async (status, kind) => {
    const f = vi.fn(async () => envelope("some_code", "Backend explanation.", status));
    await expect(make(f as unknown as typeof fetch).getDifficulty(ISSUE)).rejects.toMatchObject({ kind, status, code: "some_code" });
  });

  it("reads Retry-After on 429", async () => {
    const f = vi.fn(async () => envelope("github_rate_limited", "GitHub rate limit reached; try again later", 429, { "retry-after": "120" }));
    await expect(make(f as unknown as typeof fetch).getHealth(ISSUE)).rejects.toMatchObject({ kind: "rate_limited", retryAfterSeconds: 120 });
  });

  it("copes with error bodies that are not the expected envelope", async () => {
    for (const body of ["<html>Bad gateway</html>", "{}", "null"]) {
      const f = vi.fn(async () => new Response(body, { status: 502 }));
      await expect(make(f as unknown as typeof fetch).getMe()).rejects.toMatchObject({ kind: "server" });
    }
  });

  it("truncates very long server messages", async () => {
    const f = vi.fn(async () => envelope("x", "A".repeat(5000), 404));
    const err = await errorOf(make(f as unknown as typeof fetch).getMe());
    expect(err.message.length).toBeLessThanOrEqual(300);
  });

  it("reports network failure without leaking details", async () => {
    const f = vi.fn(async () => { throw new TypeError(`Failed to fetch http://localhost:8000 Bearer ${TOKEN}`); });
    const err = await errorOf(make(f as unknown as typeof fetch).getMe());
    expect(err.kind).toBe("network");
    expect(err.message).not.toContain(TOKEN);
  });

  it("times out", async () => {
    vi.useFakeTimers();
    const f = vi.fn((_u: string, init: RequestInit) => new Promise<Response>((_res, rej) => init.signal!.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")))));
    const p = errorOf(make(f as unknown as typeof fetch, config(), 5000).getMe());
    await vi.advanceTimersByTimeAsync(5001);
    expect((await p).kind).toBe("timeout");
  });
});

describe("response validation", () => {
  it("returns validated data and ignores unknown extra fields", async () => {
    const f = vi.fn(async () => json({ ...issueMatch(), surprise: "extra" }));
    const out = await make(f as unknown as typeof fetch).getIssueMatch(ISSUE, { persist: false });
    expect(out.match.final_score).toBe(63.4);
    expect(out).not.toHaveProperty("surprise");
  });

  it("keeps unknown component scores as null (never 0)", async () => {
    const out = await make((async () => json(issueMatch())) as unknown as typeof fetch).getIssueMatch(ISSUE, { persist: false });
    expect(out.match.components.interest_similarity.score).toBeNull();
  });

  it.each([
    ["non-JSON body", () => new Response("not json", { status: 200 })],
    ["wrong top-level type", () => json([1, 2, 3])],
    ["missing required field", () => json({ ...issueMatch(), match: undefined })],
    ["score as a string", () => json(issueMatch({ match: { final_score: "63" } }))],
    ["component with wrong shape", () => json(issueMatch({ match: { components: { skill_compatibility: { score: "high" } } } }))],
    ["null body", () => json(null)],
  ])("rejects a malformed response: %s", async (_name, make_) => {
    const f = vi.fn(async () => make_());
    const err = await errorOf(make(f as unknown as typeof fetch).getIssueMatch(ISSUE, { persist: false }));
    expect(err).toBeInstanceOf(ApiError);
    expect(err.kind).toBe("malformed");
    expect(err.message).not.toContain("Parser crashes"); // field paths only, never payload content
  });
});

describe("validateBaseUrl", () => {
  it.each([
    ["http://localhost:8000", "http://localhost:8000"], ["http://127.0.0.1:9000/", "http://127.0.0.1:9000"],
    ["https://compass.example.com", "https://compass.example.com"], ["  https://compass.example.com  ", "https://compass.example.com"],
  ])("accepts %s", (raw, expected) => expect(validateBaseUrl(raw)).toBe(expected));

  it.each([
    "http://compass.example.com", "ftp://localhost", "javascript:alert(1)", "not a url", "",
    "https://user:pw@compass.example.com", "https://compass.example.com/api", "https://compass.example.com/?x=1", "https://compass.example.com/#x",
  ])("rejects %s", (raw) => expect(() => validateBaseUrl(raw)).toThrow(ApiError));
});

describe("profile and saved-issue calls", () => {
  const profile = { skills: [], interests: [], contribution_goals: [], preferred_task_types: [], preferred_difficulty: null, experience_level: null, updated_at: "2026-01-01T00:00:00Z" };
  const input = { skills: [], interests: ["ml"], contribution_goals: [], preferred_task_types: [], preferred_difficulty: null, experience_level: null };

  it("treats a missing profile as a normal state (null), but other 404s as errors", async () => {
    const f = vi.fn(async () => envelope("profile_not_found", "No profile yet.", 404));
    expect(await make(f as unknown as typeof fetch).getProfile()).toBeNull();
    const g = vi.fn(async () => envelope("not_found", "Nope.", 404));
    await expect(make(g as unknown as typeof fetch).getProfile()).rejects.toMatchObject({ kind: "not_found" });
  });

  it("creates with POST and updates with PUT, sending only the validated fields", async () => {
    const f = vi.fn(async () => json(profile));
    const api = make(f as unknown as typeof fetch);
    await api.saveProfile(input, false);
    await api.saveProfile(input, true);
    const calls = f.mock.calls as unknown as [string, RequestInit][];
    expect(calls.map(([u, i]) => `${i.method} ${new URL(u).pathname}`)).toEqual(["POST /api/v1/profile", "PUT /api/v1/profile"]);
    expect(JSON.parse(String(calls[0][1].body))).toEqual(input);
  });

  it("surfaces validation details from a 422 as short field messages", async () => {
    const f = vi.fn(async () => json({ error: { code: "validation_error", message: "Request validation failed", details: [{ loc: ["body", "interests"], msg: "duplicate entry: 'a'", type: "value_error" }] } }, { status: 422 }));
    const err = await errorOf(make(f as unknown as typeof fetch).saveProfile(input, true));
    expect(err).toMatchObject({ kind: "invalid_request", details: ["interests: duplicate entry: 'a'"] });
  });

  it("looks up the saved state and lists saved issues for the signed-in user only (no user id is ever sent)", async () => {
    const f = vi.fn(async (url: string) => json(url.includes("lookup") ? { saved: true, saved_issue: savedIssue() } : { items: [savedIssue()], total: 1, limit: 20, offset: 0 }));
    const api = make(f as unknown as typeof fetch);
    expect(await api.lookupSaved(ISSUE)).toMatchObject({ saved: true });
    expect((await api.listSaved(20, 0)).total).toBe(1);
    const urls = (f.mock.calls as unknown as [string][]).map(([u]) => u);
    expect(urls[0]).toBe("http://localhost:8000/api/v1/saved-issues/lookup/acme/ml-api/7");
    expect(urls[1]).toBe("http://localhost:8000/api/v1/saved-issues?limit=20&offset=0");
    expect(urls.join()).not.toMatch(/user/i);
  });

  it("rejects bad paging and ids before any request", async () => {
    const f = vi.fn();
    const api = make(f as unknown as typeof fetch);
    expect(() => api.listSaved(0, 0)).toThrow(ApiError);
    expect(() => api.listSaved(20, -1)).toThrow(ApiError);
    expect(() => api.listSaved(101, 0)).toThrow(ApiError);
    await expect(api.unsaveIssue(0)).rejects.toMatchObject({ kind: "invalid_request" });
    expect(() => api.lookupSaved({ owner: "..", repo: "x", number: 1 })).toThrow(ApiError);
    expect(f).not.toHaveBeenCalled();
  });
});

describe("auth calls", () => {
  const pair = { access_token: "a", refresh_token: "r", token_type: "Bearer", expires_in: 900, refresh_expires_in: 100, session_expires_at: "2026-02-01T00:00:00Z" };

  it("exchange/refresh/logout send no Authorization header and use the safe fetch options", async () => {
    const f = vi.fn(async (url: string) => (url.endsWith("/logout") ? new Response(null, { status: 204 }) : json(pair)));
    await exchangeCode("http://localhost:8000", " c ", "Chrome extension", f as unknown as typeof fetch);
    await refreshTokens("http://localhost:8000", "r", f as unknown as typeof fetch);
    await logoutRemote("http://localhost:8000", "r", f as unknown as typeof fetch);
    for (const [, init] of f.mock.calls as unknown as [string, RequestInit][]) {
      expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
      expect(init.credentials).toBe("omit");
      expect(init.redirect).toBe("error");
      expect(init.cache).toBe("no-store");
    }
    expect(JSON.parse(String((f.mock.calls[0] as unknown as [string, RequestInit])[1].body))).toEqual({ code: "c", client_label: "Chrome extension", persistent: false });
  });

  it("refuses to send credentials to an unsafe origin", async () => {
    const f = vi.fn();
    await expect(exchangeCode("http://evil.example", "c", null, f as unknown as typeof fetch)).rejects.toMatchObject({ kind: "invalid_config" });
    await expect(refreshTokens("https://u:p@x.example", "r", f as unknown as typeof fetch)).rejects.toMatchObject({ kind: "invalid_config" });
    expect(f).not.toHaveBeenCalled();
  });

  it("a 401 on refresh is an auth error with a fixed message (the server's text is not echoed)", async () => {
    const f = vi.fn(async () => envelope("unauthorized", "refresh token r was reused", 401));
    const err = await errorOf(refreshTokens("http://localhost:8000", "r", f as unknown as typeof fetch));
    expect(err).toMatchObject({ kind: "auth", message: "Your session has ended. Sign in again." });
    expect(err.message).not.toContain("reused");
  });

  it("logout treats an already-dead session (401) as done but reports other failures", async () => {
    await expect(logoutRemote("http://localhost:8000", "r", (async () => envelope("unauthorized", "x", 401)) as unknown as typeof fetch)).resolves.toBeUndefined();
    await expect(logoutRemote("http://localhost:8000", "r", (async () => envelope("server", "x", 503)) as unknown as typeof fetch)).rejects.toMatchObject({ kind: "server" });
  });

  it("a network failure never leaks the underlying error text", async () => {
    const f = vi.fn(async () => { throw new TypeError("fetch failed: POST http://localhost:8000 body {refresh_token: r}"); });
    const err = await errorOf(refreshTokens("http://localhost:8000", "r", f as unknown as typeof fetch));
    expect(err.kind).toBe("network");
    expect(err.message).not.toContain("refresh_token");
  });
});

describe("sign-out everywhere and persistent sign-in", () => {
  it("logoutAll is a bearer-authenticated POST with no body", async () => {
    const f = vi.fn(async () => new Response(null, { status: 204 }));
    await make(f as unknown as typeof fetch).logoutAll();
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(`${init.method} ${new URL(url).pathname}`).toBe("POST /api/v1/auth/logout-all");
    expect((init.headers as Record<string, string>).Authorization).toBe(`Bearer ${TOKEN}`);
    expect(init.body).toBeUndefined();
  });

  it("logoutAll surfaces failures (so the UI can say other sessions may remain)", async () => {
    const f = vi.fn(async () => envelope("unauthorized", "x", 401));
    await expect(make(f as unknown as typeof fetch).logoutAll()).rejects.toMatchObject({ kind: "auth" });
  });

  it("exchange tells the server whether the refresh token will be kept on disk", async () => {
    const f = vi.fn(async () => json({ access_token: "a", refresh_token: "r", token_type: "Bearer", expires_in: 900, refresh_expires_in: 100, session_expires_at: "2026-02-01T00:00:00Z" }));
    await exchangeCode("http://localhost:8000", "c", null, f as unknown as typeof fetch);
    await exchangeCode("http://localhost:8000", "c", null, f as unknown as typeof fetch, true);
    const bodies = (f.mock.calls as unknown as [string, RequestInit][]).map(([, i]) => JSON.parse(String(i.body)));
    expect(bodies).toEqual([{ code: "c", persistent: false }, { code: "c", persistent: true }]);
  });
});

describe("single retry on 401", () => {
  it("retries once after onUnauthorized resolves true, and never more", async () => {
    let n = 0;
    const f = vi.fn(async () => (++n === 1 ? envelope("unauthorized", "x", 401) : json({ id: 1, github_user_id: 2, username: "u" })));
    let token = "old";
    const onUnauthorized = vi.fn(async () => { token = "new"; return true; });
    const api = createApi({ getConfig: async () => config({ token }), fetchImpl: f as unknown as typeof fetch, onUnauthorized });
    expect(await api.getMe()).toMatchObject({ username: "u" });
    expect(onUnauthorized).toHaveBeenCalledWith("old");
    expect((f.mock.calls[1] as unknown as [string, RequestInit])[1].headers).toMatchObject({ Authorization: "Bearer new" });

    const always401 = vi.fn(async () => envelope("unauthorized", "x", 401));
    const again = vi.fn(async () => true);
    const api2 = createApi({ getConfig: async () => config(), fetchImpl: always401 as unknown as typeof fetch, onUnauthorized: again });
    await expect(api2.getMe()).rejects.toMatchObject({ kind: "auth" });
    expect(always401).toHaveBeenCalledTimes(2);
    expect(again).toHaveBeenCalledTimes(1);
  });

  it("does not retry when renewal was refused", async () => {
    const f = vi.fn(async () => envelope("unauthorized", "x", 401));
    const api = createApi({ getConfig: async () => config(), fetchImpl: f as unknown as typeof fetch, onUnauthorized: async () => false });
    await expect(api.getMe()).rejects.toMatchObject({ kind: "auth" });
    expect(f).toHaveBeenCalledTimes(1);
  });
});

describe("recommendation calls", () => {
  it("searches without writing anything, sends only the chosen filters, and never sends a user id", async () => {
    const f = vi.fn(async () => json(matchSearch()));
    const api = make(f as unknown as typeof fetch);
    await api.searchMatches({ limit: 10, offset: 0 });
    await api.searchMatches({ limit: 10, offset: 10, language: "Python", topic: "cli", preferredDifficulty: 3, repository: "psf/requests" });
    const calls = f.mock.calls as unknown as [string, RequestInit][];
    expect(calls.map(([u, i]) => `${i.method} ${new URL(u).pathname}`)).toEqual(["POST /api/v1/matches/search", "POST /api/v1/matches/search"]);
    expect(JSON.parse(String(calls[0][1].body))).toEqual({ scorer: "heuristic_v1", persist: false, limit: 10, offset: 0 });
    expect(JSON.parse(String(calls[1][1].body))).toEqual({
      scorer: "heuristic_v1", persist: false, limit: 10, offset: 10, language: "Python", topic: "cli", preferred_difficulty: 3, repositories: ["psf/requests"],
    });
    expect(JSON.stringify(calls.map(([, i]) => i.body))).not.toMatch(/user/i);
  });

  it("uses the experimental scorer only when the setting is on", async () => {
    const f = vi.fn(async () => json(matchSearch()));
    await make(f as unknown as typeof fetch, config({ useExperimentalScorer: true })).searchMatches({ limit: 5, offset: 0 });
    expect(JSON.parse(String((f.mock.calls as unknown as [string, RequestInit][])[0][1].body)).scorer).toBe("heuristic_v2");
  });

  it("rejects a malformed search reply instead of showing something invented", async () => {
    const f = vi.fn(async () => json({ items: [{ final_score: "high" }] }));
    await expect(make(f as unknown as typeof fetch).searchMatches({ limit: 10, offset: 0 })).rejects.toMatchObject({ kind: "malformed" });
  });

  it("lists stored matches with a bounded page size", async () => {
    const f = vi.fn(async () => json(storedMatches()));
    const api = make(f as unknown as typeof fetch);
    expect((await api.listStoredMatches(100)).total).toBe(0);
    expect((f.mock.calls as unknown as [string][])[0][0]).toBe("http://localhost:8000/api/v1/matches?limit=100");
    expect(() => api.listStoredMatches(0)).toThrow(ApiError);
    expect(() => api.listStoredMatches(101)).toThrow(ApiError);
  });
});
