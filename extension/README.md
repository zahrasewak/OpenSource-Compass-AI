# OpenSource Compass AI - Chrome extension

Manifest V3 extension (React, TypeScript, Vite) that adds a panel to GitHub issue pages using your own OpenSource Compass backend.
Full documentation, security design, install guide and manual checklist: [docs/EXTENSION.md](../docs/EXTENSION.md).

```
npm install
npm run check     # type-check, unit/component tests, build (with packaging and security checks)
npm run build     # development build -> dist/   (Load unpacked in chrome://extensions, select the dist folder)
COMPASS_BACKEND_URL=https://your-backend.example npm run build:production   # production build: backend pinned, no localhost anywhere in dist/
npm run e2e       # optional: real Chromium + real local backend + controlled github.com page (needs backend deps and Playwright's Chromium)
node e2e/real-github.mjs psf/requests#1   # optional: OBSERVE the real github.com (unauthenticated GitHub API quota; see docs/EXTENSION.md)
BROWSER=chrome|msedge|chromium COLOR_SCHEME=light|dark npm run e2e   # same suite in installed Chrome/Edge, light or dark (docs/BROWSER_TEST_REPORT.md)
BROWSER=chrome COLOR_SCHEME=dark node e2e/real-github-extra.mjs dark,session,ratelimit   # real-site dark mode, session expiry/re-auth, and a DELIBERATE rate-limit run
```

Layout: `src/background` (service worker; the only place tokens are used; session manager and response cache), `src/content` (panel; never sees the token),
`src/options` (settings page: account/pairing sign-in, profile editor, saved list), `src/shared` (API client, schemas, URL parsing), `tests/`, `e2e/`, `manifest.json`.
