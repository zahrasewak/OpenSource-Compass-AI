import { describe, expect, it, vi } from "vitest";
import { ApiError, createApi } from "../src/shared/api";
import { SessionManager } from "../src/shared/session";
import { memoryStorage } from "../src/shared/storage";

const BASE = "http://localhost:8000";
const json = (body: unknown, init: ResponseInit = {}) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" }, ...init });
const unauthorized = () => json({ error: { code: "unauthorized", message: "Invalid token SECRET-LEAK", details: null, request_id: "r" } }, { status: 401 });

let counter = 0;
/** What the backend returns from /auth/exchange and /auth/refresh. */
const tokens = (over: Record<string, unknown> = {}) => {
  counter += 1;
  return {
    access_token: `access-${counter}`, refresh_token: `refresh-${counter}`, token_type: "Bearer", expires_in: 900, refresh_expires_in: 604_800,
    session_expires_at: new Date(T0 + 30 * 86_400_000).toISOString(), ...over,
  };
};
const T0 = Date.parse("2026-01-01T00:00:00Z");

function setup(handler: (url: string, init: RequestInit) => Response | Promise<Response>, opts: { now?: () => number } = {}) {
  counter = 0; // token names restart for every test
  const local = memoryStorage();
  const session = memoryStorage();
  let time = T0;
  const now = opts.now ?? (() => time);
  const fetchImpl = vi.fn(async (url: string, init: RequestInit) => handler(url, init)) as unknown as typeof fetch & ReturnType<typeof vi.fn>;
  const manager = new SessionManager({ local, session, fetchImpl, now, withLock: (fn) => fn() });
  return { manager, local, session, fetchImpl, advance: (ms: number) => void (time += ms), calls: () => (fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls as [string, RequestInit][] };
}
const bodyOf = (init: RequestInit) => JSON.parse(String(init.body)) as Record<string, string>;
const route = (table: Record<string, (init: RequestInit) => Response | Promise<Response>>) => (url: string, init: RequestInit) => {
  const path = new URL(url).pathname;
  const h = table[path];
  if (!h) throw new Error(`unexpected request to ${path}`);
  return h(init);
};

describe("signing in", () => {
  it("exchanges a pairing code and keeps the access and refresh tokens in session (memory) storage only", async () => {
    const t = setup(route({ "/api/v1/auth/exchange": () => json(tokens()) }));
    const state = await t.manager.connect(" CODE-123 ", false);
    expect(state).toMatchObject({ signedIn: true, keepSignedIn: false, backendUrl: BASE });
    expect(bodyOf(t.calls()[0][1])).toMatchObject({ code: "CODE-123" });
    expect(JSON.stringify(t.session.data)).toContain("access-1");
    expect(JSON.stringify(t.session.data)).toContain("refresh-1");
    // nothing secret on disk
    expect(JSON.stringify(t.local.data)).not.toMatch(/access-|refresh-|CODE-123/);
  });

  it("tells the server whether the session is persistent so it can apply tighter limits", async () => {
    const t = setup(route({ "/api/v1/auth/exchange": () => json(tokens()) }));
    await t.manager.connect("c", true);
    expect(bodyOf(t.calls()[0][1])).toMatchObject({ persistent: true });
    const u = setup(route({ "/api/v1/auth/exchange": () => json(tokens()) }));
    await u.manager.connect("c", false);
    expect(bodyOf(u.calls()[0][1])).toMatchObject({ persistent: false });
  });

  it("persists the refresh token (never the access token) only when 'keep me signed in' is chosen", async () => {
    const t = setup(route({ "/api/v1/auth/exchange": () => json(tokens()) }));
    await t.manager.connect("c", true);
    expect(JSON.stringify(t.local.data)).toContain("refresh-1");
    expect(JSON.stringify(t.local.data)).not.toContain("access-1");
    expect(JSON.stringify(t.session.data)).toContain("access-1");
    expect(JSON.stringify(t.session.data)).not.toContain("refresh-1");
    // simulate a browser restart: session storage is wiped, local survives
    for (const k of Object.keys(t.session.data)) delete t.session.data[k];
    expect((await t.manager.getState()).signedIn).toBe(true);
  });

  it("without 'keep me signed in' a browser restart signs the user out", async () => {
    const t = setup(route({ "/api/v1/auth/exchange": () => json(tokens()) }));
    await t.manager.connect("c", false);
    for (const k of Object.keys(t.session.data)) delete t.session.data[k]; // session storage is cleared when the browser closes
    expect(await t.manager.getState()).toMatchObject({ signedIn: false });
    expect((await t.manager.getConfig()).token).toBeNull();
  });

  it("a malformed (422) code gets the same message as a rejected (401) one", async () => {
    const t = setup(route({ "/api/v1/auth/exchange": () => json({ error: { code: "validation_error", message: "Request validation failed", details: [] } }, { status: 422 }) }));
    await expect(t.manager.connect("short", false)).rejects.toMatchObject({ kind: "auth", message: "The pairing code is invalid, expired or already used." });
  });

  it("reports a rejected code with a fixed message and stores nothing", async () => {
    const t = setup(route({ "/api/v1/auth/exchange": () => unauthorized() }));
    const err = (await t.manager.connect("bad", true).catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ kind: "auth", message: "The pairing code is invalid, expired or already used." });
    expect(String(err.message)).not.toContain("SECRET-LEAK");
    expect(Object.keys(t.session.data)).toHaveLength(0);
    expect(JSON.stringify(t.local.data)).not.toMatch(/refresh|access/);
  });

  it("never sends cookies or follows redirects on auth calls", async () => {
    const t = setup(route({ "/api/v1/auth/exchange": () => json(tokens()) }));
    await t.manager.connect("c", false);
    const init = t.calls()[0][1];
    expect(init.credentials).toBe("omit");
    expect(init.redirect).toBe("error");
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it("rejects a malformed token response instead of storing it", async () => {
    const t = setup(route({ "/api/v1/auth/exchange": () => json({ access_token: "x" }) }));
    await expect(t.manager.connect("c", false)).rejects.toMatchObject({ kind: "malformed" });
    expect(Object.keys(t.session.data)).toHaveLength(0);
  });
});

describe("using and renewing the access token", () => {
  async function signedIn(extra: Record<string, (init: RequestInit) => Response | Promise<Response>> = {}) {
    const t = setup(route({ "/api/v1/auth/exchange": () => json(tokens()), ...extra }));
    await t.manager.connect("c", false);
    return t;
  }

  it("returns the current access token without any request while it is fresh", async () => {
    const t = await signedIn();
    t.fetchImpl.mockClear();
    t.advance(10 * 60_000);
    expect((await t.manager.getConfig()).token).toBe("access-1");
    expect(t.calls()).toHaveLength(0);
  });

  it("renews shortly before the 15-minute access token expires, rotating the refresh token", async () => {
    let seen = "";
    const t = await signedIn({ "/api/v1/auth/refresh": (init) => { seen = bodyOf(init).refresh_token; return json(tokens()); } });
    t.advance(14.75 * 60_000); // inside the 30 s skew
    const config = await t.manager.getConfig();
    expect(seen).toBe("refresh-1");
    expect(config.token).toBe("access-2");
    expect(JSON.stringify(t.session.data)).toContain("refresh-2");
    expect(JSON.stringify(t.session.data)).not.toContain("refresh-1");
  });

  it("renews an expired token once for many concurrent callers (single flight)", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    let refreshes = 0;
    const t = await signedIn({ "/api/v1/auth/refresh": async () => { refreshes++; await gate; return json(tokens()); } });
    t.advance(16 * 60_000);
    const pending = Array.from({ length: 6 }, () => t.manager.getConfig());
    await Promise.resolve();
    release();
    const results = await Promise.all(pending);
    expect(refreshes).toBe(1);
    expect(new Set(results.map((r) => r.token))).toEqual(new Set(["access-2"]));
  });

  it("another context that already renewed is respected (no second refresh, no reuse of a spent token)", async () => {
    let refreshes = 0;
    const t = await signedIn({ "/api/v1/auth/refresh": () => { refreshes++; return json(tokens()); } });
    // Another context stored a newer pair after we read ours:
    t.advance(16 * 60_000);
    await t.session.set("compass.auth.v2", { accessToken: "access-other", accessExpiresAt: T0 + 31 * 60_000, refreshToken: "refresh-other", sessionExpiresAt: T0 + 30 * 86_400_000 });
    expect(await t.manager.onUnauthorized("access-1")).toBe(true);
    expect(refreshes).toBe(0);
    expect((await t.manager.getConfig()).token).toBe("access-other");
  });

  it("a server rejection of the refresh token ends the session: tokens are wiped and the UI is told why", async () => {
    const t = await signedIn({ "/api/v1/auth/refresh": () => unauthorized() });
    t.advance(16 * 60_000);
    await expect(t.manager.getConfig()).rejects.toMatchObject({ kind: "auth" });
    expect(Object.keys(t.session.data).filter((k) => k !== "compass.ended.v2")).toHaveLength(0);
    expect(await t.manager.getState()).toMatchObject({ signedIn: false, sessionEnded: true });
    t.fetchImpl.mockClear();
    await expect(t.manager.getConfig()).rejects.toMatchObject({ kind: "auth" }); // says so again, without calling the server with a dead token
    expect(t.calls()).toHaveLength(0);
  });

  it("a network failure during renewal keeps the refresh token so a later attempt can succeed", async () => {
    let up = false;
    const t = await signedIn({ "/api/v1/auth/refresh": () => { if (!up) throw new TypeError("offline"); return json(tokens()); } });
    t.advance(16 * 60_000);
    await expect(t.manager.getConfig()).rejects.toMatchObject({ kind: "network" });
    expect((await t.manager.getState()).signedIn).toBe(true);
    up = true;
    expect((await t.manager.getConfig()).token).toBe("access-2");
  });

  it("a rate-limited renewal (429) keeps the session: the refresh token is not discarded", async () => {
    const t = await signedIn({ "/api/v1/auth/refresh": () => json({ error: { code: "rate_limited", message: "Too many attempts.", details: null } }, { status: 429, headers: { "retry-after": "120" } }) });
    t.advance(16 * 60_000);
    await expect(t.manager.getConfig()).rejects.toMatchObject({ kind: "rate_limited", retryAfterSeconds: 120 });
    expect((await t.manager.getState()).signedIn).toBe(true);
    expect(JSON.stringify(t.session.data)).toContain("refresh-1");
  });

  it("a rate-limited sign-in (429) is reported as rate limiting, not as a rejected code, and stores nothing", async () => {
    const t = setup(route({ "/api/v1/auth/exchange": () => json({ error: { code: "rate_limited", message: "Too many attempts.", details: null } }, { status: 429, headers: { "retry-after": "300" } }) }));
    await expect(t.manager.connect("some-code", false)).rejects.toMatchObject({ kind: "rate_limited", retryAfterSeconds: 300 });
    expect(Object.keys(t.session.data)).toHaveLength(0);
  });

  it("a 5xx during renewal is not treated as a revoked session", async () => {
    const t = await signedIn({ "/api/v1/auth/refresh": () => json({ error: { code: "server", message: "down", details: null } }, { status: 503 }) });
    t.advance(16 * 60_000);
    await expect(t.manager.getConfig()).rejects.toMatchObject({ kind: "server" });
    expect((await t.manager.getState()).signedIn).toBe(true);
  });

  it("an expired absolute session lifetime requires signing in again without contacting the server", async () => {
    const t = await signedIn();
    t.fetchImpl.mockClear();
    t.advance(31 * 86_400_000);
    await expect(t.manager.getConfig()).rejects.toMatchObject({ kind: "auth" });
    expect(t.calls()).toHaveLength(0);
    expect(await t.manager.getState()).toMatchObject({ signedIn: false, sessionEnded: true });
  });
});

describe("the API client with a session", () => {
  function api(t: ReturnType<typeof setup>) {
    return createApi({ getConfig: () => t.manager.getConfig(), onUnauthorized: (tok) => t.manager.onUnauthorized(tok), fetchImpl: t.fetchImpl });
  }

  it("retries exactly once with a renewed token when the backend answers 401 (e.g. revoked access token)", async () => {
    const seenAuth: string[] = [];
    const t = setup(route({
      "/api/v1/auth/exchange": () => json(tokens()),
      "/api/v1/auth/refresh": () => json(tokens()),
      "/api/v1/auth/me": (init) => {
        const a = (init.headers as Record<string, string>).Authorization;
        seenAuth.push(a);
        return a === "Bearer access-1" ? unauthorized() : json({ id: 1, github_user_id: 5, username: "octocat" });
      },
    }));
    await t.manager.connect("c", false);
    expect(await api(t).getMe()).toMatchObject({ username: "octocat" });
    expect(seenAuth).toEqual(["Bearer access-1", "Bearer access-2"]);
  });

  it("does not loop: a second 401 is reported as an authentication error", async () => {
    let me = 0;
    const t = setup(route({
      "/api/v1/auth/exchange": () => json(tokens()),
      "/api/v1/auth/refresh": () => json(tokens()),
      "/api/v1/auth/me": () => { me++; return unauthorized(); },
    }));
    await t.manager.connect("c", false);
    const err = await api(t).getMe().catch((e) => e as ApiError);
    expect(err).toMatchObject({ kind: "auth", message: "Your session is invalid or has expired." });
    expect(me).toBe(2); // original + one retry, never more
  });

  it("when renewal is refused (revoked session) the 401 surfaces and the session is cleared", async () => {
    const t = setup(route({
      "/api/v1/auth/exchange": () => json(tokens()),
      "/api/v1/auth/refresh": () => unauthorized(),
      "/api/v1/auth/me": () => unauthorized(),
    }));
    await t.manager.connect("c", false);
    await expect(api(t).getMe()).rejects.toMatchObject({ kind: "auth" });
    expect(await t.manager.getState()).toMatchObject({ signedIn: false, sessionEnded: true });
  });

  it("makes no request at all when signed out", async () => {
    const t = setup(route({}));
    await expect(api(t).getMe()).rejects.toMatchObject({ kind: "not_configured" });
    expect(t.calls()).toHaveLength(0);
  });
});

describe("signing out and changing settings", () => {
  it("revokes on the server with the refresh token and wipes local credentials", async () => {
    let logoutBody: Record<string, string> = {};
    const t = setup(route({ "/api/v1/auth/exchange": () => json(tokens()), "/api/v1/auth/logout": (init) => { logoutBody = bodyOf(init); return new Response(null, { status: 204 }); } }));
    await t.manager.connect("c", true);
    await t.manager.signOut();
    expect(logoutBody).toEqual({ refresh_token: "refresh-1" });
    expect(JSON.stringify(t.local.data)).not.toMatch(/refresh-|access-/);
    expect(Object.keys(t.session.data)).toHaveLength(0);
    expect(await t.manager.getState()).toMatchObject({ signedIn: false, sessionEnded: false });
  });

  it("still signs out locally when the server cannot be reached", async () => {
    let offline = false;
    const t = setup(route({ "/api/v1/auth/exchange": () => json(tokens()), "/api/v1/auth/logout": () => { if (offline) throw new TypeError("offline"); return new Response(null, { status: 204 }); } }));
    await t.manager.connect("c", true);
    offline = true;
    await t.manager.signOut();
    expect(JSON.stringify(t.local.data)).not.toContain("refresh-1");
    expect((await t.manager.getConfig()).token).toBeNull();
  });

  it("changing the backend origin signs out, so credentials are never sent to a different server", async () => {
    const t = setup(route({ "/api/v1/auth/exchange": () => json(tokens()) }));
    await t.manager.connect("c", true);
    await t.manager.saveSettings({ baseUrl: "https://other.example.com", useExperimentalScorer: false, keepSignedIn: true });
    expect((await t.manager.getConfig()).token).toBeNull();
    expect(JSON.stringify(t.local.data)).not.toContain("refresh-1");
  });

  it("changing only the scorer keeps the session", async () => {
    const t = setup(route({ "/api/v1/auth/exchange": () => json(tokens()) }));
    await t.manager.connect("c", false);
    await t.manager.saveSettings({ baseUrl: BASE, useExperimentalScorer: true, keepSignedIn: false });
    expect(await t.manager.getConfig()).toMatchObject({ token: "access-1", useExperimentalScorer: true });
  });

  it("refuses to save an unsafe backend URL", async () => {
    const t = setup(route({}));
    for (const baseUrl of ["http://evil.example", "https://user:pw@x.example", "ftp://x"]) {
      await expect(t.manager.saveSettings({ baseUrl, useExperimentalScorer: false, keepSignedIn: false })).rejects.toMatchObject({ kind: "invalid_config" });
    }
  });

  it("tolerates corrupted storage", async () => {
    const t = setup(route({}));
    await t.local.set("compass.settings.v2", "garbage");
    await t.session.set("compass.auth.v2", 42);
    await t.local.set("compass.refresh.v2", ["x"]);
    expect(await t.manager.getSettings()).toEqual({ baseUrl: BASE, useExperimentalScorer: false, keepSignedIn: false });
    expect(await t.manager.getState()).toMatchObject({ signedIn: false });
    expect((await t.manager.getConfig()).token).toBeNull();
  });
});
