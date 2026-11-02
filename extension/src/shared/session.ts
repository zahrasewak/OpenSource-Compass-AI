import { ApiError, exchangeCode, logoutRemote, refreshTokens, validateBaseUrl, type ApiConfig } from "./api";
import type { StorageLike } from "./storage";
import type { TokenPair } from "./authSchemas";

/**
 * Session handling for the extension. Only the background service worker constructs this; the content script and the
 * options page never see a token.
 *
 * Where things live (see docs/AUTHENTICATION.md for the threat model):
 *  - settings (backend origin, scorer, "keep me signed in") .... storage.local   (not secret)
 *  - access token (15 min) ..................................... storage.session (memory only, trusted contexts only)
 *  - refresh token ............................................. storage.session by default;
 *                                                                 storage.local ONLY if the user ticked "Keep me signed in"
 *
 * storage.local is plain text in the browser profile. Nothing here makes it secure; the mitigations are that the
 * persisted credential is short-lived-by-rotation, revocable on the server, and opt-in.
 */

const SETTINGS_KEY = "compass.settings.v2";
const AUTH_KEY = "compass.auth.v2";
const REFRESH_KEY = "compass.refresh.v2";
const ENDED_KEY = "compass.ended.v2";
import { BACKEND_PINNED, DEFAULT_BACKEND_URL } from "./buildinfo";

export const DEFAULT_BASE_URL = DEFAULT_BACKEND_URL;
const SKEW_MS = 30_000; // renew a little before expiry so a request does not race the deadline

export interface Settings {
  baseUrl: string;
  useExperimentalScorer: boolean;
  keepSignedIn: boolean;
}

/** What the UI may learn about the session. Never contains a token. */
export interface SessionState {
  signedIn: boolean;
  /** True when we had a session and the server ended it (revoked, expired, reuse detected). */
  sessionEnded: boolean;
  keepSignedIn: boolean;
  sessionExpiresAt: string | null;
  backendUrl: string;
  experimentalScorer: boolean;
}

interface AuthRecord {
  accessToken: string | null;
  accessExpiresAt: number; // epoch ms
  refreshToken: string | null; // only when not persisted
  sessionExpiresAt: number; // epoch ms
}

interface RefreshRecord {
  refreshToken: string;
  sessionExpiresAt: number;
}

export interface SessionDeps {
  local: StorageLike;
  session: StorageLike;
  fetchImpl?: typeof fetch;
  now?: () => number;
  /** Cross-context mutual exclusion for refresh. Defaults to Web Locks when available. */
  withLock?: <T>(fn: () => Promise<T>) => Promise<T>;
}

const asObject = (v: unknown): Record<string, unknown> => (typeof v === "object" && v !== null ? (v as Record<string, unknown>) : {});

function coerceSettings(raw: unknown): Settings {
  const r = asObject(raw);
  return {
    // A pinned (production) build ignores whatever is stored and always uses its configured backend.
    baseUrl: BACKEND_PINNED ? DEFAULT_BASE_URL : typeof r.baseUrl === "string" && r.baseUrl ? r.baseUrl : DEFAULT_BASE_URL,
    useExperimentalScorer: r.useExperimentalScorer === true,
    keepSignedIn: r.keepSignedIn === true,
  };
}

function defaultLock<T>(fn: () => Promise<T>): Promise<T> {
  const locks = (globalThis as { navigator?: { locks?: { request: (n: string, cb: () => Promise<T>) => Promise<T> } } }).navigator?.locks;
  return locks ? locks.request("compass-session-refresh", fn) : fn();
}

export class SessionManager {
  private inflight: Promise<boolean> | null = null;
  private readonly now: () => number;
  private readonly withLock: <T>(fn: () => Promise<T>) => Promise<T>;

  constructor(private readonly deps: SessionDeps) {
    this.now = deps.now ?? Date.now;
    this.withLock = deps.withLock ?? defaultLock;
  }

  // ------------------------------------------------------------------------------------------ settings

