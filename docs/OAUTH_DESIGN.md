# GitHub OAuth sign-in: design and security analysis (Stage 14)

**Status: DESIGN ONLY. Nothing in this document is implemented.** Per the Stage 14 rules, no new authentication flow is built before
this design and its security implications have been reviewed. Current behaviour (admin-issued pairing codes, session tokens) is
described in [AUTHENTICATION.md](AUTHENTICATION.md).

## 1. Recommendation in one paragraph
Use **"Sign in with GitHub" purely as proof of identity** for the eventual public-facing extension, implemented as
**authorization-code flow with PKCE, driven by the backend**, requesting **no scopes**, and then **discarding GitHub's token**.
The result of a successful sign-in is exactly what a pairing code is today: a one-time code bound to a verified user, which the
extension exchanges at the existing `/auth/exchange` for the existing short-lived access token + rotating refresh token. So the
session layer built in Stage 13 (rotation, reuse detection, revocation, extension storage rules) is reused unchanged; only the way a
user *proves who they are* changes. Keep the admin pairing-code flow for controlled development, **disabled by default in production**.

Why this and not the alternatives:
- It removes the administrator from the loop (the reason pairing codes cannot serve a public extension) without storing any GitHub
  credential: the thing worth stealing (a GitHub token) never exists at rest.
- A user's GitHub identity (numeric id) is cryptographically proven by GitHub instead of typed in by an administrator.
- Least privilege: with no scopes, GitHub only vouches for public profile information (id, login).

## 2. Required GitHub permissions
- **OAuth App (recommended for this use) or a GitHub App used only for user sign-in.** Request **no scopes**; do not request
  `user:email`, `read:user`, `repo` or anything else. The `/user` endpoint returns the public id and login without scopes.
