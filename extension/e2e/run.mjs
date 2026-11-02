// Browser-level end-to-end run: real Chromium + the built unpacked extension + the real FastAPI backend (SQLite, fictional data).
// github.com pages are served by Playwright request interception, so the real github.com is never modified or even contacted.
// (A separate script, e2e/real-github.mjs, is for pages on the real github.com.)
//
//   npm run build && node e2e/run.mjs        (needs backend deps installed and Playwright's Chromium)
//
// Environment: CHROMIUM_PATH (optional) path to a Chromium binary that still honours --load-extension.
//
// Sign-in uses a one-time pairing code issued by the backend CLI, exactly as a real user would. The access token is made
// short-lived (45 s) so token renewal and expiry are exercised for real, not mocked.
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const here = dirname(fileURLToPath(import.meta.url));
const extDir = resolve(here, "..");
const backendDir = resolve(extDir, "../backend");
const dist = resolve(extDir, "dist");
const work = resolve(here, ".tmp");
// BROWSER=chromium (default: Playwright's Chromium) | chrome (installed Google Chrome) | msedge (installed Microsoft Edge)
// COLOR_SCHEME=light (default) | dark: emulates the OS/browser colour scheme (prefers-color-scheme)
const BROWSER = process.env.BROWSER ?? "chromium";
const SCHEME = process.env.COLOR_SCHEME ?? "light";
const shots = resolve(here, BROWSER === "chromium" && SCHEME === "light" ? "screenshots" : `screenshots/${BROWSER}-${SCHEME}`);
const PORT = 8000;
const BASE = `http://localhost:${PORT}`;
const ACCESS_TTL = 45; // seconds
const SKEW = 30; // the extension renews this many seconds before expiry
const python = resolve(backendDir, process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
const chromiumPath =
  process.env.CHROMIUM_PATH ??
  resolve(process.env.LOCALAPPDATA ?? "", "ms-playwright/chromium-1234/chrome-win64/chrome.exe");

assert.ok(existsSync(resolve(dist, "manifest.json")), "build the extension first: npm run build");
assert.ok(BROWSER !== "chromium" || existsSync(chromiumPath), `Chromium not found at ${chromiumPath}; set CHROMIUM_PATH`);
rmSync(work, { recursive: true, force: true });
mkdirSync(work, { recursive: true });
mkdirSync(shots, { recursive: true });

const dbFile = resolve(work, "e2e.db");
const env = {
  ...process.env,
  COMPASS_DATABASE_URL: `sqlite:///${dbFile.replace(/\\/g, "/")}`,
  COMPASS_AUTH_SECRET: randomBytes(32).toString("hex"),
  COMPASS_AUTH_ACCESS_TTL_SECONDS: String(ACCESS_TTL),
  COMPASS_GITHUB_MAX_RATE_LIMIT_WAIT_SECONDS: "1",
  COMPASS_GITHUB_TIMEOUT_SECONDS: "5",
  COMPASS_GITHUB_MAX_RETRIES: "0",
  PYTHONIOENCODING: "utf-8",
};
// Never let a personal GitHub credential from the environment reach this run.
delete env.COMPASS_GITHUB_TOKEN;
delete env.GITHUB_TOKEN;

const results = [];
const step = async (name, fn) => {
  try {
    await fn();
    results.push([name, "PASS"]);
    console.log(`PASS  ${name}`);
  } catch (err) {
    results.push([name, "FAIL", err.message]);
    console.log(`FAIL  ${name}\n      ${String(err.message).split("\n")[0]}`);
  }
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ------------------------------------------------------------------ backend
const cli = (...args) => execFileSync(python, ["-m", "app.cli", ...args], { cwd: backendDir, env, stdio: ["ignore", "pipe", "pipe"] }).toString();
const pairingCode = () => cli("issue-pairing-code", "--github-id", "1", "--username", "e2e-user").trim().split("\n")[0].trim();

execFileSync(python, ["-m", "alembic", "upgrade", "head"], { cwd: backendDir, env, stdio: "pipe" });
execFileSync(python, [resolve(here, "seed.py")], { cwd: backendDir, env });
let server = spawn(python, ["-m", "uvicorn", "app.main:app", "--port", String(PORT), "--log-level", "warning"], { cwd: backendDir, env, stdio: "pipe" });
let serverLog = "";
server.stderr.on("data", (d) => (serverLog += d));
const waitReady = async () => {
  for (let i = 0; ; i++) {
    try {
      if ((await fetch(`${BASE}/ready`)).ok) return;
    } catch { /* not up yet */ }
    if (i > 60) throw new Error("backend did not start:\n" + serverLog);
    await sleep(500);
  }
};
await waitReady();
console.log("backend ready; running browser checks\n");
// Stop the backend and start it again, optionally behind the test-only delay wrapper (e2e/slow_app.py).
const restartServer = async (delays = "") => {
  server.kill();
  await sleep(800);
  const args = delays ? ["-m", "uvicorn", "slow_app:app", "--app-dir", here, "--port", String(PORT), "--log-level", "warning"] : ["-m", "uvicorn", "app.main:app", "--port", String(PORT), "--log-level", "warning"];
  server = spawn(python, args, { cwd: backendDir, env: { ...env, E2E_DELAYS: delays }, stdio: "pipe" });
  server.stderr.on("data", (d) => (serverLog += d));
  await waitReady();
};

const query = (sql, ...args) => {
  const db = new DatabaseSync(dbFile, { readOnly: true });
  try { return db.prepare(sql).all(...args); } finally { db.close(); }
};
// Direct HTTP to the backend from the test runner. uvicorn closes idle keep-alive sockets after 5 s; Node's pooled socket can be
// reused at that exact moment and fail with ECONNRESET (a test-client artefact seen intermittently before step 23b), so a connection
// reset BEFORE any response is retried once with a fresh socket.
const api = async (path, { method = "GET", token, body } = {}) => {
  const once = () => fetch(BASE + path, {
    method,
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  try {
    return await once();
  } catch (e) {
    if (!/ECONNRESET|UND_ERR_SOCKET|other side closed/i.test(`${e.cause?.code ?? ""} ${e.cause?.message ?? ""} ${e.message}`)) throw new Error(`fetch ${method} ${path} failed: ${e.cause?.code ?? e.message}`);
    return once();
  }
};

// GitHub's issue markup differs between its UI generations. `layout` picks what the controlled page imitates:
//   legacy (#partial-discussion-sidebar), pane (data-testid=issue-viewer-metadata-pane), layout (.Layout-sidebar),
//   none (no sidebar: the panel floats), late (pane appears 1.5 s after load, as seen on the real site after back-navigation).
const issuePage = (path, layout) => {
  const l = layout ?? (path.endsWith("/7") ? "legacy" : "none");
  const sidebars = {
    legacy: '<div id="partial-discussion-sidebar" style="width:296px"><div id="gh-sidebar-item">Assignees</div></div>',
    pane: '<div data-testid="issue-viewer-metadata-pane" style="width:296px"><div id="gh-sidebar-item">Assignees</div></div>',
    layout: '<div class="Layout-sidebar" style="width:296px"><div id="gh-sidebar-item">Assignees</div></div>',
    none: "", late: "",
  };
  const late = l === "late" ? "<script>setTimeout(() => { const d = document.createElement('div'); d.setAttribute('data-testid', 'issue-viewer-metadata-pane'); document.querySelector('main').append(d); }, 1500);</script>" : "";
  return `<!doctype html><html lang="en" data-color-mode="light"><head><title>${path}</title></head><body>
    <header>GitHub (controlled test page)</header>
    <main><h1 id="gh-title">Issue page ${path}</h1><div id="gh-content"><p>Original GitHub content.</p></div>
    ${sidebars[l]}</main>${late}</body></html>`;
};

const launchOptions = {
  headless: !process.env.HEADED,
  viewport: { width: 1280, height: 900 },
  colorScheme: SCHEME,
};
if (BROWSER === "chromium") {
  Object.assign(launchOptions, {
    executablePath: chromiumPath,
    args: [`--disable-extensions-except=${dist}`, `--load-extension=${dist}`, "--no-first-run", "--no-default-browser-check"],
  });
} else if (BROWSER === "msedge") {
  // Edge 156 still honours --load-extension when this feature switch is disabled.
  Object.assign(launchOptions, {
    channel: "msedge", ignoreDefaultArgs: ["--disable-extensions"],
    args: [`--disable-extensions-except=${dist}`, `--load-extension=${dist}`, "--disable-features=DisableLoadExtensionCommandLineSwitch", "--no-first-run"],
  });
} else if (BROWSER === "chrome") {
  // Branded Chrome 137+ removed --load-extension; load the unpacked extension through DevTools instead.
  Object.assign(launchOptions, { channel: "chrome", ignoreDefaultArgs: ["--disable-extensions"], args: ["--enable-unsafe-extension-debugging", "--no-first-run"] });
} else throw new Error(`unknown BROWSER ${BROWSER}`);

const context = await chromium.launchPersistentContext(resolve(work, "profile"), launchOptions);
const browserVersion = context.browser()?.version() ?? "unknown";
await context.route("https://github.com/**", (route) => { const u = new URL(route.request().url()); return route.fulfill({ contentType: "text/html", body: issuePage(u.pathname, u.searchParams.get("layout")) }); });

let extId;
if (BROWSER === "chrome") {
  extId = (await (await context.browser().newBrowserCDPSession()).send("Extensions.loadUnpacked", { path: dist })).id;
  const wake = await context.newPage(); // an MV3 service worker only starts on demand
  await wake.goto(`chrome-extension://${extId}/options.html`);
  await wake.close();
} else {
  let [worker] = context.serviceWorkers();
  worker ??= await context.waitForEvent("serviceworker", { timeout: 15000 });
  extId = new URL(worker.url()).host;
}
console.log(`browser: ${BROWSER} ${browserVersion}, colour scheme: ${SCHEME}`);
console.log(`extension loaded: id ${extId}\n`);

const pageErrors = [];
const open = async (path) => {
  const page = await context.newPage();
  page.on("pageerror", (e) => pageErrors.push(e.message));
  await page.goto(`https://github.com${path}`);
  return page;
};
const panel = (page) => page.locator("#compass-ai-host");
const optionsPage = async (tab) => {
  const page = await context.newPage();
  await page.goto(`chrome-extension://${extId}/options.html`);
  if (tab) await page.getByRole("tab", { name: tab }).click();
  return page;
};

// A page of the extension itself, kept open only to inspect chrome.storage (the same view the extension's own code has).
let inspector = await context.newPage();
await inspector.goto(`chrome-extension://${extId}/options.html`);
const storage = () => inspector.evaluate(async () => ({ session: await chrome.storage.session.get(null), local: await chrome.storage.local.get(null) }));
const liveTokens = async () => {
  const { session, local } = await storage();
  const auth = session["compass.auth.v2"] ?? {};
  const refresh = local["compass.refresh.v2"] ?? {};
  return { access: auth.accessToken ?? null, refresh: auth.refreshToken ?? refresh.refreshToken ?? null, accessExpiresAt: auth.accessExpiresAt ?? 0 };
};
const tokenFreePage = async (page, ...secrets) => {
  const html = await page.evaluate(() => document.documentElement.outerHTML + (document.getElementById("compass-ai-host")?.shadowRoot?.innerHTML ?? ""));
  const { access, refresh } = await liveTokens();
  for (const s of [access, refresh, ...secrets].filter(Boolean)) assert.ok(!html.includes(s), "a credential must never appear in the page");
};
const signIn = async ({ keep = false, code = pairingCode() } = {}) => {
  const page = await optionsPage();
  await page.locator("#code").fill(code);
  if (keep) await page.getByLabel(/Keep me signed in/).check();
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByText("Signed in.", { exact: true }).waitFor();
  await page.close();
};

await step("0. fresh installation: the extension starts signed out with the default backend (and the settings page opens by itself)", async () => {
  const autoOpened = context.pages().some((p) => p.url().includes("options.html")) || (await context.waitForEvent("page", { timeout: 4000 }).then((p) => p.url().includes("options.html")).catch(() => false));
  console.log(`      settings page opened automatically on install: ${autoOpened ? "yes" : "no (browser did not fire the install event for this load method)"}`);
  const page = await optionsPage();
  await page.locator("#code").waitFor();
  assert.equal(await page.locator("#baseUrl").inputValue(), "http://localhost:8000", "default backend");
  assert.equal(await page.getByRole("tab", { name: "Profile" }).isDisabled(), true, "profile tab locked while signed out");
  assert.equal(await page.getByRole("tab", { name: "Saved issues" }).isDisabled(), true);
  const { session, local } = await storage();
  assert.ok(!session["compass.auth.v2"] && !local["compass.refresh.v2"], "no credentials");
  await page.screenshot({ path: resolve(shots, "0-fresh-install.png") });
  await page.close();
});

await step("1. panel appears on an issue page and says to sign in (no session yet)", async () => {
  const page = await open("/acme/ml-api/issues/7");
  await panel(page).getByText("Sign in to see scores.").waitFor({ timeout: 10000 });
  assert.equal(await page.locator("#gh-content").innerText(), "Original GitHub content.", "GitHub's own content is untouched");
  assert.equal(await page.locator("#partial-discussion-sidebar > #compass-ai-host").count(), 1, "panel is placed in GitHub's sidebar");
  assert.equal(await page.locator("#partial-discussion-sidebar > :first-child").getAttribute("id"), "compass-ai-host");
  await page.screenshot({ path: resolve(shots, "1-signed-out.png") });
  await page.close();
});

await step("2. a wrong pairing code is rejected by the real backend and nothing is stored", async () => {
  const page = await optionsPage();
  for (const wrong of ["TYPO", "NOT-A-REAL-CODE-BUT-LONG-ENOUGH-TO-BE-WELL-FORMED"]) { // too short for the API (422) and well-formed but unknown (401)
    await page.locator("#code").fill(wrong);
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.getByRole("alert").filter({ hasText: "pairing code is invalid, expired or already used" }).waitFor();
    assert.equal(await page.locator("#code").inputValue(), "", "the code is cleared from the page");
    await page.getByRole("button", { name: "Sign in" }).waitFor();
  }
  await page.screenshot({ path: resolve(shots, "2-options-wrong-code.png") });
  await page.close();
  const { session, local } = await storage();
  assert.ok(!session["compass.auth.v2"] && !local["compass.refresh.v2"], "no credentials stored");
  assert.equal(query("SELECT count(*) AS c FROM auth_sessions")[0].c, 0);
});

let firstCode;
await step("3. the right pairing code signs in once; reusing it fails; credentials sit only where the design says", async () => {
  firstCode = pairingCode();
  const page = await optionsPage();
  await page.locator("#code").fill(firstCode);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByText("Signed in.", { exact: true }).waitFor();
  await page.getByTestId("session-info").getByText("e2e-user").waitFor();
  const text = await page.locator("body").innerText();
  await page.screenshot({ path: resolve(shots, "3-options-signed-in.png") });
  await page.close();
  const t = await liveTokens();
  assert.ok(t.access && t.refresh, "tokens are in session storage");
  assert.ok(!text.includes(t.access) && !text.includes(t.refresh) && !text.includes(firstCode), "no credential is displayed");
  const { session, local } = await storage();
  assert.ok(session["compass.auth.v2"].refreshToken, "refresh token is in memory-only session storage by default");
  assert.ok(!local["compass.refresh.v2"], "nothing secret is written to persistent storage by default");
  assert.ok(!JSON.stringify(local).includes(t.access) && !JSON.stringify(local).includes(t.refresh));
  const again = await api("/api/v1/auth/exchange", { method: "POST", body: { code: firstCode } });
  assert.equal(again.status, 401, "a pairing code is single use");
  const rows = query("SELECT refresh_hash, revoked_at FROM auth_sessions");
  assert.equal(rows.length, 1);
  assert.notEqual(rows[0].refresh_hash, t.refresh, "the server stores only a hash of the refresh token");
  assert.equal(rows[0].revoked_at, null);
});

const page7 = await open("/acme/ml-api/issues/7");

await step("4. match score, breakdown and honest labelling (heuristic, unknown parts shown as unknown)", async () => {
  const score = panel(page7).getByLabel(/^Match score \d+ out of 100$/);
  await score.waitFor({ timeout: 20000 });
  const n = Number((await score.getAttribute("aria-label")).match(/\d+/)[0]);
  assert.ok(n > 0 && n <= 100, `score ${n}`);
  await panel(page7).getByText("Heuristic v1").waitFor();
  await panel(page7).getByText("Parser crashes on load").first().waitFor();
  await panel(page7).getByText("Why it matches").waitFor(); // the reasons are up front, the numbers are behind a toggle
  assert.equal(await panel(page7).getByRole("progressbar", { name: "Skill compatibility" }).count(), 0, "the breakdown starts closed");
  await panel(page7).getByRole("button", { name: /Score breakdown/ }).click();
  await panel(page7).getByText(/not an ML prediction/).first().waitFor();
  const bars = await panel(page7).getByRole("progressbar", { name: "Skill compatibility" }).getAttribute("aria-valuenow");
  assert.ok(Number(bars) > 0, "skill compatibility has a real score for a user with matching skills");
  await tokenFreePage(page7);
});

await step("4b. the panel's colours follow the colour scheme (computed styles, not just a screenshot)", async () => {
  const c = await panel(page7).locator(".panel").evaluate((el) => {
    const cs = getComputedStyle(el);
    return { bg: cs.backgroundColor, fg: cs.color };
  });
  const lum = (rgb) => {
    const [r, g, b] = rgb.match(/\d+/g).slice(0, 3).map((v) => { const x = Number(v) / 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const [lb, lf] = [lum(c.bg), lum(c.fg)];
  const ratio = (Math.max(lb, lf) + 0.05) / (Math.min(lb, lf) + 0.05);
  console.log(`      scheme=${SCHEME} panel background ${c.bg}, text ${c.fg}, contrast ${ratio.toFixed(1)}:1`);
  assert.ok(SCHEME === "dark" ? lb < 0.2 : lb > 0.8, `background should be ${SCHEME}: ${c.bg}`);
  assert.ok(ratio >= 4.5, `text contrast ${ratio.toFixed(2)} is below 4.5:1`);
  await page7.screenshot({ path: resolve(shots, `4b-panel-${SCHEME}.png`) });
});

await step("4c. settings page text is readable in this colour scheme (WCAG AA contrast of every text element sampled, on every tab)", async () => {
  const sample = (page) => page.evaluate(() => {
    const parse = (c) => c.match(/[\d.]+/g).map(Number);
    const lum = (rgb) => { const [r, g, b] = rgb.slice(0, 3).map((v) => { const x = v / 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; }); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
    const bgOf = (el) => { for (let e = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor); if (c.length < 4 || c[3] > 0.99) return c; } return [255, 255, 255]; };
    const out = [];
    for (const el of document.querySelectorAll("h1, h2, h3, h4, label, legend, summary, button, select, input, .help, .badge, .score, .repo, p, a, li, [role=tab]")) {
      if (!el.textContent.trim() && !["INPUT", "SELECT"].includes(el.tagName)) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === "hidden" || el.getBoundingClientRect().width === 0) continue;
      if (el.tagName === "LI" && el.children.length) continue; // containers; their text elements are sampled themselves
      const fg = parse(cs.color), bg = bgOf(el);
      const [x, y] = [lum(fg), lum(bg)];
      out.push({ what: `${el.tagName.toLowerCase()}${el.getAttribute("role") ? "[" + el.getAttribute("role") + "]" : ""}${el.className && typeof el.className === "string" ? "." + el.className : ""} "${(el.textContent || el.value || "").trim().slice(0, 24)}"`, ratio: (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05) });
    }
    return out;
  });
  const all = [];
  for (const tab of ["Find issues", "Profile", "Account"]) {
    const page = await optionsPage(tab);
    if (tab === "Find issues") {
      await page.locator(".issues, .empty").first().waitFor({ timeout: 20000 });
      const details = page.getByRole("button", { name: /^Details/ }).first();
      if (await details.count()) { await details.click(); await page.getByText(/Difficulty:/).first().waitFor({ timeout: 15000 }); }
    }
    if (tab === "Profile") {
      await page.getByLabel("Add an interest").waitFor();
      await page.getByText("More preferences (optional)").click();
    }
    const rows = await sample(page);
    await page.screenshot({ path: resolve(shots, `4c-${tab.toLowerCase().replace(/ /g, "-")}-${SCHEME}.png`), fullPage: true });
    await page.close();
    all.push(...rows.map((r) => ({ ...r, tab })));
  }
  const bad = all.filter((r) => r.ratio < 4.5);
  console.log(`      sampled ${all.length} elements on 3 tabs, lowest contrast ${Math.min(...all.map((r) => r.ratio)).toFixed(2)}:1`);
  assert.equal(bad.length, 0, "below 4.5:1: " + bad.map((b) => `${b.tab}: ${b.what} ${b.ratio.toFixed(2)}`).join("; "));
});

await step("4d. no horizontal overflow at narrow widths (640 px ~ 200% zoom, 400 px phone-like)", async () => {
  for (const tab of ["Find issues", "Account", "Profile", "Saved issues"]) {
    for (const width of [640, 400]) {
      const page = await optionsPage(tab);
      await page.setViewportSize({ width, height: 900 });
      await page.waitForTimeout(150);
      const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      await page.close();
      assert.ok(over <= 0, `settings page "${tab}" overflows by ${over}px at ${width}px`);
    }
  }
  const page = await open("/acme/ml-api/issues/8"); // no sidebar: the panel floats
  await page.setViewportSize({ width: 400, height: 900 });
  await panel(page).getByLabel(/^Match score/).waitFor({ timeout: 20000 });
  const box = await panel(page).locator(".panel").boundingBox();
  await page.screenshot({ path: resolve(shots, `4d-narrow-panel-${SCHEME}.png`) });
  await page.close();
  assert.ok(box && box.x >= 0 && box.x + box.width <= 400, `floating panel leaves the 400px viewport: ${JSON.stringify(box)}`);
});

await step("4e. keyboard only: every control on each settings tab can be reached with Tab and shows a focus ring", async () => {
  const CONTROLS = "button, input, select, textarea, a[href], summary";
  for (const tab of ["Find issues", "Account", "Profile", "Saved issues"]) {
    const page = await optionsPage(tab);
    await page.getByRole("tabpanel").waitFor();
    if (tab === "Find issues") await page.locator(".issues, .empty").first().waitFor({ timeout: 20000 });
    await page.waitForFunction(() => !document.querySelector(".loading"), null, { timeout: 20000 }); // the Profile form exists only after its data arrived
    // Collapsed <details> content is not focusable (that is correct); open them so the controls inside are covered too.
    await page.evaluate(() => document.querySelectorAll("details").forEach((d) => { d.open = true; }));
    await page.waitForTimeout(400);
    const total = await page.evaluate((sel) => [...document.querySelectorAll(sel)].filter((e) => !e.disabled && e.getBoundingClientRect().width > 0).length, CONTROLS);
    const seen = new Set();
    let ringless = 0;
    for (let i = 0; i < total + 3; i++) {
      await page.keyboard.press("Tab");
      const info = await page.evaluate((sel) => {
        const el = document.activeElement;
        if (!el || el === document.body) return null;
        const cs = getComputedStyle(el);
        const all = [...document.querySelectorAll(sel)];
        return { index: all.indexOf(el), ring: cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0 };
      }, CONTROLS);
      if (info && info.index >= 0) { seen.add(info.index); if (!info.ring) ringless++; }
    }
    if (seen.size < total) {
      const missed = await page.evaluate(([sel, seenList]) => [...document.querySelectorAll(sel)].map((e, i) => ({ e, i })).filter(({ e, i }) => !e.disabled && e.getBoundingClientRect().width > 0 && !seenList.includes(i)).map(({ e }) => `${e.tagName.toLowerCase()} "${e.getAttribute("aria-label") || e.textContent.trim().slice(0, 20)}" tabindex=${e.tabIndex}`), [CONTROLS, [...seen]]);
      console.log(`      not reached: ${missed.join(" | ")}`);
    }
    await page.close();
    assert.ok(seen.size >= total, `tab "${tab}": reached ${seen.size} of ${total} controls`);
    assert.equal(ringless, 0, `tab "${tab}": ${ringless} focused controls had no visible outline`);
  }
});

await step("4f. every supported GitHub layout gets the panel in the right place (and a late-rendered sidebar is picked up)", async () => {
  const parent = (page) => panel(page).evaluate((h) => { const p = h.parentElement; return { tag: p.tagName.toLowerCase(), id: p.id, testid: p.getAttribute("data-testid"), cls: p.className, floating: !!h.shadowRoot.querySelector(".panel.floating") }; });
  for (const [layout, check] of [
    ["legacy", (p) => p.id === "partial-discussion-sidebar" && !p.floating],
    ["pane", (p) => p.testid === "issue-viewer-metadata-pane" && !p.floating],
    ["layout", (p) => String(p.cls).includes("Layout-sidebar") && !p.floating],
    ["none", (p) => p.tag === "body" && p.floating],
  ]) {
    const page = await open(`/acme/ml-api/issues/8?layout=${layout}`);
    await panel(page).waitFor({ state: "attached", timeout: 15000 });
    const where = await parent(page);
    assert.ok(check(where), `layout "${layout}": panel placed wrongly ${JSON.stringify(where)}`);
    assert.equal(await page.locator("#gh-content").innerText(), "Original GitHub content.", "GitHub's own content is untouched");
    assert.equal(await page.locator("#compass-ai-host").count(), 1);
    await page.close();
  }
  const late = await open("/acme/ml-api/issues/8?layout=late");
  await panel(late).waitFor({ state: "attached", timeout: 15000 });
  assert.ok((await parent(late)).floating, "before GitHub renders the sidebar the panel floats");
  await late.waitForFunction(() => document.getElementById("compass-ai-host")?.parentElement?.getAttribute("data-testid") === "issue-viewer-metadata-pane", null, { timeout: 6000 });
  assert.equal(await panel(late).locator(".panel.floating").count(), 0, "...and moves into the sidebar once it exists");
  assert.equal(await late.locator("#compass-ai-host").count(), 1, "still exactly one panel");
  await late.close();
});

await step("5. difficulty: maintainer-label hint and an unavailable, experimental ML estimate", async () => {
  await panel(page7).getByText(/Level 2 of 5 \(easy\)/).waitFor();
  await panel(page7).getByText("Experimental", { exact: true }).first().waitFor();
  await panel(page7).getByText(/No ML estimate: no ML model is configured on this backend/).waitFor();
});

await step("6. the score breakdown opens from the keyboard", async () => {
  const toggle = panel(page7).getByRole("button", { name: /Score breakdown/ });
  if ((await toggle.getAttribute("aria-expanded")) === "true") await toggle.click(); // step 4 opened it; start closed
  await toggle.focus();
  await page7.keyboard.press("Enter");
  assert.equal(await toggle.getAttribute("aria-expanded"), "true");
  await panel(page7).getByText("Evidence by component").waitFor();
  await panel(page7).getByText(/self-reported proficiency/).first().waitFor();
});

await step("7. repository health loads on demand from the stored analysis", async () => {
  await panel(page7).getByRole("button", { name: /Repository health/ }).click();
  await panel(page7).getByLabel(/^Score \d+ out of 100$/).first().waitFor({ timeout: 20000 });
  await panel(page7).getByText("healthy").first().waitFor();
  await panel(page7).getByText(/Data collected/).waitFor();
});

await step("8. readiness loads; a failing linked-PR lookup is reported, not hidden", async () => {
  await panel(page7).getByRole("button", { name: /Contribution readiness/ }).click();
  await panel(page7).getByText(/were not executed/).first().waitFor({ timeout: 30000 });
  const readinessText = await panel(page7).getByRole("region", { name: "Contribution readiness" }).innerText();
  const outcome = /Some checks failed: linked pull requests/.test(readinessText) ? "failure reported"
    : /No pull requests reference this issue/.test(readinessText) ? "no linked PRs found"
    : /Linked pull requests have not been checked/.test(readinessText) ? "explicitly marked as not checked" : null;
  assert.ok(outcome, "linked-PR status must be stated explicitly, not omitted:\n" + readinessText.slice(0, 400));
  console.log(`      linked-PR lookup outcome: ${outcome}`);
  await page7.screenshot({ path: resolve(shots, "5-full-panel.png") });
  await panel(page7).locator(".panel").screenshot({ path: resolve(shots, "6-sidebar-width-296px.png") });
});

await step("9. Save issue writes a saved issue for this user; Unsave deletes it", async () => {
  await panel(page7).getByRole("button", { name: "Save issue" }).click();
  await panel(page7).getByText("Saved to your issues.").waitFor();
  assert.deepEqual(query("SELECT i.issue_number AS n FROM saved_issues s JOIN issues i ON i.id = s.issue_id").map((r) => r.n), [7]);
  const events = query("SELECT feedback_type, source, recommendation_id FROM feedback WHERE feedback_type = 'saved'");
  assert.equal(events.length, 1);
  assert.equal(events[0].source, "system");
  assert.ok(events[0].recommendation_id, "the save is linked to a stored match");
  await panel(page7).getByRole("button", { name: "Unsave" }).click();
  await panel(page7).getByRole("button", { name: "Save issue" }).waitFor();
  assert.equal(query("SELECT count(*) AS c FROM saved_issues")[0].c, 0);
  await panel(page7).getByRole("button", { name: "Save issue" }).click();
  await panel(page7).getByText("Saved to your issues.").waitFor();
});

await step("10. the saved state is shown when a page opens (another tab, fresh load)", async () => {
  const page = await open("/acme/ml-api/issues/7");
  await panel(page).getByText("Already in your saved issues.").waitFor({ timeout: 15000 });
  assert.equal(await panel(page).getByRole("button", { name: "Save issue" }).count(), 0);
  await page.screenshot({ path: resolve(shots, "7-saved-indicator.png") });
  await page.close();
  const other = await open("/acme/ml-api/issues/8");
  await panel(other).getByRole("button", { name: "Save issue" }).waitFor({ timeout: 15000 });
  assert.equal(await panel(other).getByText("Already in your saved issues.").count(), 0, "an unsaved issue is not shown as saved");
  await other.close();
});

await step("11. feedback is stored and linked to the stored match; repeating it is a duplicate", async () => {
  await panel(page7).getByRole("button", { name: /Was this useful\?/ }).click();
  await panel(page7).getByRole("button", { name: "Relevant", exact: true }).click();
  await panel(page7).getByText("Thanks, your feedback was recorded.").first().waitFor();
  await panel(page7).getByRole("button", { name: "Too easy" }).click();
  await page7.waitForTimeout(500);
  const rows = query("SELECT feedback_type, feedback_value, category, source, recommendation_id FROM feedback WHERE feedback_type IN ('relevance','difficulty') ORDER BY id");
  assert.deepEqual(rows.map((r) => [r.feedback_type, r.feedback_value, r.category, r.source]), [["relevance", 1, "explicit_feedback", "user"], ["difficulty", -1, "explicit_feedback", "user"]]);
  assert.ok(rows.every((r) => r.recommendation_id), "feedback references the stored match");
  const match = query("SELECT ranking_model_version FROM recommendations");
  assert.deepEqual(match.map((m) => m.ranking_model_version), ["heuristic_v1"]);
  assert.equal(match.length, 1, "viewing pages stored nothing; one match was stored when the user acted, and reused for the later actions");
  await panel(page7).getByRole("button", { name: "Relevant", exact: true }).click();
  await panel(page7).getByText("You had already given this answer.").waitFor();
  assert.equal(query("SELECT count(*) AS c FROM feedback WHERE feedback_type = 'relevance'")[0].c, 1);
});

await step("12. only actions that work are offered: no placeholder for unbuilt features; the explanation comes from real data", async () => {
  await panel(page7).getByText("Why it matches").waitFor();
  const hasLearn = await panel(page7).getByText("You may want to learn this first").count();
  console.log(`      "You may want to learn this first" shown for this issue: ${hasLearn ? "yes" : "no (the backend reported no missing skills; the section is omitted, not faked)"}`);
  const text = await page7.evaluate(() => document.getElementById("compass-ai-host")?.shadowRoot?.textContent ?? "");
  for (const name of ["Explain this issue", "Find similar issues", "View learning path", "Not available yet"]) {
    assert.ok(!text.includes(name), `"${name}" must not appear: nothing is offered that does not exist`);
  }
});

await step("13. in-page navigation (no reload) re-targets the panel, then removes it on non-issue pages", async () => {
  await page7.evaluate(() => history.pushState({}, "", "/acme/ml-api/issues/8"));
  await page7.waitForFunction(() => document.getElementById("compass-ai-host")?.dataset.issue === "acme/ml-api#8", null, { timeout: 5000 });
  await panel(page7).getByText("Add pagination to the issues endpoint").first().waitFor({ timeout: 15000 });
  assert.equal(await page7.locator("#compass-ai-host").count(), 1, "exactly one panel");
  await page7.evaluate(() => history.pushState({}, "", "/acme/ml-api/issues"));
  await page7.waitForFunction(() => !document.getElementById("compass-ai-host"), null, { timeout: 5000 });
  await page7.evaluate(() => history.pushState({}, "", "/acme/ml-api/pull/3"));
  await page7.waitForTimeout(1500);
  assert.equal(await page7.locator("#compass-ai-host").count(), 0, "no panel on pull requests");
  // Going back to a page seen a moment ago is served from the extension's memory: no new match is computed by the backend.
  await page7.evaluate(() => history.pushState({}, "", "/acme/ml-api/issues/8"));
  await panel(page7).getByText("Add pagination to the issues endpoint").first().waitFor({ timeout: 15000 });
});

await step("14. without a sidebar the panel floats; an unknown issue is a clear error", async () => {
  const page = await open("/acme/ml-api/issues/8");
  await panel(page).getByLabel(/^Match score/).waitFor({ timeout: 20000 });
  assert.equal(await panel(page).locator(".panel.floating").count(), 1);
  const missing = await open("/acme/ml-api/issues/999");
  await panel(missing).getByRole("alert").first().waitFor({ timeout: 20000 });
  assert.ok((await panel(missing).getByRole("alert").first().innerText()).length > 0);
  await page.close();
  await missing.close();
});

await step("15. the page's own scripts cannot see credentials or the extension's storage", async () => {
  const leaked = await page7.evaluate(() => typeof chrome !== "undefined" && !!chrome.storage);
  assert.equal(leaked, false, "chrome.storage is not exposed to the page");
  await tokenFreePage(page7);
});

await step("16. profile editing: view tags, change, save; a changed profile refreshes scores; completion reflects what is saved", async () => {
  const page = await optionsPage("Profile");
  await page.getByLabel("Add an interest").waitFor();
  const interests = page.getByRole("list", { name: "Selected: Areas you are interested in" });
  await interests.getByText("machine learning").waitFor();
  await page.getByLabel("Python level").waitFor();
  await page.getByLabel("FastAPI level").waitFor();
  await page.getByText(/\d of 4 basics saved/).waitFor();
  await page.getByRole("button", { name: "Remove machine learning" }).click();
  await page.getByLabel("Add an interest").fill("rust");
  await page.getByLabel("Add an interest").press("Enter");
  await page.getByLabel("Add an interest").fill("databases");
  await page.getByLabel("Add an interest").press("Enter");
  await page.getByLabel("Add a language").fill("Rust");
  await page.getByLabel("Add a language").press("Enter");
  await page.getByLabel("Rust level").selectOption({ label: "Learning" });
  await page.getByLabel("Add a language").fill("python"); // already chosen: must not appear twice
  await page.getByLabel("Add a language").press("Enter");
  assert.equal(await page.getByLabel("Python level").count(), 1, "a skill is listed once");
  await page.getByText("More preferences (optional)").click();
  await page.getByLabel("bug fixing").check();
  await page.getByRole("button", { name: "Save profile" }).click();
  await page.getByText(/Profile saved/).waitFor();
  await page.screenshot({ path: resolve(shots, "8-profile-editor.png"), fullPage: true });
  await page.close();
  assert.deepEqual(JSON.parse(query("SELECT interests FROM user_preferences")[0].interests), ["rust", "databases"]);
  assert.deepEqual(query("SELECT skill_name AS name FROM user_skills ORDER BY skill_name").map((r) => r.name), ["fastapi", "python", "rust"]);
  assert.equal(query("SELECT proficiency FROM user_skills WHERE skill_name = 'rust'")[0].proficiency, 0.3);
});

await step("17. the saved-issues list shows the saved issue, links to it safely, and removes it", async () => {
  const page = await optionsPage("Saved issues");
  const link = page.getByRole("link", { name: "Open on GitHub: Parser crashes on load" });
  await link.waitFor();
  assert.equal(await link.getAttribute("href"), "https://github.com/acme/ml-api/issues/7");
  assert.equal(await link.getAttribute("rel"), "noopener noreferrer");
  await page.screenshot({ path: resolve(shots, "9-saved-list.png") });
  await page.getByRole("button", { name: "Unsave Parser crashes on load" }).click();
  await page.getByText(/Nothing saved yet/).waitFor();
  assert.equal(query("SELECT count(*) AS c FROM saved_issues")[0].c, 0);
  await page.close();
  const fresh = await open("/acme/ml-api/issues/7");
  await panel(fresh).getByRole("button", { name: "Save issue" }).waitFor({ timeout: 15000 });
  await fresh.close();
});

await step("18. the short-lived access token is renewed in the background and the refresh token rotates", async () => {
  const before = await liveTokens();
  const hashBefore = query("SELECT refresh_hash FROM auth_sessions")[0].refresh_hash;
  const wait = before.accessExpiresAt - SKEW * 1000 - Date.now() + 1500;
  console.log(`      waiting ${Math.max(0, Math.ceil(wait / 1000))} s for the access token to enter its renewal window`);
  await sleep(Math.max(0, wait));
  const page = await optionsPage("Saved issues"); // an uncached request
  await page.getByText(/Nothing saved yet/).waitFor();
  await page.close();
  const after = await liveTokens();
  assert.notEqual(after.access, before.access, "a new access token was obtained");
  assert.notEqual(after.refresh, before.refresh, "the refresh token was rotated");
  assert.notEqual(query("SELECT refresh_hash FROM auth_sessions")[0].refresh_hash, hashBefore);
  assert.equal(query("SELECT count(*) AS c FROM auth_sessions")[0].c, 1, "still one session");
  // The OLD refresh token is dead server-side: replaying it is treated as theft and ends the session, so test that on its own session.
  globalThis.oldAccess = before.access;
  globalThis.oldAccessExp = before.accessExpiresAt;
});

await step("19. an expired access token is refused by the backend", async () => {
  const wait = globalThis.oldAccessExp - Date.now() + 1500;
  if (wait > 0) {
    console.log(`      waiting ${Math.ceil(wait / 1000)} s for the previous access token to expire`);
    await sleep(wait);
  }
  const r = await api("/api/v1/auth/me", { token: globalThis.oldAccess });
  assert.equal(r.status, 401);
  assert.ok(!JSON.stringify(await r.json()).includes(globalThis.oldAccess), "the error does not echo the token");
  const { access } = await liveTokens();
  assert.equal((await api("/api/v1/auth/me", { token: access })).status, 200, "the current token still works");
});

await step("20. refresh-token reuse is detected and ends that session (real HTTP, separate session)", async () => {
  const ex = await (await api("/api/v1/auth/exchange", { method: "POST", body: { code: pairingCode(), client_label: "reuse-test" } })).json();
  const rotated = await (await api("/api/v1/auth/refresh", { method: "POST", body: { refresh_token: ex.refresh_token } })).json();
  const replay = await api("/api/v1/auth/refresh", { method: "POST", body: { refresh_token: ex.refresh_token } });
  assert.equal(replay.status, 401);
  assert.equal((await api("/api/v1/auth/me", { token: rotated.access_token })).status, 401, "the whole session, including its newest access token, is revoked");
  assert.equal((await api("/api/v1/auth/refresh", { method: "POST", body: { refresh_token: rotated.refresh_token } })).status, 401);
  assert.equal(query("SELECT revoked_reason FROM auth_sessions WHERE client_label = 'reuse-test'")[0].revoked_reason, "refresh_reuse");
});

await step("21. when the backend is down the panel says so and recovers after restart", async () => {
  server.kill();
  await sleep(800);
  const page = await open("/acme/ml-api/issues/9"); // never requested before, so not in the extension's memory
  await panel(page).getByRole("alert").filter({ hasText: /Could not reach the backend/ }).first().waitFor({ timeout: 20000 });
  server = spawn(python, ["-m", "uvicorn", "app.main:app", "--port", String(PORT), "--log-level", "warning"], { cwd: backendDir, env, stdio: "pipe" });
  await waitReady();
  await panel(page).getByRole("button", { name: "Try again" }).first().click();
  await panel(page).getByLabel(/^Match score/).waitFor({ timeout: 20000 });
  await page.close();
});

await step("22. Sign out revokes the session on the server at once and wipes local credentials", async () => {
  const t = await liveTokens();
  const page = await optionsPage("Account");
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.getByText(/Signed out/).waitFor();
  await page.close();
  const { session, local } = await storage();
  assert.ok(!session["compass.auth.v2"] && !local["compass.refresh.v2"], "no credentials remain");
  assert.equal((await api("/api/v1/auth/me", { token: t.access })).status, 401, "the old access token stops working immediately, before its expiry");
  assert.equal((await api("/api/v1/auth/refresh", { method: "POST", body: { refresh_token: t.refresh } })).status, 401);
  assert.equal(query("SELECT revoked_reason FROM auth_sessions WHERE client_label = 'Chrome extension'")[0].revoked_reason, "logout");
  const p = await open("/acme/ml-api/issues/7");
  await panel(p).getByText("Sign in to see scores.").waitFor({ timeout: 10000 });
  assert.equal(await panel(p).getByLabel(/Match score/).count(), 0, "no data without a session, even for issues cached before sign-out");
  await p.close();
});

await step("23. 'Keep me signed in' persists the refresh token only; a browser restart does not sign the user out", async () => {
  await signIn({ keep: true });
  const { session, local } = await storage();
  const t = await liveTokens();
  assert.ok(local["compass.refresh.v2"]?.refreshToken, "the refresh token is persisted (unencrypted, as documented)");
  assert.ok(!JSON.stringify(local).includes(t.access), "the access token is never persisted");
  assert.ok(!session["compass.auth.v2"].refreshToken, "and is then not duplicated in session storage");
  const row = query("SELECT persistent, created_at, refresh_expires_at, expires_at FROM auth_sessions ORDER BY id DESC LIMIT 1")[0];
  const utc = (v) => new Date(String(v).replace(" ", "T") + "Z").getTime();
  assert.equal(row.persistent, 1, "the server was told this session is persistent");
  const idleHours = (utc(row.refresh_expires_at) - utc(row.created_at)) / 3_600_000;
  const absDays = (utc(row.expires_at) - utc(row.created_at)) / 86_400_000;
  assert.ok(Math.abs(idleHours - 72) < 0.1 && Math.abs(absDays - 14) < 0.01, `persistent limits are 72 h idle / 14 d absolute, got ${idleHours.toFixed(2)} h / ${absDays.toFixed(2)} d`);
  await inspector.evaluate(() => chrome.storage.session.clear()); // what closing the browser does to session storage
  const p = await open("/acme/ml-api/issues/8");
  await panel(p).getByLabel(/^Match score/).waitFor({ timeout: 20000 });
  assert.ok((await liveTokens()).access, "a new access token was obtained from the persisted refresh token");
  await p.close();
});

await step("23b. 'Sign out everywhere' ends every session of the account, including one on another device", async () => {
  const other = await (await api("/api/v1/auth/exchange", { method: "POST", body: { code: pairingCode(), client_label: "other-device" } })).json();
  assert.equal((await api("/api/v1/auth/me", { token: other.access_token })).status, 200, "the other device is signed in");
  const mine = await liveTokens();
  const page = await optionsPage("Account");
  await page.getByRole("button", { name: "Sign out everywhere" }).click();
  await page.getByRole("group", { name: "Confirm sign out everywhere" }).waitFor();
  await page.screenshot({ path: resolve(shots, "11-sign-out-everywhere-confirm.png") });
  await page.getByRole("button", { name: "Yes, sign out everywhere" }).click();
  await page.getByText(/Signed out everywhere/).waitFor();
  await page.close();
  assert.equal((await api("/api/v1/auth/me", { token: other.access_token })).status, 401, "the other device's access token died at once");
  assert.equal((await api("/api/v1/auth/refresh", { method: "POST", body: { refresh_token: other.refresh_token } })).status, 401);
  assert.equal((await api("/api/v1/auth/me", { token: mine.access })).status, 401, "this browser's access token died at once");
  assert.equal(query("SELECT count(*) AS c FROM auth_sessions WHERE revoked_at IS NULL")[0].c, 0, "no live session remains");
  assert.ok(query("SELECT revoked_reason FROM auth_sessions").some((r) => r.revoked_reason === "logout_all"));
  const { session, local } = await storage();
  assert.ok(!session["compass.auth.v2"] && !local["compass.refresh.v2"], "local credentials are gone");
  await signIn({ keep: true }); // the next step needs a live session again
});

await step("23c. slow backend: a long loading state then success; a timeout shows a clear error and recovers when the backend is fast again", async () => {
  await restartServer("/match=7,/difficulty=40"); // match answers after 7 s; difficulty never within the extension's 30 s limit
  const page = await open("/acme/ml-api/issues/8?layout=none");
  await panel(page).getByText("Scoring this issue…").waitFor({ timeout: 10000 });
  await page.screenshot({ path: resolve(shots, "13-slow-loading.png") });
  await panel(page).getByLabel(/^Match score/).waitFor({ timeout: 30000 });
  const alert = panel(page).getByRole("alert").filter({ hasText: /did not answer in time/ });
  await alert.waitFor({ timeout: 60000 });
  await page.screenshot({ path: resolve(shots, "14-timeout.png") });
  assert.ok(await panel(page).getByRole("button", { name: "Try again" }).first().isVisible(), "a retry is offered");
  await restartServer(""); // back to a fast backend
  await panel(page).getByRole("button", { name: "Try again" }).first().click();
  await panel(page).getByText("Maintainer label").waitFor({ timeout: 20000 });
  await page.close();
});

await step("23d. extension reload: 'Keep me signed in' survives, the access token is renewed, and the panel still works", async () => {
  const before = await liveTokens();
  assert.ok(before.refresh, "signed in with a persisted refresh token");
  if (BROWSER === "chrome") {
    // An extension loaded through the debugging protocol is not brought back by chrome.runtime.reload() (its pages then answer
    // ERR_BLOCKED_BY_CLIENT, and loading the folder again did not help), so the "Reload" button of chrome://extensions is NOT exercised in
    // Chrome. What is exercised instead is the part that matters for sign-in: the service worker is stopped and restarted by the browser
    // (all of its in-memory state is lost, as on any extension update/idle stop) while the browser profile and the storage stay.
    const cdp = await context.newCDPSession(inspector);
    const versions = new Map();
    cdp.on("ServiceWorker.workerVersionUpdated", (e) => e.versions.forEach((v) => versions.set(v.versionId, v)));
    await cdp.send("ServiceWorker.enable");
    await sleep(1000);
    const mine = [...versions.values()].filter((v) => v.scriptURL.startsWith(`chrome-extension://${extId}/`));
    assert.ok(mine.length > 0, "found the extension's service worker");
    for (const v of mine) await cdp.send("ServiceWorker.stopWorker", { versionId: v.versionId });
    console.log(`      note: service worker stopped via the debugging protocol (${mine.length} version); chrome.runtime.reload() is not exercised in Chrome`);
    await sleep(1500);
  } else {
    await inspector.evaluate(() => chrome.runtime.reload()).catch(() => undefined);
    await sleep(2500);
    inspector = await context.newPage(); // a reload closes the extension's own pages: open a fresh inspector
    await inspector.goto(`chrome-extension://${extId}/options.html`);
  }
  const page = await optionsPage("Account");
  await page.getByRole("button", { name: "Sign out", exact: true }).waitFor({ timeout: 15000 });
  await page.close();
  const p = await open("/acme/ml-api/issues/8?layout=none");
  await panel(p).getByLabel(/^Match score/).waitFor({ timeout: 30000 });
  await p.close();
  assert.ok((await liveTokens()).refresh, "still signed in after the restart");
});

await step("24. the server can revoke a session; the extension then shows 'session ended'", async () => {
  assert.match(cli("revoke-sessions", "--github-id", "1"), /revoked [1-9]/);
  const page = await optionsPage("Profile");
  await page.getByRole("alert").first().waitFor();
  const text = await page.getByRole("alert").first().innerText();
  assert.match(text, /session has ended|Sign in again/i);
  await page.close();
  const { session, local } = await storage();
  assert.ok(!session["compass.auth.v2"]?.accessToken && !local["compass.refresh.v2"], "dead credentials were removed");
  const p = await open("/acme/ml-api/issues/7");
  await panel(p).getByText("Your session ended.").waitFor({ timeout: 10000 });
  await p.screenshot({ path: resolve(shots, "10-session-ended.png") });
  await p.close();
});

await step("24b. repeated wrong pairing codes lock sign-in out for a while; the page says so; a valid code is refused too", async () => {
  await inspector.evaluate(() => chrome.storage.session.clear()); // make sure nothing is signed in
  const page = await optionsPage();
  let said = null;
  for (let i = 0; i < 8 && !said; i++) {
    await page.locator("#code").fill(`WRONG-CODE-${i}-` + "x".repeat(20));
    await page.getByRole("button", { name: "Sign in" }).click();
    const alert = page.getByRole("alert");
    await alert.waitFor({ timeout: 10000 });
    const text = await alert.innerText();
    if (/Too many sign-in attempts/.test(text)) said = text;
    else assert.match(text, /pairing code is invalid, expired or already used/);
    await page.waitForTimeout(150);
  }
  assert.ok(said, "the lockout message appeared within 8 wrong attempts (limit is 5 failures per 15 minutes)");
  assert.match(said, /Try again in about \d+ min/);
  await page.locator("#code").fill(pairingCode()); // a perfectly valid code during the lockout is refused as well
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("alert").filter({ hasText: "Too many sign-in attempts" }).waitFor({ timeout: 10000 });
  await page.screenshot({ path: resolve(shots, "12-sign-in-locked-out.png") });
  await page.close();
  assert.equal(query("SELECT count(*) AS c FROM auth_sessions WHERE revoked_at IS NULL")[0].c, 0, "no session was created during the lockout");
  assert.ok(query("SELECT count(*) AS c FROM pairing_codes WHERE used_at IS NULL")[0].c >= 1, "the valid code was not consumed");
});

await step("25. no uncaught script errors on the page during the whole run", async () => {
  assert.deepEqual(pageErrors, []);
});

await context.close();
server.kill();

const failed = results.filter((r) => r[1] === "FAIL");
console.log(`\n${results.length - failed.length}/${results.length} browser checks passed. Screenshots: ${shots}`);
if (failed.length) {
  console.log("\nFailures:\n" + failed.map((f) => ` - ${f[0]}\n   ${f[2]}`).join("\n"));
  process.exit(1);
}
