// Builds the unpacked extension into extension/dist (load that folder via chrome://extensions -> Load unpacked).
import { build } from "vite";
import react from "@vitejs/plugin-react";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { makeIcons } from "./make-icons.mjs";
import { verifyDist } from "./verify-dist.mjs";

// Usage: node scripts/build.mjs [--production] [--out <dir>]
//   development (default): backend http://localhost:8000 by default; manifest grants localhost + optional https hosts.
//   --production: requires COMPASS_BACKEND_URL=https://your-backend-origin ; that origin is pinned and is the ONLY host permission.
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const production = args.includes("--production") || process.env.COMPASS_BUILD === "production";
const outArg = args.indexOf("--out");
const dist = outArg >= 0 ? resolve(args[outArg + 1]) : resolve(root, "dist");
let backendOrigin = null;
if (production) {
  let url;
  try {
    url = new URL(process.env.COMPASS_BACKEND_URL ?? "");
  } catch {
    throw new Error("production build: set COMPASS_BACKEND_URL to the https origin of your backend, e.g. https://compass.example.com");
  }
  if (url.protocol !== "https:" || url.username || url.password || (url.pathname !== "/" && url.pathname !== "") || url.search || url.hash) {
    throw new Error("production build: COMPASS_BACKEND_URL must be a bare https origin (no credentials, path, query or fragment)");
  }
  backendOrigin = url.origin;
}
rmSync(dist, { recursive: true, force: true });
mkdirSync(dist, { recursive: true });

const common = {
  configFile: false,
  root,
  logLevel: "warn",
  plugins: [react()],
  define: {
    "process.env.NODE_ENV": '"production"',
    __COMPASS_BACKEND_URL__: JSON.stringify(backendOrigin ?? "http://localhost:8000"),
    __COMPASS_PINNED__: JSON.stringify(production),
  },
};

// Content scripts and the service worker are classic scripts: one self-contained IIFE each.
for (const [entry, name, file] of [
  ["src/content/index.tsx", "CompassContent", "content.js"],
  ["src/background/index.ts", "CompassBackground", "background.js"],
]) {
  await build({
    ...common,
    build: {
      outDir: dist,
      emptyOutDir: false,
      sourcemap: false,
      minify: true,
      cssCodeSplit: false,
      lib: { entry: resolve(root, entry), formats: ["iife"], name, fileName: () => file },
    },
  });
}

// The settings page is a normal HTML entry.
await build({
  ...common,
  root: resolve(root, "src/options"),
  base: "./",
  build: { outDir: dist, emptyOutDir: false, sourcemap: false, rollupOptions: { input: resolve(root, "src/options/options.html") } },
});

const manifest = JSON.parse(readFileSync(resolve(root, "manifest.json"), "utf8"));
if (production) {
  manifest.host_permissions = [`${backendOrigin}/*`]; // the pinned backend only: no localhost, no optional wildcard
  delete manifest.optional_host_permissions;
}
writeFileSync(resolve(dist, "manifest.json"), JSON.stringify(manifest, null, 2) + "\n");
makeIcons(resolve(dist, "icons"));

for (const f of ["manifest.json", "content.js", "background.js", "options.html"]) {
  if (!existsSync(resolve(dist, f))) throw new Error(`build output is missing ${f}`);
}
verifyDist(dist, { production, backendOrigin });
console.log(`Built ${production ? "PRODUCTION (backend pinned to " + backendOrigin + ")" : "development"} extension into`, dist, "(packaging and security checks passed)");
