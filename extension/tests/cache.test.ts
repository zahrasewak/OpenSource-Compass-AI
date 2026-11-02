import { describe, expect, it, vi } from "vitest";
import { ResponseCache } from "../src/background/cache";
import { ApiError } from "../src/shared/api";

function clock() {
  let t = 1_000_000;
  return { now: () => t, advance: (ms: number) => void (t += ms) };
}

describe("ResponseCache", () => {
  it("returns a fresh value without calling the loader, and reloads after the ttl", async () => {
    const c = clock();
    const cache = new ResponseCache(c.now);
    const load = vi.fn(async () => ({ v: load.mock.calls.length }));
    expect(await cache.get("k", 1000, load)).toEqual({ v: 1 });
    expect(await cache.get("k", 1000, load)).toEqual({ v: 1 });
    c.advance(1001);
    expect(await cache.get("k", 1000, load)).toEqual({ v: 2 });
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("shares one in-flight call between concurrent requests, including when ttl is 0", async () => {
    const cache = new ResponseCache();
    let release!: (v: number) => void;
    const load = vi.fn(() => new Promise<number>((r) => (release = r)));
    const all = Promise.all([cache.get("k", 0, load), cache.get("k", 0, load), cache.get("k", 0, load)]);
    release(7);
    expect(await all).toEqual([7, 7, 7]);
    expect(load).toHaveBeenCalledTimes(1);
    await cache.get("k", 0, async () => 8); // ttl 0 stores nothing
    expect(cache.peek("k")).toBeUndefined();
  });

  it("does not cache failures, so the next user-initiated call tries again", async () => {
    const cache = new ResponseCache();
    const load = vi.fn<() => Promise<string>>().mockRejectedValueOnce(new ApiError("network", "x")).mockResolvedValue("ok");
    await expect(cache.get("k", 1000, load)).rejects.toMatchObject({ kind: "network" });
    expect(await cache.get("k", 1000, load)).toBe("ok");
  });

  it("after a rate limit, fails fast with the remaining wait until Retry-After passes, then allows one probe", async () => {
    const c = clock();
    const cache = new ResponseCache(c.now);
    const load = vi.fn<() => Promise<string>>()
      .mockRejectedValueOnce(new ApiError("rate_limited", "limit", 429, undefined, 120))
      .mockResolvedValue("ok");
    await expect(cache.get("k", 1000, load)).rejects.toMatchObject({ kind: "rate_limited", retryAfterSeconds: 120 });
    c.advance(30_000);
    await expect(cache.get("k", 1000, load)).rejects.toMatchObject({ kind: "rate_limited", retryAfterSeconds: 90 });
    await expect(cache.get("k", 1000, load)).rejects.toMatchObject({ kind: "rate_limited" });
    expect(load).toHaveBeenCalledTimes(1);
    c.advance(91_000);
    expect(await cache.get("k", 1000, load)).toBe("ok");
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("uses a default wait when the server gave none and caps absurd waits", async () => {
    const c = clock();
    const cache = new ResponseCache(c.now);
    await expect(cache.get("a", 1, async () => { throw new ApiError("rate_limited", "x", 429); })).rejects.toBeInstanceOf(ApiError);
    await expect(cache.get("a", 1, async () => "no")).rejects.toMatchObject({ retryAfterSeconds: 60 });
    await expect(cache.get("b", 1, async () => { throw new ApiError("rate_limited", "x", 429, undefined, 10 ** 9); })).rejects.toBeInstanceOf(ApiError);
    await expect(cache.get("b", 1, async () => "no")).rejects.toMatchObject({ retryAfterSeconds: 3600 });
  });

  it("a rate limit on one key does not block other keys", async () => {
    const cache = new ResponseCache();
    await expect(cache.get("a", 1000, async () => { throw new ApiError("rate_limited", "x", 429, undefined, 60); })).rejects.toBeInstanceOf(ApiError);
    expect(await cache.get("b", 1000, async () => "fine")).toBe("fine");
  });

  it("clear() drops values and blocks, and a request already in flight cannot repopulate the cache for the next session", async () => {
    const cache = new ResponseCache();
    await cache.get("k", 1000, async () => "old");
    let release!: (v: string) => void;
    const slow = cache.get("slow", 1000, () => new Promise<string>((r) => (release = r)));
    cache.clear();
    expect(cache.peek("k")).toBeUndefined();
    release("from-old-session");
    await slow;
    expect(cache.peek("slow")).toBeUndefined();
  });

  it("is bounded", async () => {
    const cache = new ResponseCache();
    for (let i = 0; i < 300; i++) cache.put(`k${i}`, i, 60_000);
    expect(cache.peek("k0")).toBeUndefined();
    expect(cache.peek("k299")).toBe(299);
  });
});