- Registered callback URL: the backend's `https://<backend>/api/v1/auth/github/callback` (https only; no wildcard).
- If a GitHub App is chosen instead: no repository or account permissions, user-token expiry enabled, installation not required.
  (OAuth App is simpler; GitHub's general advice to prefer GitHub Apps matters when the app acts on repositories or holds tokens,
  which this design deliberately avoids. Revisit if the product ever acts on a user's behalf.)

## 3. Proposed flow (backend-driven, PKCE-bound)
```
Extension (background worker)                 Backend                                  GitHub
1  make code_verifier (256-bit random), code_challenge = S256(verifier)
2  launchWebAuthFlow( GET /auth/github/start?challenge=…&ext_redirect=https://<id>.chromiumapp.org/cb )
                                              3  validate ext_redirect is on the allow-list;
                                                 create oauth_states row: state (256-bit random, stored hashed),
                                                 challenge, ext_redirect, expires (10 min), used=false
                                                 302 -> github.com/login/oauth/authorize
                                                        ?client_id&state&redirect_uri=<backend callback>&scope=(empty)
                                                                                      4  user signs in / approves (if needed)
                                              5  GET /auth/github/callback?code&state <- 302 from GitHub
                                                 look up state; must exist, be unused, unexpired -> mark used (conditional UPDATE)
                                                 POST github.com/login/oauth/access_token (client_id, client_secret, code)
                                                 GET api.github.com/user -> {id, login}
                                                 upsert users by github_user_id; refresh username
                                                 DISCARD the GitHub token (optionally DELETE /applications/{client}/token)
                                                 create a one-time login code bound to (user, challenge), TTL 2 min
                                                 302 -> https://<id>.chromiumapp.org/cb?code=<login code>
3  launchWebAuthFlow resolves with the redirect URL
4  POST /auth/exchange { code, code_verifier }   verify S256(code_verifier) == stored challenge, code unused/unexpired,
                                                 then the unchanged Stage 13 session creation
```
Errors at any step redirect to the extension URL with `error=login_failed` and no detail (no oracle), and are logged server-side.

## 4. CSRF, state and code-interception defences
| Threat | Defence |
|---|---|
| Login CSRF (attacker makes the victim's browser finish the attacker's login) | Server-side `state`, single use, 10 min, compared exactly; **and** the final exchange needs the `code_verifier` known only to the extension instance that started the flow, so a code obtained by an attacker cannot be redeemed by the victim's extension, nor the victim's by the attacker |
| Authorization-code injection / interception at the redirect | PKCE (S256 only; `plain` rejected); login code is single use, 2 min, bound to the challenge; redirect only to the allow-listed `https://<extension-id>.chromiumapp.org/…` URL |
| Open redirect | `ext_redirect` and GitHub's `redirect_uri` are exact-match allow-lists, never reflected from input; callback never redirects anywhere else |
| State/code replay | conditional `UPDATE … WHERE used_at IS NULL` (same pattern, and PostgreSQL-tested, as pairing codes) |
| Leakage of the OAuth client secret | Server-side only (secret manager / environment), never in the extension, logs, errors or repository; rotated on a schedule and after any suspicion; `verify-dist` already fails the build if credential-like strings reach the content script |
| GitHub token theft | The token is held in memory for the length of one request and never written to disk or returned; with no scopes its value is minimal anyway |
| Username takeover (login names are reusable) | Identity key is the immutable numeric `github_user_id`, never the login or email |
| Brute force / abuse of start/callback/exchange | Per-IP and global rate limits (reverse proxy or application), small state TTL, generic errors; today `/auth/exchange` has none |
| Phishing look-alike backend | The extension talks only to the configured https origin; the redirect URI allow-list pins the extension id |

## 5. How authenticated users map to backend profiles
- `users.github_user_id` (unique, already present) is the identity key. Sign-in upserts the user and refreshes `username` (display
  only). Profiles, saved issues, feedback and matches already hang off `users.id`, so **nothing about profile storage changes**.
- First sign-in creates a user with **no profile**; the existing rule holds: a missing profile is *unknown*, never a perfect match.
- Pairing-created users and OAuth users share the table. An administrator-typed `--github-id` is unverified; once OAuth exists,
  a pairing code for an id should be refused unless an admin explicitly marks a user as "dev", and production should not
  enable pairing at all (§7).
- Deletion: deleting a user cascades to sessions, saved issues and matches (foreign keys exist); a "delete my account" endpoint and
  a privacy notice (we store GitHub id, login, profile, saved issues, feedback; no email) are needed before a public launch.

## 6. Token expiry, storage, renewal, revocation
Unchanged from Stage 13 ([AUTHENTICATION.md](AUTHENTICATION.md)): access token 15 min in `storage.session`; refresh token rotating,
memory-only unless "Keep me signed in" (plaintext in the profile; residual risk documented); renewal 30 s before expiry, single-flight;
reuse detection revokes the session; sign-out and admin revocation are immediate; absolute session lifetime 30 days. New for OAuth:
the OAuth `state` and login code are short-lived rows (10 min / 2 min), housekeeping-deleted like pairing codes. GitHub-side
revocation of the app (user removes it in their settings) does **not** end our session, because we hold no GitHub token; the user
signs out in the extension, and an "end all sessions" action already exists (`/auth/logout-all`).

## 7. Keeping the current mechanism for controlled development
Appropriate and low risk **if** it cannot be reached in production by accident. Recommended (not implemented): a setting
`COMPASS_AUTH_PAIRING_ENABLED`, default **true** when `COMPASS_ENVIRONMENT=development` and **false** in production (exchange of a
pairing code then returns the same generic 401), plus the CLI printing a warning when used with it disabled. Legacy `issue-token`
tokens are already refused in production.

## 8. Prerequisites and work plan (when approved)
1. A public https backend domain and an OAuth App registered with the callback URL; client secret in a secret store.
2. A **stable extension id** (publish to the Chrome Web Store or pin `key` in `manifest.json`) because the redirect allow-list
   contains `https://<id>.chromiumapp.org/…`; add the `identity` permission (a visible permission prompt; this is the price of
   `launchWebAuthFlow`). Edge uses a different redirect host and id; plan a second allow-list entry.
3. Backend: `oauth_states` + login-code table (migration 0007), `/auth/github/start`, `/auth/github/callback`, extended
   `/auth/exchange` (verifier), pairing flag, rate limits. 4. Extension: sign-in button, PKCE helper, `launchWebAuthFlow` in the worker.
5. Tests: state replay/expiry/mismatch, wrong verifier, tampered redirect, GitHub failure paths with mocked GitHub, no token in
   logs/responses, PostgreSQL race tests; then a manual run with a throwaway OAuth App.
6. Security review of the finished flow before any public release.

Alternatives considered: **device flow** (no redirect, public client, but the GitHub token would be polled by the extension or
proxied by the backend; worse UX, same identity result), **extension-driven OAuth with `client_secret` in the extension**
(rejected: a secret in a public extension is not a secret), **using the user's GitHub token for API calls** (see
[GITHUB_API_STRATEGY.md](GITHUB_API_STRATEGY.md); rejected for now because it creates a stored credential per user).

## 9. Open decisions for the owner
OAuth App vs GitHub App; invite-only beta (allow-list of GitHub ids) vs open signup; data retention and deletion policy; whether the
Edge build will be distributed (second extension id); who operates the OAuth app (a GitHub organization account rather than a
personal account, so the app does not depend on one person).
