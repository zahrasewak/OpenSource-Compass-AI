// Can the extension's CONTENT SCRIPT (isolated world on a github.com page) read chrome.storage.local / .session?
// Plants a harmless marker in each area from an extension page, then tries to read it from the content script's world via CDP.
//   BROWSER=chrome|msedge node e2e/probe-storage-access.mjs
// Expectation after Stage 15: both reads are DENIED (access level TRUSTED_CONTEXTS is applied by the background worker).
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const dist = resolve(dirname(fileURLToPath(import.meta.url)), "../dist");
const BROWSER = process.env.BROWSER ?? "chrome";
const launch = { headless: true, ignoreDefaultArgs: ["--disable-extensions"] };
if (BROWSER === "msedge") Object.assign(launch, { channel: "msedge", args: [`--disable-extensions-except=${dist}`, `--load-extension=${dist}`, "--disable-features=DisableLoadExtensionCommandLineSwitch", "--no-first-run"] });
else Object.assign(launch, { channel: "chrome", args: ["--enable-unsafe-extension-debugging", "--no-first-run"] });
const context = await chromium.launchPersistentContext(mkdtempSync(resolve(tmpdir(), "probe-")), launch);
let extId;
if (BROWSER === "chrome") extId = (await (await context.browser().newBrowserCDPSession()).send("Extensions.loadUnpacked", { path: dist })).id;
else { let [w] = context.serviceWorkers(); w ??= await context.waitForEvent("serviceworker", { timeout: 15000 }); extId = new URL(w.url()).host; }
await context.route("https://github.com/**", (r) => r.fulfill({ contentType: "text/html", body: "<html><body><main><h1>issue</h1></main></body></html>" }));

const opts = await context.newPage();
await opts.goto(`chrome-extension://${extId}/options.html`);
await opts.waitForTimeout(1500); // let the background worker start and apply its access levels
await opts.evaluate(async () => {
  await chrome.storage.local.set({ "probe.local": "LOCAL-MARKER" });
  await chrome.storage.session.set({ "probe.session": "SESSION-MARKER" });
});

const page = await context.newPage();
const cdp = await context.newCDPSession(page);
const contexts = [];
cdp.on("Runtime.executionContextCreated", (e) => contexts.push(e.context));
await cdp.send("Runtime.enable");
await page.goto("https://github.com/acme/ml-api/issues/7");
await page.waitForFunction(() => !!document.getElementById("compass-ai-host"), null, { timeout: 15000 });
const isolated = contexts.find((c) => String(c.origin).startsWith(`chrome-extension://${extId}`));
if (!isolated) { console.log("content-script world not found", contexts.map((c) => c.origin)); process.exit(2); }

if (process.env.STOP_WORKER) {
  // Does the restriction survive the MV3 service worker being stopped (it is not running when nothing wakes it)?
  const browserCdp = await context.browser().newBrowserCDPSession();
  const { targetInfos } = await browserCdp.send("Target.getTargets");
  const workers = targetInfos.filter((t) => t.type === "service_worker" && t.url.includes(extId));
  for (const w of workers) await browserCdp.send("Target.closeTarget", { targetId: w.targetId }).catch(() => undefined);
  await page.waitForTimeout(1500);
  const after = (await browserCdp.send("Target.getTargets")).targetInfos.filter((t) => t.type === "service_worker" && t.url.includes(extId));
  console.log(`service workers before stop: ${workers.length}, still running after: ${after.length}`);
}

async function read(area) {
  const r = await cdp.send("Runtime.evaluate", {
    contextId: isolated.id, awaitPromise: true, returnByValue: true,
    expression: `(async () => { try { if (!chrome.storage || !chrome.storage.${area}) return { error: "chrome.storage.${area} is not available" };
      return { data: await chrome.storage.${area}.get(null) }; } catch (e) { return { error: String(e.message || e) }; } })()`,
  });
  return r.result?.value ?? { error: "no result: " + JSON.stringify(r.exceptionDetails?.text) };
}
const [local, session] = [await read("local"), await read("session")];
const leaked = (v, marker) => !!v.data && JSON.stringify(v.data).includes(marker);
console.log(`${BROWSER} ${context.browser()?.version()}`);
console.log(`content script reads storage.local   : ${leaked(local, "LOCAL-MARKER") ? "READABLE (marker seen)" : "denied"}  ${local.error ? "[" + local.error + "]" : ""}`);
console.log(`content script reads storage.session : ${leaked(session, "SESSION-MARKER") ? "READABLE (marker seen)" : "denied"}  ${session.error ? "[" + session.error + "]" : ""}`);
await context.close();
process.exit(leaked(local, "LOCAL-MARKER") || leaked(session, "SESSION-MARKER") ? 1 : 0);
