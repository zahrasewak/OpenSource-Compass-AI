// Extra real-github.com observations (Stage 14): dark mode, session expiry + re-authentication, GitHub rate limits.
// Nothing on github.com is intercepted. The backend uses GitHub's UNAUTHENTICATED API (60 requests/hour per IP); no personal token
// is passed. The `ratelimit` scenario DELIBERATELY spends that shared quota until GitHub refuses, to observe the error UI; the
// quota refills at GitHub's reset time. Nothing is rotated or worked around.
//
//   BROWSER=chrome|msedge|chromium  COLOR_SCHEME=light|dark  node e2e/real-github-extra.mjs dark,session,ratelimit
//
// Prints observations, not a pass/fail gate: results are evidence for this page, browser, network and time only.
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
const BROWSER = process.env.BROWSER ?? "chrome";
const SCHEME = process.env.COLOR_SCHEME ?? "light";
const scenarios = (process.argv[2] ?? "dark,session").split(",");
const work = resolve(here, ".tmp-real-extra");
const shots = resolve(here, `screenshots-real/${BROWSER}-${SCHEME}`);
const PORT = 8000;
const BASE = `http://localhost:${PORT}`;
const python = resolve(backendDir, process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
const chromiumPath = process.env.CHROMIUM_PATH ?? resolve(process.env.LOCALAPPDATA ?? "", "ms-playwright/chromium-1234/chrome-win64/chrome.exe");
// Issue 1 of several unrelated public repositories (each needs its own repository fetch + health analysis).
const CANDIDATES = ["psf/requests#1", "pallets/flask#1", "pallets/jinja#1", "psf/black#1", "pytest-dev/pytest#1", "pypa/pip#1", "pallets/click#1", "python-poetry/poetry#1"];

if (!existsSync(resolve(dist, "manifest.json"))) throw new Error("build the extension first");
if (!process.env.KEEP_DB) rmSync(work, { recursive: true, force: true }); // KEEP_DB=1 reuses the previous run's database and profile
mkdirSync(work, { recursive: true });
mkdirSync(shots, { recursive: true });

const dbFile = resolve(work, "real.db");
const env = {
  ...process.env,
  COMPASS_DATABASE_URL: `sqlite:///${dbFile.replace(/\\/g, "/")}`,
  COMPASS_AUTH_SECRET: randomBytes(32).toString("hex"),
  COMPASS_AUTH_ACCESS_TTL_SECONDS: "45",
  COMPASS_GITHUB_MAX_RATE_LIMIT_WAIT_SECONDS: "1",
  COMPASS_GITHUB_MAX_RETRIES: "0",
  PYTHONIOENCODING: "utf-8",
};
delete env.COMPASS_GITHUB_TOKEN;
delete env.GITHUB_TOKEN;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const note = (label, value) => console.log(`${label.padEnd(64)} ${value}`);
const tryStep = async (label, fn) => {
  try { note(label, String(await fn())); } catch (e) { note(label, `NOT OBSERVED: ${String(e.message).split("\n")[0]}`); }
};
const quota = async () => {
  try {
    const j = await (await fetch("https://api.github.com/rate_limit", { headers: { "User-Agent": "compass-real-check" } })).json();
    return j.resources.core.remaining;
  } catch { return null; }
};
const cli = (...a) => execFileSync(python, ["-m", "app.cli", ...a], { cwd: backendDir, env, stdio: ["ignore", "pipe", "pipe"] }).toString();
const pairingCode = () => cli("issue-pairing-code", "--github-id", "424242", "--username", "real-check-user").trim().split("\n")[0].trim();
const query = (sql) => { const d = new DatabaseSync(dbFile, { readOnly: true }); try { return d.prepare(sql).all(); } finally { d.close(); } };

execFileSync(python, ["-m", "alembic", "upgrade", "head"], { cwd: backendDir, env, stdio: "pipe" });
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--port", String(PORT), "--log-level", "warning"], { cwd: backendDir, env, stdio: "pipe" });
for (let i = 0; ; i++) {
  try { if ((await fetch(`${BASE}/ready`)).ok) break; } catch { /* starting */ }
  if (i > 60) throw new Error("backend did not start");
  await sleep(500);
}

const launch = { headless: !process.env.HEADED, viewport: { width: 1360, height: 1000 }, colorScheme: SCHEME };
if (BROWSER === "chromium") Object.assign(launch, { executablePath: chromiumPath, args: [`--disable-extensions-except=${dist}`, `--load-extension=${dist}`, "--no-first-run"] });
else if (BROWSER === "msedge") Object.assign(launch, { channel: "msedge", ignoreDefaultArgs: ["--disable-extensions"], args: [`--disable-extensions-except=${dist}`, `--load-extension=${dist}`, "--disable-features=DisableLoadExtensionCommandLineSwitch", "--no-first-run"] });
else Object.assign(launch, { channel: "chrome", ignoreDefaultArgs: ["--disable-extensions"], args: ["--enable-unsafe-extension-debugging", "--no-first-run"] });
const context = await chromium.launchPersistentContext(resolve(work, "profile"), launch);
let extId;
if (BROWSER === "chrome") {
  extId = (await (await context.browser().newBrowserCDPSession()).send("Extensions.loadUnpacked", { path: dist })).id;
} else {
  let [w] = context.serviceWorkers();
  w ??= await context.waitForEvent("serviceworker", { timeout: 15000 });
  extId = new URL(w.url()).host;
}
console.log(`browser: ${BROWSER} ${context.browser()?.version()}, colour scheme: ${SCHEME}, scenarios: ${scenarios.join(",")}`);
console.log(`GitHub unauthenticated core quota at start: ${await quota()}\n`);

const pageErrors = [];
const issueUrl = (ref) => { const [, o, r, n] = ref.match(/^([\w.-]+)\/([\w.-]+)#(\d+)$/); return `https://github.com/${o}/${r}/issues/${n}`; };
const openIssue = async (ref) => {
  const page = await context.newPage();
  page.on("pageerror", (e) => pageErrors.push(e.message));
  const resp = await page.goto(issueUrl(ref), { waitUntil: "domcontentloaded", timeout: 45000 });
  return { page, status: resp?.status() };
};
const host = (page) => page.locator("#compass-ai-host");
const signIn = async () => {
  const p = await context.newPage();
  await p.goto(`chrome-extension://${extId}/options.html`);
  await p.locator("#code").fill(pairingCode());
  await p.getByRole("button", { name: "Sign in" }).click();
  await p.getByText("Signed in.", { exact: true }).waitFor({ timeout: 15000 });
  await p.close();
};
const contrast = (page) => page.evaluate(() => {
  const parse = (c) => c.match(/[\d.]+/g).map(Number);
  const lum = (rgb) => { const [r, g, b] = rgb.slice(0, 3).map((v) => { const x = v / 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; }); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
  const panelEl = document.getElementById("compass-ai-host")?.shadowRoot?.querySelector(".panel");
  const pcs = panelEl && getComputedStyle(panelEl);
  const gh = getComputedStyle(document.body);
  const ratio = (a, b) => { const [x, y] = [lum(parse(a)), lum(parse(b))]; return ((Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05)).toFixed(1); };
  return {
    githubMode: document.documentElement.getAttribute("data-color-mode"), githubTheme: document.documentElement.getAttribute(document.documentElement.getAttribute("data-color-mode") === "dark" || window.matchMedia("(prefers-color-scheme: dark)").matches ? "data-dark-theme" : "data-light-theme"),
    githubBody: gh.backgroundColor, githubText: gh.color,
    panelBg: pcs?.backgroundColor, panelText: pcs?.color, panelContrast: pcs ? ratio(pcs.backgroundColor, pcs.color) : null,
    usesGithubVariables: !!(panelEl && getComputedStyle(panelEl).getPropertyValue("--bgColor-default").trim()),
  };
});

try {
  await signIn();
  const first = CANDIDATES[0];
  const { page, status } = await openIssue(first);
  note("real page loaded (HTTP status)", status);
  await tryStep("match section", async () => {
    await host(page).getByLabel(/^Match score \d+ out of 100$/).waitFor({ timeout: 60000 });
    return "score shown";
  });

  if (scenarios.includes("dark")) {
    await tryStep(`${SCHEME}: GitHub's own colour mode vs the panel's`, async () => JSON.stringify(await contrast(page)));
    await page.screenshot({ path: resolve(shots, "dark-or-light-issue-page.png") });
    await tryStep(`${SCHEME}: options page (account tab) screenshot`, async () => {
      const o = await context.newPage();
      await o.goto(`chrome-extension://${extId}/options.html`);
      await o.getByRole("heading", { name: "Account" }).waitFor();
      await o.screenshot({ path: resolve(shots, "options.png") });
      await o.close();
      return "saved";
    });
  }

  if (scenarios.includes("session")) {
    await tryStep("save while signed in (backend only)", async () => {
      const save = host(page).getByRole("button", { name: "Save issue" });
      if (await save.count()) await save.click();
      await host(page).getByText(/Saved to your issues|Already in your saved issues/).waitFor({ timeout: 20000 });
      return "saved";
    });
    const before = query("SELECT last_refreshed_at FROM auth_sessions")[0]?.last_refreshed_at ?? null;
    await tryStep("wait 50 s (access token lifetime is 45 s), then act again", async () => {
      await sleep(50000);
      await host(page).getByRole("button", { name: "Remove" }).click();
      await host(page).getByRole("button", { name: "Save issue" }).waitFor({ timeout: 20000 });
      const after = query("SELECT last_refreshed_at FROM auth_sessions")[0]?.last_refreshed_at ?? null;
      return `action succeeded after expiry; token renewed in the background (last_refreshed_at ${before ? "was set" : "was empty"} -> ${after ? "set" : "empty"})`;
    });
    await tryStep("administrator revokes the session; the next action in the panel", async () => {
      cli("revoke-sessions", "--github-id", "424242");
      await host(page).getByRole("button", { name: "Save issue" }).click();
      const alert = host(page).getByRole("alert").first();
      await alert.waitFor({ timeout: 20000 });
      return `error shown: ${(await alert.innerText()).replace(/\s+/g, " ").slice(0, 170)}`;
    });
    await page.screenshot({ path: resolve(shots, "session-ended-panel.png") });
    await tryStep("panel after reload says the session ended", async () => {
      await page.reload({ waitUntil: "domcontentloaded" });
      await host(page).getByText("Your session ended.").waitFor({ timeout: 20000 });
      return "yes";
    });
    await tryStep("re-authenticate with a new pairing code, reload: panel works again", async () => {
      await signIn();
      await page.reload({ waitUntil: "domcontentloaded" });
      await host(page).getByLabel(/^Match score \d+ out of 100$/).waitFor({ timeout: 30000 });
      return "score shown again";
    });
    await tryStep("saved state survives re-authentication (same user, new session)", async () => {
      await host(page).getByRole("button", { name: "Save issue" }).click();
      await host(page).getByText(/Saved to your issues/).waitFor({ timeout: 20000 });
      await page.reload({ waitUntil: "domcontentloaded" });
      await host(page).getByText("Already in your saved issues.").waitFor({ timeout: 20000 });
      return "saved before and after";
    });
  }

  if (scenarios.includes("ratelimit")) {
    console.log("\n-- rate limit: spending the unauthenticated GitHub quota on purpose --");
    let limitedPage = null;
    for (const ref of CANDIDATES) {
      const q0 = await quota();
      const { page: p, status: st } = await openIssue(ref);
      const outcome = await Promise.race([
        host(p).getByLabel(/^Match score \d+ out of 100$/).waitFor({ timeout: 60000 }).then(() => "score"),
        host(p).getByRole("alert").first().waitFor({ timeout: 60000 }).then(() => "alert"),
      ]).catch(() => "timeout");
      note(`${ref}: page HTTP ${st}, match section`, outcome === "alert" ? `error: ${(await host(p).getByRole("alert").first().innerText()).replace(/\s+/g, " ").slice(0, 150)}` : outcome);
      if (outcome === "score") {
        await host(p).getByRole("button", { name: /Repository health/ }).click();
        const r = await Promise.race([
          host(p).getByLabel(/^Score \d+ out of 100$/).first().waitFor({ timeout: 90000 }).then(() => "health report"),
          host(p).getByRole("alert").first().waitFor({ timeout: 90000 }).then(() => "alert"),
        ]).catch(() => "timeout");
        const text = r === "alert" ? (await host(p).getByRole("alert").first().innerText()).replace(/\s+/g, " ").slice(0, 170) : "";
        note(`${ref}: repository health`, r === "alert" ? `error: ${text}` : r);
        if (r === "alert" && /Rate limited/i.test(text)) limitedPage = p;
      } else if (outcome === "alert" && /Rate limited/i.test(await host(p).getByRole("alert").first().innerText())) limitedPage = p;
      note(`   quota used by ${ref}`, `${q0} -> ${await quota()}`);
      if (limitedPage) break;
      await p.close();
    }
    if (!limitedPage) note("rate limit", "NOT OBSERVED: the candidate repositories did not exhaust the quota");
    else {
      const p = limitedPage;
      await tryStep("rate-limited panel: 'Try again' is held with a countdown", async () => {
        const btn = host(p).getByRole("button", { name: /Try again in/ }).first();
        await btn.waitFor({ timeout: 10000 });
        return `${await btn.innerText()} (disabled=${await btn.isDisabled()})`;
      });
      await p.screenshot({ path: resolve(shots, "rate-limited.png") });
      const qa = await quota();
      await tryStep("expanding readiness while limited (partial results or a clear error)", async () => {
        await host(p).getByRole("button", { name: /Contribution readiness/ }).click();
        const sec = host(p).getByRole("region", { name: "Contribution readiness" });
        await Promise.race([sec.getByText(/were not executed|Rate limited/).first().waitFor({ timeout: 60000 })]);
        return (await sec.innerText()).replace(/\s+/g, " ").slice(0, 260);
      });
      await tryStep("a different issue while limited: error, not a crash", async () => {
        const { page: other } = await openIssue("pallets/werkzeug#2");
        const a = host(other).getByRole("alert").first();
        await Promise.race([a.waitFor({ timeout: 60000 }), host(other).getByLabel(/^Match score/).waitFor({ timeout: 60000 })]);
        const t = (await a.count()) ? (await a.innerText()).replace(/\s+/g, " ").slice(0, 150) : "a score was shown (answered from stored data)";
        await other.close();
        return t;
      });
      note("GitHub requests spent while limited (quota before -> after)", `${qa} -> ${await quota()}`);
      await p.screenshot({ path: resolve(shots, "rate-limited-after.png") });
    }
  }
  if (scenarios.includes("navigate")) {
    console.log("\n-- navigation inside the real GitHub UI (Turbo/SPA), list page -> issue -> back -> forward --");
    const nav = await context.newPage();
    nav.on("pageerror", (e) => pageErrors.push(e.message));
    const placement = () => host(nav).evaluate((h) => `${h.dataset.issue} ${h.shadowRoot.querySelector(".panel.floating") ? "FLOATING" : "in sidebar"}`);
    await tryStep("issues list page: no panel", async () => {
      await nav.goto("https://github.com/psf/requests/issues?q=is%3Aissue+is%3Aclosed", { waitUntil: "domcontentloaded", timeout: 45000 });
      await nav.waitForTimeout(2000);
      return (await nav.locator("#compass-ai-host").count()) === 0 ? "no panel on a list page" : "UNEXPECTED panel on a list page";
    });
    let first = null;
    await tryStep("click an issue in the list (in-page navigation): panel appears for that issue", async () => {
      await nav.waitForFunction(() => [...document.querySelectorAll("a")].some((x) => /^(https:\/\/github\.com)?\/psf\/requests\/issues\/\d+$/.test(x.getAttribute("href") ?? "")), null, { timeout: 30000 });
      const href = await nav.evaluate(() => [...document.querySelectorAll("a")].map((x) => x.getAttribute("href")).find((h) => /^(https:\/\/github\.com)?\/psf\/requests\/issues\/\d+$/.test(h ?? "")));
      const link = nav.locator(`a[href="${href}"]`).first();
      await link.click();
      await nav.waitForFunction(() => !!document.getElementById("compass-ai-host"), null, { timeout: 30000 });
      first = new URL(nav.url()).pathname;
      await host(nav).getByRole("heading", { name: "Match for you" }).waitFor({ timeout: 60000 });
      return `clicked ${href} -> ${first}; ${await placement()}`;
    });
    await tryStep("back to the list: panel removed", async () => {
      await nav.goBack();
      await nav.waitForFunction(() => !document.getElementById("compass-ai-host"), null, { timeout: 15000 });
      return "removed";
    });
    await tryStep("forward to the issue: panel returns for the same issue, ends in the sidebar", async () => {
      await nav.goForward();
      await nav.waitForFunction(() => !!document.getElementById("compass-ai-host"), null, { timeout: 30000 });
      await nav.waitForFunction(() => !document.getElementById("compass-ai-host")?.shadowRoot?.querySelector(".panel.floating"), null, { timeout: 8000 }).catch(() => undefined);
      return `${await placement()} (url ${new URL(nav.url()).pathname === first ? "same issue" : "DIFFERENT"})`;
    });
    await tryStep("exactly one panel after all that", async () => String(await nav.locator("#compass-ai-host").count()));
    await nav.screenshot({ path: resolve(shots, "navigate-final.png") });
    await nav.close();
  }

  if (scenarios.includes("partial")) {
    console.log("\n-- partial results: stored issue, GitHub quota exhausted --");
    const { page: p } = await openIssue("pallets/flask#1");
    await tryStep("match for a stored issue while GitHub refuses", async () => {
      const r = await Promise.race([
        host(p).getByLabel(/^Match score \d+ out of 100$/).waitFor({ timeout: 40000 }).then(() => "score shown from stored data"),
        host(p).getByRole("alert").first().waitFor({ timeout: 40000 }).then(async () => `error: ${(await host(p).getByRole("alert").first().innerText()).replace(/\s+/g, " ").slice(0, 120)}`),
      ]);
      return r;
    });
    await tryStep("repository health for a stored analysis", async () => {
      await host(p).getByRole("button", { name: /Repository health/ }).click();
      const sec = host(p).getByRole("region", { name: "Repository health" });
      await sec.getByText(/Data collected|Rate limited|error/i).first().waitFor({ timeout: 40000 });
      return (await sec.innerText()).replace(/\s+/g, " ").slice(0, 200);
    });
    await tryStep("contribution readiness while limited (partial results expected)", async () => {
      await host(p).getByRole("button", { name: /Contribution readiness/ }).click();
      const sec = host(p).getByRole("region", { name: "Contribution readiness" });
      await sec.getByText(/were not executed|Rate limited/).first().waitFor({ timeout: 60000 });
      return (await sec.innerText()).replace(/\s+/g, " ").slice(0, 420);
    });
    await p.screenshot({ path: resolve(shots, "partial-results.png") });
    note("GitHub quota during partial-results check", `${await quota()}`);
  }
  note("page script errors", pageErrors.length ? pageErrors.join(" | ").slice(0, 300) : "none");
} finally {
  await context.close();
  server.kill();
}
console.log(`\nGitHub unauthenticated core quota at end: ${await quota()}. Screenshots: ${shots}`);