  async getSettings(): Promise<Settings> {
    return coerceSettings(await this.deps.local.get(SETTINGS_KEY));
  }

  /** Changing the backend origin signs out: credentials issued by one server must never be sent to another. */
  async saveSettings(next: Settings): Promise<void> {
    const before = await this.getSettings();
    const baseUrl = validateBaseUrl(next.baseUrl);
    await this.deps.local.set(SETTINGS_KEY, { ...coerceSettings(next), baseUrl });
    if (baseUrl !== before.baseUrl) await this.clearTokens();
  }

  // ------------------------------------------------------------------------------------------ state

  private async loadAuth(): Promise<(AuthRecord & { persisted: boolean }) | null> {
    const a = asObject(await this.deps.session.get(AUTH_KEY));
    const r = asObject(await this.deps.local.get(REFRESH_KEY));
    const sessionRefresh = typeof a.refreshToken === "string" ? a.refreshToken : null;
    const localRefresh = typeof r.refreshToken === "string" ? r.refreshToken : null;
    const refreshToken = sessionRefresh ?? localRefresh;
    const accessToken = typeof a.accessToken === "string" ? a.accessToken : null;
    if (!refreshToken && !accessToken) return null;
    const exp = Number(sessionRefresh ? a.sessionExpiresAt : (r.sessionExpiresAt ?? a.sessionExpiresAt));
    return {
      accessToken,
      accessExpiresAt: Number(a.accessExpiresAt) || 0,
      refreshToken,
      sessionExpiresAt: Number.isFinite(exp) ? exp : 0,
      persisted: !sessionRefresh && Boolean(localRefresh),
    };
  }

  async getState(): Promise<SessionState> {
    const [settings, auth] = await Promise.all([this.getSettings(), this.loadAuth()]);
    const live = auth !== null && Boolean(auth.refreshToken) && auth.sessionExpiresAt > this.now();
    return {
      signedIn: live,
      sessionEnded: !live && (await this.deps.session.get(ENDED_KEY)) === true, // lets the UI say "your session ended"
      keepSignedIn: settings.keepSignedIn,
      sessionExpiresAt: live && auth ? new Date(auth.sessionExpiresAt).toISOString() : null,
      backendUrl: settings.baseUrl,
      experimentalScorer: settings.useExperimentalScorer,
    };
  }

  // ------------------------------------------------------------------------------------------ token use

  /**
   * The configuration for one API call, with an access token that is valid right now (renewed first if needed).
   * Returns token=null when signed out. Throws `auth` if the server has ended the session, or a network/server
   * error if renewal could not be attempted; in that case the refresh token is kept so a later call can retry.
   */
  async getConfig(): Promise<ApiConfig> {
    const settings = await this.getSettings();
    const config = (token: string | null): ApiConfig => ({ baseUrl: settings.baseUrl, token, useExperimentalScorer: settings.useExperimentalScorer });
    let auth = await this.loadAuth();
    if (!auth) return this.signedOut(config);
    if (!auth.accessToken || auth.accessExpiresAt - SKEW_MS <= this.now()) {
      await this.renew(auth.accessToken);
      auth = await this.loadAuth();
      if (!auth) return this.signedOut(config);
    }
    return config(auth.accessToken);
  }

  /** No credentials: "never signed in" is token=null, but a session the server ended is reported as such, consistently. */
  private async signedOut(config: (token: string | null) => ApiConfig): Promise<ApiConfig> {
    if ((await this.deps.session.get(ENDED_KEY)) === true) throw new ApiError("auth", "Your session has ended. Sign in again.", 401);
    return config(null);
  }

  /** A request came back 401. Try to renew once. Resolves true if a different access token is now available. */
  async onUnauthorized(usedToken: string): Promise<boolean> {
    try {
      const auth = await this.loadAuth();
      if (auth?.accessToken && auth.accessToken !== usedToken && auth.accessExpiresAt > this.now()) return true; // someone else already renewed
      return await this.renew(usedToken);
    } catch {
      return false; // the caller then reports the original 401
    }
  }

