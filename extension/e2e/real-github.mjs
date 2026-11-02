// Real-GitHub browser check: real Chromium + the built extension + the real FastAPI backend, on pages from the REAL github.com.
// Nothing on github.com is intercepted or modified. The backend uses GitHub's UNAUTHENTICATED public API (60 requests/hour per IP);
// no personal token is ever passed (COMPASS_GITHUB_TOKEN / GITHUB_TOKEN are removed from the environment) and nothing is rotated or
// worked around when the quota runs out: that is one of the things being observed.
//
//   npm run build && node e2e/real-github.mjs [owner/repo#number]
//
// It prints what it OBSERVED, including failures. It is an observation script, not a pass/fail gate: GitHub's markup, bot
// protection and the shared quota all vary, and a result here is only evidence for the page, time and network it ran on.
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
const work = resolve(here, ".tmp-real");
const shots = resolve(here, "screenshots-real");
const PORT = 8000;
const BASE = `http://localhost:${PORT}`;
const python = resolve(backendDir, process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
const chromiumPath = process.env.CHROMIUM_PATH ?? resolve(process.env.LOCALAPPDATA ?? "", "ms-playwright/chromium-1234/chrome-win64/chrome.exe");
const target = (process.argv[2] ?? "psf/requests#1").match(/^([\w.-]+)\/([\w.-]+)#(\d+)$/);
if (!target) throw new Error("usage: node e2e/real-github.mjs owner/repo#number");
const [, owner, repo, number] = target;
const issuePath = `/${owner}/${repo}/issues/${number}`;

if (!existsSync(resolve(dist, "manifest.json")) || !existsSync(chromiumPath)) throw new Error("build the extension and install Playwright's Chromium first");
rmSync(work, { recursive: true, force: true });
mkdirSync(work, { recursive: true });
mkdirSync(shots, { recursive: true });

const dbFile = resolve(work, "real.db");
const env = {
  ...process.env,
  COMPASS_DATABASE_URL: `sqlite:///${dbFile.replace(/\\/g, "/")}`,
  COMPASS_AUTH_SECRET: randomBytes(32).toString("hex"),
  COMPASS_GITHUB_MAX_RATE_LIMIT_WAIT_SECONDS: "1",
  COMPASS_GITHUB_MAX_RETRIES: "0",
  PYTHONIOENCODING: "utf-8",
};
delete env.COMPASS_GITHUB_TOKEN;
delete env.GITHUB_TOKEN;

const observations = [];
const note = (label, value) => {
  observations.push([label, value]);
  console.log(`${label.padEnd(58)} ${value}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const quota = async () => {
  try {
    const r = await fetch("https://api.github.com/rate_limit", { headers: { "User-Agent": "compass-real-check" } });
    const j = await r.json();
    return j.resources.core.remaining;
  } catch {
    return null;
  }
};
const tryStep = async (label, fn) => {
  try {
    note(label, String(await fn()));
  } catch (e) {
    note(label, `NOT OBSERVED: ${String(e.message).split("\n")[0]}`);
  }
};

const cli = (...args) => execFileSync(python, ["-m", "app.cli", ...args], { cwd: backendDir, env, stdio: ["ignore", "pipe", "pipe"] }).toString();
execFileSync(python, ["-m", "alembic", "upgrade", "head"], { cwd: backendDir, env, stdio: "pipe" });
const code = cli("issue-pairing-code", "--github-id", "424242", "--username", "real-check-user").trim().split("\n")[0].trim();
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--port", String(PORT), "--log-level", "warning"], { cwd: backendDir, env, stdio: "pipe" });
for (let i = 0; ; i++) {
  try { if ((await fetch(`${BASE}/ready`)).ok) break; } catch { /* starting */ }
  if (i > 60) throw new Error("backend did not start");
  await sleep(500);
}
const db = (sql) => {
  const d = new DatabaseSync(dbFile, { readOnly: true });
  try { return d.prepare(sql).all(); } finally { d.close(); }
};

console.log(`Real GitHub page: https://github.com${issuePath}\n`);
const q0 = await quota();
note("GitHub unauthenticated core quota before", String(q0));

const context = await chromium.launchPersistentContext(resolve(work, "profile"), {
  executablePath: chromiumPath,
  headless: true,
  viewport: { width: 1360, height: 1000 },
  args: [`--disable-extensions-except=${dist}`, `--load-extension=${dist}`, "--no-first-run", "--no-default-browser-check"],
});
let [worker] = context.serviceWorkers();
worker ??= await context.waitForEvent("serviceworker", { timeout: 15000 });
const extId = new URL(worker.url()).host;

const pageErrors = [];
const consoleErrors = [];
const page = await context.newPage();
page.on("pageerror", (e) => pageErrors.push(e.message));
page.on("console", (m) => { if (m.type() === "error" && /compass|extension/i.test(m.text())) consoleErrors.push(m.text().slice(0, 200)); });
const host = page.locator("#compass-ai-host");

try {
  // 1. Open the real page before signing in.
  await tryStep("real page loaded (HTTP status)", async () => (await page.goto(`https://github.com${issuePath}`, { waitUntil: "domcontentloaded", timeout: 45000 })).status());
  await tryStep("page title", () => page.title());
  await tryStep("panel injected while signed out", async () => { await host.getByText("Sign in to see scores.").waitFor({ timeout: 15000 }); return "yes (sign-in notice shown)"; });
  const placement = () => host.evaluate((h) => {
    const p = h.parentElement;
    const floating = !!h.shadowRoot?.querySelector(".panel.floating");
    return (floating ? "FLOATING " : "in sidebar ") + `(parent: <${p?.tagName.toLowerCase()}${p?.id ? "#" + p.id : ""}${p?.getAttribute("data-testid") ? ' data-testid="' + p.getAttribute("data-testid") + '"' : ""}>)`;
  });
  await tryStep("placement on first load", placement);
  await page.screenshot({ path: resolve(shots, "1-real-signed-out.png") });

  // 2. Sign in with a pairing code through the options page.
  const options = await context.newPage();
  await options.goto(`chrome-extension://${extId}/options.html`);
  await options.locator("#code").fill(code);
  await options.getByRole("button", { name: "Sign in" }).click();
  await tryStep("sign-in with pairing code", async () => { await options.getByText("Signed in.", { exact: true }).waitFor({ timeout: 15000 }); return "signed in"; });
  await options.close();

  // 3. First open after sign-in: count GitHub API requests this costs.
  const qa = await quota();
  await page.reload({ waitUntil: "domcontentloaded" });
  await tryStep("match section on the real page", async () => {
    const score = host.getByLabel(/^Match score \d+ out of 100$/);
    const alert = host.getByRole("alert").first();
    const which = await Promise.race([
      score.waitFor({ timeout: 60000 }).then(() => "score"),
      alert.waitFor({ timeout: 60000 }).then(() => "alert"),
    ]);
    return which === "score" ? await score.getAttribute("aria-label") : `error shown: ${(await alert.innerText()).replace(/\s+/g, " ").slice(0, 160)}`;
  });
  await tryStep("difficulty section (ML absent by design)", async () => {
    await host.getByRole("heading", { name: "Difficulty" }).waitFor({ timeout: 30000 });
    return (await host.getByText(/No ML estimate/).count()) ? "no ML estimate shown, as expected without a trained model" : "difficulty shown (see screenshot)";
  });
  await page.screenshot({ path: resolve(shots, "2-real-first-open.png") });
  const qb = await quota();
  note("GitHub API requests used by sign-in + first open", qa !== null && qb !== null ? String(qa - qb) : "unknown");

  // 4. Reload: nothing should be fetched again (backend stored the issue; the extension memory is cleared by reload only if the worker stopped).
  await page.reload({ waitUntil: "domcontentloaded" });
  await tryStep("match after a reload", async () => { await host.getByLabel(/^Match score \d+ out of 100$/).waitFor({ timeout: 30000 }); return "shown"; });
  const qc = await quota();
  note("GitHub API requests used by the reload", qb !== null && qc !== null ? String(qb - qc) : "unknown");

  // 5. Navigate inside GitHub (Turbo, no full reload) away from the issue and back.
  await tryStep("in-page navigation away and back", async () => {
    const link = page.locator(`a[href="/${owner}/${repo}/issues"]`).first();
    await link.click({ timeout: 15000 });
    await page.waitForFunction(() => !document.getElementById("compass-ai-host"), null, { timeout: 15000 });
    await page.goBack();
    await host.getByLabel(/^Match score \d+ out of 100$/).waitFor({ timeout: 30000 });
    return "panel removed on the list page and restored on return";
  });
  await tryStep("placement after back-navigation (polled for up to 12 s)", async () => {
    const t0 = Date.now();
    const seen = [];
    while (Date.now() - t0 < 12000) {
      const where = await placement();
      const sidebars = await page.evaluate(() => ({
        pane: !!document.querySelector('[data-testid="issue-viewer-metadata-pane"]'),
        legacy: !!document.querySelector("#partial-discussion-sidebar"),
        layout: !!document.querySelector(".Layout-sidebar"),
      }));
      const line = `${where} sidebars present: ${JSON.stringify(sidebars)}`;
      if (seen.at(-1)?.line !== line) seen.push({ at: Date.now() - t0, line });
      if (where.startsWith("in sidebar")) break;
      await sleep(500);
    }
    return seen.map((x) => `+${x.at}ms ${x.line}`).join(" | ");
  });
  const qd = await quota();
  note("GitHub API requests used by that navigation", qc !== null && qd !== null ? String(qc - qd) : "unknown");

  // 6. Repository health (the most GitHub-hungry section). Skipped with LIGHT=1 to spare the shared unauthenticated quota.
  if (!process.env.LIGHT) await tryStep("repository health on demand", async () => {
    await host.getByRole("button", { name: /Repository health/ }).click();
    const ok = host.getByLabel(/^Score \d+ out of 100$/).first();
    const err = host.getByRole("alert").first();
    const which = await Promise.race([ok.waitFor({ timeout: 90000 }).then(() => "score"), err.waitFor({ timeout: 90000 }).then(() => "alert")]);
    return which === "score" ? "health report shown" : `error/partial shown: ${(await err.innerText()).replace(/\s+/g, " ").slice(0, 200)}`;
  });
  if (!process.env.LIGHT) await page.screenshot({ path: resolve(shots, "3-real-health.png") });
  const qe = await quota();
  note("GitHub API requests used by repository health", qd !== null && qe !== null ? String(qd - qe) : "unknown");
  note("GitHub quota remaining at the end", String(qe));

  // 7. Save / saved state.
  await tryStep("save issue then reload shows it as saved", async () => {
    await host.getByRole("button", { name: "Save issue" }).click();
    await host.getByText(/Saved to your issues|Already in your saved issues/).waitFor({ timeout: 30000 });
    await page.reload({ waitUntil: "domcontentloaded" });
    await host.getByText("Already in your saved issues.").waitFor({ timeout: 30000 });
    return "saved, and shown as saved after reload";
  });
  await tryStep("saved row stored for the real-check user only", async () => String(db("SELECT count(*) AS c FROM saved_issues")[0].c));

  // 8. Session expiry/reauth behaviour is covered by the controlled-page run (access tokens there are 45 s); here only note the defaults.
  note("page script errors during the run", pageErrors.length ? pageErrors.join(" | ").slice(0, 300) : "none");
  note("extension console errors during the run", consoleErrors.length ? consoleErrors.join(" | ").slice(0, 300) : "none");
} finally {
  await context.close();
  server.kill();
}

console.log("\nDone. Screenshots:", shots);
