# Authentication and token handling (Stage 13)

Replaces the Stage 3 / Stage 12 scheme (one manually issued 12 h bearer JWT, pasted into the extension, not revocable).
Tokens now come from a **session**: short-lived access tokens, rotating refresh tokens, server-side revocation.
**Chrome extension storage does not make any of this secure.** What it does and does not protect is spelled out in §5.

## 1. Lifecycle

| Step | What happens | Where the secret is |
|---|---|---|
| **Creation: pairing code** | An administrator runs `python -m app.cli issue-pairing-code --github-id <id> --username <name>` on the backend machine. A 192-bit random one-time code is printed once; only its SHA-256 is stored. Valid 10 min (`COMPASS_AUTH_PAIRING_TTL_MINUTES`). | Admin's terminal; the user's clipboard briefly |
| **Creation: session** | The extension calls `POST /api/v1/auth/exchange {code, persistent}` (rate limited per client address, see section 7). A conditional `UPDATE ... WHERE used_at IS NULL` spends the code (single use, race-safe). An `auth_sessions` row is created; the response carries an **access token** (JWT, 15 min, claims `sub`, `sid`, `typ=access`) and a **refresh token** (256-bit random, only its SHA-256 stored). Response has `Cache-Control: no-store`. | Extension background worker |
| **Storage (extension)** | Access token: `chrome.storage.session` (memory only, never written to disk, cleared when the browser closes, restricted to trusted extension contexts). Refresh token: also `storage.session` by default; `storage.local` (persistent, **plain text in the profile**) only if the user ticks **Keep me signed in**. Settings (backend URL, scorer) are not secret. | see §5 |
| **Transmission** | Access token: `Authorization: Bearer` header only, to the one configured backend origin (https, or http for localhost), `credentials: omit`, `redirect: error`, `cache: no-store`. Refresh token: request body of `/auth/refresh` and `/auth/logout` only. Neither appears in a URL, a log line, an error message or any API response other than the token endpoints (tests enforce this). | TLS in transit (the backend does not terminate TLS itself) |
| **Validation (every request)** | Signature, expiry, issuer/audience, `typ=access`, then the `sid` must exist, belong to the same user, not be revoked and not past its absolute lifetime. Failures are a generic 401 (no oracle for *why*). | Server |
| **Renewal** | The worker renews 30 s before expiry (and once on a 401): `POST /auth/refresh {refresh_token}` rotates the refresh token and returns a new pair. Single-flight inside the worker plus a Web Lock across extension contexts, re-reading storage inside the lock. | Worker |
| **Reuse detection** | The previous refresh token's hash is kept. Presenting an already-rotated token means a copy exists somewhere else: the **whole session is revoked** (its access tokens die immediately). Conditional UPDATEs make concurrent double-spend impossible. | Server |
| **Limits** | Access 15 min. Default sessions: refresh 7 days idle (restarted on every rotation), 30 days absolute. **"Keep me signed in" sessions (Stage 15): 3 days idle, 14 days absolute.** All configurable; a refresh token never outlives its session. | Server |
| **Invalidation: logout** | Options page "Sign out" -> `POST /auth/logout {refresh_token}` (works even when the access token expired), then local credentials are wiped regardless of the server's answer. `POST /auth/logout-all` ends every session of the caller. | |
| **Invalidation: admin / theft** | `python -m app.cli revoke-sessions --github-id <id>`. The extension notices on its next request (401, refresh refused), wipes its credentials and shows "Your session ended". | |
| **Backend URL change** | Signs the extension out: a session is only valid for the server that issued it, and credentials are never sent to a different origin. | |

Legacy `issue-token` tokens (12 h, no revocation) still verify in development when `COMPASS_AUTH_ALLOW_LEGACY_TOKENS=true`,
and are **refused in production** (`COMPASS_ENVIRONMENT=production`). The CLI prints a deprecation warning.

## 2. Endpoints
`POST /auth/exchange`, `POST /auth/refresh`, `POST /auth/logout`, `POST /auth/logout-all`, `GET /auth/me` (see [API.md](API.md)).
Unknown/expired/used codes and unknown/expired/revoked/reused refresh tokens return the **same** 401 body.

## 3. Where each secret may and may not be

| | Backend DB | Extension worker | storage.session | storage.local | Options page DOM | Content script / GitHub page | Logs / errors / API responses |
|---|---|---|---|---|---|---|---|
| Pairing code | hash | request body once | no | no | typed, cleared right after use | **no** | no |
| Access token | no (signed JWT, stateless) | yes | yes | **never** | **no** (page talks to the worker) | **no** | no |
| Refresh token | hash only | yes | yes (default) | only with *Keep me signed in* | **no** | **no** | no |
| `COMPASS_AUTH_SECRET` | n/a (env) | **never** (extension has no key) | no | no | no | no | no |

