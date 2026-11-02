/**
 * Build-time configuration injected by scripts/build.mjs (vite `define`). Outside a build (unit tests) the development
 * defaults apply.
 *
 *  - development build (npm run build): default backend http://localhost:8000, user may enter another https origin.
 *  - production build (npm run build:production with COMPASS_BACKEND_URL=https://...): the backend origin is PINNED:
 *    it is the only host permission, the settings page shows it read-only, http/localhost is refused, and no
 *    development URL is present in the manifest.
 */
declare const __COMPASS_BACKEND_URL__: string | undefined;
declare const __COMPASS_PINNED__: boolean | undefined;

export const DEFAULT_BACKEND_URL: string = typeof __COMPASS_BACKEND_URL__ === "string" ? __COMPASS_BACKEND_URL__ : "http://localhost:8000";
export const BACKEND_PINNED: boolean = typeof __COMPASS_PINNED__ === "boolean" ? __COMPASS_PINNED__ : false;
