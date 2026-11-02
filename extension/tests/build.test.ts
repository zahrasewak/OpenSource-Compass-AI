import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { afterAll, describe, expect, it, vi } from "vitest";

const root = resolve(__dirname, "..");
const out = mkdtempSync(resolve(tmpdir(), "compass-prod-"));
afterAll(() => rmSync(out, { recursive: true, force: true }));

function build(env: Record<string, string>, ...args: string[]) {
  return spawnSync(process.execPath, [resolve(root, "scripts/build.mjs"), ...args], { cwd: root, env: { ...process.env, ...env }, encoding: "utf8" });
}
function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? files(resolve(dir, e.name)) : [resolve(dir, e.name)]));
}

describe("production build (pinned backend)", () => {
  it("refuses to build without a backend origin, or with an insecure or malformed one", { timeout: 120_000 }, () => {
    for (const bad of ["", "http://compass.example.com", "https://user:pw@compass.example.com", "https://compass.example.com/api", "not a url"]) {
      const r = build({ COMPASS_BACKEND_URL: bad }, "--production", "--out", resolve(out, "bad"));
      expect(r.status, `COMPASS_BACKEND_URL=${bad}`).not.toBe(0);
      expect(r.stderr).toMatch(/COMPASS_BACKEND_URL/);
    }
    expect(existsSync(resolve(out, "bad", "manifest.json"))).toBe(false);
  });

  it("pins the manifest to the one https backend: no localhost, no optional wildcard, no source maps, no secrets", () => {
    const dist = resolve(out, "good");
    const r = build({ COMPASS_BACKEND_URL: "https://compass.example.com/" }, "--production", "--out", dist);
    expect(r.status, r.stderr).toBe(0);
    const manifest = JSON.parse(readFileSync(resolve(dist, "manifest.json"), "utf8"));
    expect(manifest.host_permissions).toEqual(["https://compass.example.com/*"]);
    expect(manifest.optional_host_permissions).toBeUndefined();
    expect(manifest.permissions).toEqual(["storage"]);
    expect(manifest.content_scripts[0].matches).toEqual(["https://github.com/*"]);
    expect(JSON.stringify(manifest)).not.toMatch(/localhost|127\.0\.0\.1/);
    for (const f of files(dist)) {
      expect(f).not.toMatch(/\.map$/);
      if (/\.(js|html|css|json)$/.test(f)) expect(readFileSync(f, "utf8")).not.toMatch(/sourceMappingURL|http:\/\/localhost|http:\/\/127\./);
    }
    expect(readFileSync(resolve(dist, "background.js"), "utf8")).toContain("https://compass.example.com");
  }, 180_000);

  it("the development build keeps the localhost defaults (and is the one the e2e suite loads)", () => {
    const manifest = JSON.parse(readFileSync(resolve(root, "manifest.json"), "utf8"));
    expect(manifest.host_permissions).toEqual(["http://localhost:8000/*", "http://127.0.0.1:8000/*"]);
  });

  it("verify-dist rejects a shipped source map and an embedded token", () => {
    const dist = resolve(out, "dirty");
    execFileSync(process.execPath, [resolve(root, "scripts/build.mjs"), "--out", dist], { cwd: root, env: { ...process.env, COMPASS_BUILD: "" } });
    const verify = () =>
      spawnSync(process.execPath, ["--input-type=module", "-e", `import { verifyDist } from ${JSON.stringify(pathToFileURL(resolve(root, "scripts/verify-dist.mjs")).href)}; verifyDist(${JSON.stringify(dist)});`], { encoding: "utf8" });
    expect(verify().status).toBe(0);
    writeFileSync(resolve(dist, "x.js.map"), "{}");
    const map = verify();
    expect(map.status).not.toBe(0);
    expect(map.stderr).toMatch(/source map/);
    rmSync(resolve(dist, "x.js.map"));
    writeFileSync(resolve(dist, "leak.js"), "const t='ghp_" + "a".repeat(36) + "';");
    const leak = verify();
    expect(leak.status).not.toBe(0);
    expect(leak.stderr).toMatch(/GitHub token/);
  }, 180_000);
});

describe("a pinned build refuses other backends", () => {
  it("validateBaseUrl only accepts the pinned https origin", async () => {
    vi.resetModules();
    vi.stubGlobal("__COMPASS_BACKEND_URL__", "https://compass.example.com");
    vi.stubGlobal("__COMPASS_PINNED__", true);
    const { validateBaseUrl } = await import("../src/shared/api");
    expect(validateBaseUrl("https://compass.example.com")).toBe("https://compass.example.com");
    for (const other of ["https://evil.example", "http://localhost:8000", "http://compass.example.com"]) {
      expect(() => validateBaseUrl(other), other).toThrow();
    }
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("a stored development URL is ignored by a pinned build", async () => {
    vi.resetModules();
    vi.stubGlobal("__COMPASS_BACKEND_URL__", "https://compass.example.com");
    vi.stubGlobal("__COMPASS_PINNED__", true);
    const { SessionManager } = await import("../src/shared/session");
    const { memoryStorage } = await import("../src/shared/storage");
    const local = memoryStorage({ "compass.settings.v2": { baseUrl: "http://localhost:8000", useExperimentalScorer: true, keepSignedIn: false } });
    const m = new SessionManager({ local, session: memoryStorage() });
    expect((await m.getSettings()).baseUrl).toBe("https://compass.example.com");
    vi.unstubAllGlobals();
    vi.resetModules();
  });
});