Enforced by: `extension/scripts/verify-dist.mjs` (build fails if `content.js` contains `Authorization`, `Bearer`, `chrome.storage`,
the storage keys, `refresh_token`, `access_token` or an auth endpoint), the sender checks in `background/handler.ts` (account/profile/
saved-list messages are accepted only from the extension's own pages; panel messages only from a GitHub issue tab for that issue),
and tests: `backend/tests/test_sessions.py` (27), `extension/tests/session.test.ts`, `handler.test.ts`, `api.test.ts`, and the real-browser run
(`extension/e2e/run.mjs` steps 3, 15, 18-24).

## 4. Tests of failure modes (all run; see [VALIDATION_STATUS.md](VALIDATION_STATUS.md))
Missing, malformed, tampered, **expired**, wrong-audience, wrong-type, unknown-session, other-user's-session, **revoked**,
deleted-user, past-absolute-lifetime tokens; expired/unknown/revoked/reused refresh tokens; used/expired/unknown/malformed
pairing codes; logout (bearer and refresh variants), logout-all isolation; secrets absent from logs and error bodies; legacy
tokens refused in production. Browser level against the real backend with a 45 s access TTL: renewal and rotation, an expired
token refused by the server, reuse detected, sign-out revoking an access token **before** its expiry, server-side revocation
noticed by the extension, restart behaviour with and without *Keep me signed in*.

## 5. Risk assessment: unencrypted bearer tokens in the browser profile
**What the threat is.** `chrome.storage.local` is a plain LevelDB store in the Chrome profile directory. Any process running as
the same OS user (malware, an infostealer, a compromised backup/sync, another person with the unlocked machine) can read it.
`chrome.storage.session` is memory-only: it is never written to disk by the extension, but it can be read by anything with access
to the browser process memory or a debugging port, and by any code running inside **our own extension contexts** (including a
compromised dependency in our bundle). The content script is excluded from both areas, and web pages cannot reach either.
Chrome does not encrypt extension storage and offers extensions no OS-keychain API, so **there is no way for this extension to
store a credential "securely at rest"**. Wrapping the value in encryption with a key the extension also stores would be theatre,
and is not done.

**What was implemented to limit the damage (safest practical option):**
1. The long-lived credential never touches disk by default (`storage.session`); persistence is an explicit, warned opt-in.
2. The credential that is sent on every request (access token) lives 15 minutes and is **never** persisted.
3. A stolen refresh token cannot be used silently alongside the real one: rotation plus reuse detection revokes the session as soon
   as both parties use it, and the user sees "Your session ended".
4. Revocation is immediate and server-side (sign-out, `revoke-sessions`, absolute 30-day cap) instead of "wait for expiry".
5. No credential in the extension source, manifest, content script, page DOM, URLs, logs, error text or API responses.

**Residual risks (not removed):**
- *Keep me signed in* = a plaintext refresh token on disk for up to 3 days since last use (14 days absolute, tightened in Stage 15 from
  7/30). Offline theft of the profile gives the thief a session until the idle window lapses, the user signs out (everywhere), or an
  administrator revokes it. Off by default. Detail and options considered: section 8.
- With the default (memory only), malware that can read browser memory or attach a debugger still gets tokens.
- A compromised extension build, dependency or Chrome Web Store account compromises everything the worker can see.
- Between a theft and its detection (reuse detection only fires if the *legitimate* client refreshes again), the attacker has a
  valid session. Revocation requires either the user signing out, the admin revoking, or detection.
- The pairing code travels through an admin-to-user channel (chat/email/clipboard) in clear text for ≤10 minutes; it is single
  use, so a stolen but used code is worthless, a stolen unused one is a race the user would notice (their own exchange fails).
- Cached analysis responses (the user's own match/health data) are kept in the worker's memory for 5-10 minutes after a server-side
  revocation, until the worker restarts or the user signs in/out. No new data can be fetched.
- Two simultaneous refreshes with the **same** token (a client bug, or a worker killed after the server rotated but before it
  stored the answer) are indistinguishable from theft: the server revokes the session (`refresh_race` / `refresh_reuse`) and the
  user must pair again. The extension avoids it with single-flight + a Web Lock + re-reading storage inside the lock; the
  worker-killed-mid-refresh window is small but real.
- Transport security depends on the operator serving the backend over https; the extension refuses plain http except for localhost.
- No per-device binding (DPoP/mTLS) and no OAuth: pairing codes assume an administrator is available. GitHub OAuth (authorization
  code + PKCE via `chrome.identity.launchWebAuthFlow`) remains the recommended next step for multi-user deployments and would
  replace only the pairing step.
- Rate limiting of the unauthenticated auth endpoints exists (section 7). Its default memory backend is per process; use the database
  backend for several workers/instances. Neither replaces a gateway/proxy limit in production.

## 7. Rate limiting of /auth/exchange, /auth/refresh and /auth/logout (Stage 15, P0)
**Honest scope first.** A pairing code is 192 random bits, used once, valid 10 minutes; a refresh token is 256 random bits. Guessing either is
computationally infeasible, so rate limiting is **not** what makes them secure. It bounds the noise and database load one client can
generate against unauthenticated endpoints, makes guessing attempts *visible* (a lockout is logged), and slows anyone holding only a partial
or mistyped code. The security of pairing-code authentication rests on entropy, single use, short lifetime and the administrator-only
issuance channel. Weaknesses that rate limiting does not touch: the code travelling through chat/email/clipboard, shoulder surfing, and a
compromised administrator account.

| Budget (per client address unless noted) | Default | Setting |
|---|---|---|
| Failed codes/tokens before lockout (exchange and refresh, counted separately) | 5 per 15 minutes; lockout lasts until the oldest failure ages out | `COMPASS_AUTH_MAX_FAILURES`, `COMPASS_AUTH_FAILURE_WINDOW_SECONDS` |
| Requests of any outcome (exchange, refresh, logout, counted separately) | 20 per minute | `COMPASS_AUTH_ATTEMPTS_PER_MINUTE` |
| All clients together (database protection; deliberately generous so it cannot be turned into a denial of service) | 600 per minute per endpoint | `COMPASS_AUTH_GLOBAL_ATTEMPTS_PER_MINUTE` |
| Switch off (not recommended) | on | `COMPASS_AUTH_RATE_LIMIT_ENABLED` |

Behaviour: the budgets are checked **before** any code or token is looked up, so a locked-out client gets the same `429` (with `Retry-After`)
for a valid and an invalid code, and a valid code used during a lockout is *not consumed*. Successful requests do not reset the failure
count. Wrong, unknown, expired, used and too-short codes all return the identical `401 invalid_pairing_code` (the former `422` for short codes,
which revealed the minimum length, is gone). Refusals are logged once per minute per client/budget; neither codes nor tokens ever appear in a log
or response (tested).

**Two backends, chosen for the deployment (`COMPASS_AUTH_RATE_LIMIT_BACKEND`):**
| | `memory` (default) | `database` (recommended for more than one worker/instance) |
|---|---|---|
| State | sliding windows in the memory of ONE process | rows in `auth_throttle_events` (migration 0008), shared by every process using the database |
| Multiple workers/instances | each has its own counters: effective limits are up to N times higher; a restart forgets them | **one limit for all** (verified in tests with two limiter objects sharing a database) |
| Cost | none | 3-4 short queries per auth request; failures are written in their own transaction so they survive the failing request's rollback |
| Accuracy | exact within the process | approximate under concurrency (two simultaneous requests can both pass just below the limit) and relies on roughly synchronised clocks |
| Stores | counts only, in RAM | scope, client address, kind (`attempt`/`failure`), timestamp; never a code or token; rows older than the longest window are deleted at most once a minute per process |

With `COMPASS_ENVIRONMENT=production` and the memory backend the application logs a warning at startup. Limitations common to both:
- The key is the client address. Behind a proxy set `COMPASS_TRUSTED_PROXY_HOPS` to the number of trusted proxies so the real client is
  taken from the correct end of `X-Forwarded-For`; with the default 0 the header is ignored (a client can forge it) and every user behind
  one proxy shares one budget. Many users behind one NAT share a budget too. One attacker locking out a shared address is possible.
- An attacker with many addresses is limited only by the global ceiling.
- Memory backend: bounded (10,000 tracked clients per window; oldest evicted).
- A gateway/reverse-proxy limit in front of the API is still recommended for any public deployment (it also protects every other endpoint).

Tests: `backend/tests/test_auth_ratelimit.py` (51 cases: every behaviour test runs against BOTH backends, plus multi-worker and database-specific tests): repeated failures, lockout for valid codes, gradual expiry, per-client keys, forged and
spoofed `X-Forwarded-For`, legitimate use unaffected, per-minute and global caps, refresh/logout scopes, expiry and reuse of codes, no credential in
logs or bodies, log flooding, limiter internals. Extension: a 429 is shown as "too many sign-in attempts" with the wait, never as a rejected
code, and a rate-limited *renewal* keeps the session. Real browser: the lockout message and a refused valid code (e2e step 24b).

## 8. Audit of "Keep me signed in" and the persistent refresh token (Stage 15, P0)
**Exactly what is stored, where, for how long, in what form** (verified in code, in tests, and in Chrome 156 / Edge 156 for the access levels):

| Credential | Default (box unticked) | "Keep me signed in" | Form | Lifetime |
|---|---|---|---|---|
| Access token (JWT) | `storage.session` (memory, trusted contexts only) | same, **never persisted** | plaintext in browser memory | 15 min |
| Refresh token | `storage.session` (memory only) | **`storage.local` (on disk, in the profile)**; removed from `storage.session` | **plaintext**; Chrome does not encrypt extension storage | default 7 d idle / 30 d absolute; persistent **3 d idle / 14 d absolute** |
| Pairing code | typed into the page, cleared right after the request | same | not stored | single use, 10 min |
| Session expiry timestamp | with the tokens | `storage.local` next to the refresh token | not secret | n/a |

`chrome.storage.local` is **not** encrypted and is **not** a credential vault; nothing in this extension claims otherwise.

**Defect found and fixed in this audit.** Chrome lets *content scripts* read `storage.local` by default (only `storage.session` is restricted).
The worker restricted only the session area, so a bug or compromise in our content-script code (which runs next to GitHub's page) could have
read the persisted refresh token. Reproduced with `extension/e2e/probe-storage-access.mjs` through DevTools in the content script's isolated
world: **before** the fix the content script read `storage.local` in Chrome 156.0.8078.48 and Edge 156.0.4314.42; **after** setting
`setAccessLevel(TRUSTED_CONTEXTS)` on `storage.local` at every worker start it is denied in both ("Access to storage is not allowed from this
context"), and the denial held after the service worker was stopped. Not verified: behaviour across a full browser restart before the worker
first starts. This protects against our own content-script code; it does **not** protect against anything that can read the profile on disk.

**Safer designs considered** (the goal being to avoid a long-lived bearer credential reachable by extension JavaScript):
1. *Remove persistence entirely* (re-pair after every browser restart): safest, poor usability for a tool used daily; left as the default
   (the box is unticked) and the recommended setting on shared machines.
2. *Device-bound proof of possession* (WebCrypto non-extractable ECDSA key, DPoP-style signature on each refresh): stops a refresh token copied
   without the key from being usable and stops exfiltration by JavaScript; **but Chrome stores the key blob in the profile too**, so offline
   theft of the profile still yields a usable pair, and it needs a new server verification protocol, key registration and recovery flows.
   Substantial architectural change: **documented, not implemented**; recommended to evaluate together with the OAuth design.
3. *OS keystore via native messaging host*: the only way to get hardware/OS-backed secrecy; needs an installed native component and a signed
   installer, which a store-distributed extension does not have. Not pursued.
4. *Existing architecture, tightened* (implemented): short-lived access token never persisted; refresh rotation with reuse detection and
   server-side revocation (already present); **tighter server-side limits for persistent sessions** (3 d idle, 14 d absolute, set from the
   client's `persistent` flag, stored in `auth_sessions.persistent`, never changeable afterwards); **"Sign out everywhere"** (`POST /auth/logout-all`)
   in the settings page for a lost or copied profile; local credentials are always wiped even if the server call fails; content-script access
   to `storage.local` denied.

**Compatibility and migration.** Migration `0007` adds `auth_sessions.persistent` (NOT NULL, default false), so **existing sessions keep their
current limits**. Old extension builds do not send `persistent`, so their sessions are unaffected; the new build sends it from the checkbox.
The tighter limits apply only to sessions created after upgrade with the box ticked, which will have to be re-paired after 14 days (or after
3 idle days). `COMPASS_AUTH_PERSISTENT_REFRESH_TTL_HOURS` / `COMPASS_AUTH_PERSISTENT_SESSION_MAX_DAYS` change the bounds; they can never exceed
the general limits. Rolling back the migration drops the column (sessions then use the general limits).

**Tests for expiry, rotation, revocation, invalid refresh attempts and reuse** (all run): `backend/tests/test_sessions.py` (34, including the new
persistent-limit tests: tighter idle/absolute bounds, bounds survive rotation, absolute cap clips the last refresh, the flag cannot be changed
by a refresh request, operator's stricter general limits win), `extension/tests/session.test.ts`, and the real-browser steps for renewal,
rotation, expiry, reuse, sign-out, sign-out everywhere (including a session on another "device"), restart behaviour and server-side revocation.

**Residual risks (unchanged unless stated).** A plaintext refresh token on disk with the box ticked (shorter window now); malware able to read
browser memory or attach a debugger even without the box; a compromised extension build or dependency; detection of theft only happens when the
legitimate client refreshes after the thief (reuse detection) or the user/administrator revokes; the extension has no list of active sessions
(only revoke-all); persistence across a full browser restart of the access-level restriction is not verified.
