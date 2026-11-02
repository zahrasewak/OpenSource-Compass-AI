// Fails the build if the packaged extension violates a security or packaging invariant.
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

export function verifyDist(dist, { production = false, backendOrigin = null } = {}) {
  const problems = [];
  const read = (f) => readFileSync(resolve(dist, f), "utf8");
  const manifest = JSON.parse(read("manifest.json"));

  // 1. Every file the manifest points at exists.
  const referenced = [
    manifest.background?.service_worker,
    ...(manifest.content_scripts ?? []).flatMap((c) => c.js ?? []),
    manifest.options_ui?.page,
    ...Object.values(manifest.icons ?? {}),
  ].filter(Boolean);
  for (const f of referenced) if (!existsSync(resolve(dist, f))) problems.push(`manifest references missing file ${f}`);

  // 2. Content script (runs beside GitHub's page) must not contain credential handling or network code.
  const content = read("content.js");
  for (const needle of [
    "compass.config.v1", "compass.auth.v2", "compass.refresh.v2", "Authorization", "Bearer ", "chrome.storage", "chrome.permissions",
    "refresh_token", "access_token", "auth/exchange", "auth/refresh", "auth/logout",
  ]) {
    if (content.includes(needle)) problems.push(`content.js contains "${needle}" (credential handling must stay in the background worker)`);
  }

  // 2b. The panel's stylesheet must be bundled into the content script (it is injected into a shadow root).
  if (!content.includes(":host{all:initial")) problems.push("content.js does not contain the panel stylesheet");

  // 3. Content scripts and the service worker are classic scripts: no module syntax.
  for (const f of ["content.js", "background.js"]) {
    if (/^\s*(import|export)\s/m.test(read(f))) problems.push(`${f} contains module syntax`);
  }

  // 4. The options page must not reference remote resources.
  const html = read("options.html");
  if (/(src|href)="https?:\/\//i.test(html)) problems.push("options.html loads a remote resource");

  // 5. Build hygiene: no source maps and nothing that looks like a secret anywhere in the output.
  const all = [];
  const walk = (dir) => {
    for (const e of readdirSync(dir, { withFileTypes: true })) {
      const p = resolve(dir, e.name);
      if (e.isDirectory()) walk(p);
      else all.push(p);
    }
  };
  walk(dist);
  for (const f of all) {
    const rel = f.slice(dist.length + 1);
    if (/\.map$/i.test(rel)) problems.push(`${rel} is a source map (they must not be shipped)`);
    if (/\.(js|css|html|json)$/i.test(rel)) {
      const text = readFileSync(f, "utf8");
      if (/sourceMappingURL=/.test(text)) problems.push(`${rel} references a source map`);
      for (const [label, re] of [
        ["a GitHub token", /\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|github_pat_[A-Za-z0-9_]{30,}/],
        ["a private key", /-----BEGIN [A-Z ]*PRIVATE KEY-----/],
        ["a JWT", /\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/],
        ["the server secret setting", /COMPASS_AUTH_SECRET|auth_secret/i],
        ["a cloud access key", /\bAKIA[0-9A-Z]{16}\b/],
      ]) if (re.test(text)) problems.push(`${rel} contains ${label}`);
    }
  }

  // 6. A production build is pinned to one https backend and knows no development URL.
  if (production) {
    const m = JSON.parse(read("manifest.json"));
    if (JSON.stringify(m.host_permissions) !== JSON.stringify([`${backendOrigin}/*`])) problems.push(`production host_permissions must be exactly ["${backendOrigin}/*"]`);
    if (m.optional_host_permissions) problems.push("production manifest must not declare optional host permissions");
    for (const f of ["manifest.json", "background.js", "options.html", ...all.filter((x) => /assets\//.test(x.replace(/\\/g, "/"))).map((x) => x.slice(dist.length + 1))]) {
      if (/http:\/\/(localhost|127\.0\.0\.1)/.test(read(f))) problems.push(`${f} contains a development URL`);
    }
  }

  if (problems.length) throw new Error("dist verification failed:\n - " + problems.join("\n - "));
}
