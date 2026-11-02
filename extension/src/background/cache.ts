import { ApiError } from "../shared/api";

/**
 * Short-lived, in-memory response cache for the background worker. Its purpose is to keep the extension from
 * generating backend (and therefore GitHub) traffic: navigating between pages, reopening a panel or collapsing and
 * expanding sections must not repeat requests that were just answered.
 *
 *  - fresh hit: the stored value is returned without any request
 *  - in flight: concurrent identical requests share one promise
 *  - rate limited: the failure is remembered until the server's Retry-After (default 60 s), and calls for the same key
 *    fail immediately instead of probing the backend again. There is no automatic retry; the user decides.
 *
 * Only successful responses are cached (errors other than rate limiting are not). The cache is memory-only, so it
 * disappears when the service worker stops, and it is cleared on sign-in, sign-out and settings changes.
 */

const DEFAULT_BLOCK_SECONDS = 60;
const MAX_BLOCK_SECONDS = 3600;
const MAX_ENTRIES = 200;

interface Entry {
  value: unknown;
  expiresAt: number;
}

export class ResponseCache {
  private readonly entries = new Map<string, Entry>();
  private readonly inflight = new Map<string, Promise<unknown>>();
  private readonly blocked = new Map<string, number>();
  private generation = 0; // bumped by clear(): results of requests started before it are not stored

  constructor(private readonly now: () => number = Date.now) {}

  async get<T>(key: string, ttlMs: number, load: () => Promise<T>): Promise<T> {
    const hit = this.entries.get(key);
    if (hit && hit.expiresAt > this.now()) return hit.value as T;

    const until = this.blocked.get(key);
    if (until !== undefined) {
      if (until > this.now()) {
        const seconds = Math.max(1, Math.ceil((until - this.now()) / 1000));
        throw new ApiError("rate_limited", "Rate limited. Not asking again until the limit resets.", 429, undefined, seconds);
      }
      this.blocked.delete(key);
    }

    const running = this.inflight.get(key);
    if (running) return running as Promise<T>;

    const generation = this.generation;
    const promise = (async () => {
      try {
        const value = await load();
        if (ttlMs > 0 && generation === this.generation) this.remember(key, value, ttlMs);
        return value;
      } catch (e) {
        if (e instanceof ApiError && e.kind === "rate_limited") {
          const seconds = Math.min(e.retryAfterSeconds ?? DEFAULT_BLOCK_SECONDS, MAX_BLOCK_SECONDS);
          this.blocked.set(key, this.now() + seconds * 1000);
        }
        throw e;
      } finally {
        this.inflight.delete(key);
      }
    })();
    this.inflight.set(key, promise);
    return promise;
  }

  private remember(key: string, value: unknown, ttlMs: number) {
    if (this.entries.size >= MAX_ENTRIES) {
      const oldest = this.entries.keys().next().value;
      if (oldest !== undefined) this.entries.delete(oldest);
    }
    this.entries.set(key, { value, expiresAt: this.now() + ttlMs });
  }

  /** Remember a value computed elsewhere (for example the stored match id after the first action). */
  put(key: string, value: unknown, ttlMs: number) {
    this.remember(key, value, ttlMs);
  }

  peek<T>(key: string): T | undefined {
    const hit = this.entries.get(key);
    return hit && hit.expiresAt > this.now() ? (hit.value as T) : undefined;
  }

  delete(key: string) {
    this.entries.delete(key);
  }

  clear() {
    this.entries.clear();
    this.blocked.clear();
    this.generation++;
  }
}
