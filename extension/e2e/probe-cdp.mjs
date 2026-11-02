// Chrome >= 137 ignores --load-extension. Try the DevTools method Extensions.loadUnpacked instead.
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
const dist = resolve(dirname(fileURLToPath(import.meta.url)), "../dist");
const context = await chromium.launchPersistentContext(mkdtempSync(resolve(tmpdir(), "probe-")), {
  channel: process.argv[2] ?? "chrome", headless: !process.env.HEADED, ignoreDefaultArgs: ["--disable-extensions"], args: ["--enable-unsafe-extension-debugging", "--no-first-run"],
});
const session = await context.browser().newBrowserCDPSession();
const { id } = await session.send("Extensions.loadUnpacked", { path: dist });
console.log("loaded", id);
for (let i = 0; i < 3; i++) {
  const p = await context.newPage();
  try { await p.goto(`chrome-extension://${id}/options.html`); console.log(`open #${i}: ok, title=${await p.title()}`); }
  catch (e) { console.log(`open #${i}: ${String(e.message).split("\n")[0]}`); }
  await p.close();
  await new Promise((r) => setTimeout(r, 1500));
}
await context.close();