  /** Single-flight renewal. Resolves true when new tokens were stored. */
  private renew(staleAccessToken: string | null): Promise<boolean> {
    if (!this.inflight) {
      this.inflight = this.withLock(() => this.doRenew(staleAccessToken)).finally(() => {
        this.inflight = null;
      });
    }
    return this.inflight;
  }

  private async doRenew(staleAccessToken: string | null): Promise<boolean> {
    // Re-read inside the lock: another context may have renewed while we waited.
    const auth = await this.loadAuth();
    if (!auth || !auth.refreshToken) return false;
    if (auth.accessToken && auth.accessToken !== staleAccessToken && auth.accessExpiresAt - SKEW_MS > this.now()) return true;
    if (auth.sessionExpiresAt <= this.now()) {
      await this.endSession();
      throw new ApiError("auth", "Your session has ended. Sign in again.", 401);
    }
    const settings = await this.getSettings();
    try {
      const pair = await refreshTokens(settings.baseUrl, auth.refreshToken, this.deps.fetchImpl);
      await this.store(pair, auth.persisted || settings.keepSignedIn);
      await this.deps.session.remove(ENDED_KEY);
      return true;
    } catch (e) {
      // Only a definitive rejection ends the session. A network or server hiccup keeps the refresh token for a later try.
      if (e instanceof ApiError && (e.kind === "auth" || e.kind === "forbidden")) {
        await this.endSession();
        throw new ApiError("auth", "Your session has ended. Sign in again.", 401);
      }
      throw e;
    }
  }

  // ------------------------------------------------------------------------------------------ sign in / out

  /** Trade a one-time pairing code for a session. The code is never stored. */
  async connect(code: string, keepSignedIn: boolean, label: string | null = "Chrome extension"): Promise<SessionState> {
    const settings = await this.getSettings();
    const pair = await exchangeCode(settings.baseUrl, code, label, this.deps.fetchImpl, keepSignedIn);
    await this.saveSettings({ ...settings, keepSignedIn });
    await this.clearTokens();
    await this.store(pair, keepSignedIn);
    await this.deps.session.remove(ENDED_KEY);
    return this.getState();
  }

  /** Revoke on the server (best effort), then forget everything locally no matter what the server said. */
  async signOut(): Promise<void> {
    const auth = await this.loadAuth();
    const settings = await this.getSettings();
    try {
      if (auth?.refreshToken) await logoutRemote(settings.baseUrl, auth.refreshToken, this.deps.fetchImpl);
    } catch {
      /* offline: local credentials are still removed; the server session expires on its own */
    }
    await this.deps.session.remove(ENDED_KEY);
    await this.clearTokens();
  }

  // ------------------------------------------------------------------------------------------ storage helpers

  private async store(pair: TokenPair, persist: boolean): Promise<void> {
    const now = this.now();
    const sessionExpiresAt = Date.parse(pair.session_expires_at);
    const expiresAt = Number.isFinite(sessionExpiresAt) ? sessionExpiresAt : now + pair.refresh_expires_in * 1000;
    const record: AuthRecord = {
      accessToken: pair.access_token,
      accessExpiresAt: now + pair.expires_in * 1000,
      refreshToken: persist ? null : pair.refresh_token,
      sessionExpiresAt: expiresAt,
    };
    await this.deps.session.set(AUTH_KEY, record);
    if (persist) await this.deps.local.set(REFRESH_KEY, { refreshToken: pair.refresh_token, sessionExpiresAt: expiresAt } satisfies RefreshRecord);
    else await this.deps.local.remove(REFRESH_KEY);
  }

  private async clearTokens(): Promise<void> {
    await this.deps.session.remove(AUTH_KEY);
    await this.deps.local.remove(REFRESH_KEY);
  }

  private async endSession(): Promise<void> {
    await this.clearTokens();
    await this.deps.session.set(ENDED_KEY, true);
  }
}
