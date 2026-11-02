// Can this browser load the built extension under automation? Usage: node e2e/probe-browser.mjs chrome|msedge
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const channel = process.argv[2] ?? "chrome";
const dist = resolve(dirname(fileURLToPath(import.meta.url)), "../dist");
const flags = process.env.NO_FLAG ? [] : ["--disable-features=DisableLoadExtensionCommandLineSwitch"];
const context = await chromium.launchPersistentContext(mkdtempSync(resolve(tmpdir(), "probe-")), {
  channel, headless: false, ignoreDefaultArgs: ["--disable-extensions"],
  args: [`--disable-extensions-except=${dist}`, `--load-extension=${dist}`, "--no-first-run", ...flags],
});
let [worker] = context.serviceWorkers();
worker ??= await context.waitForEvent("serviceworker", { timeout: 10000 }).catch(() => null);
console.log(channel, context.browser()?.version(), worker ? `extension loaded: ${new URL(worker.url()).host}` : "extension NOT loaded");
await context.close();
