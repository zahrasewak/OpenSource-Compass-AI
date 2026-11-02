# GitHub API access strategy: is a server-side token necessary? (Stage 14)

**Status: analysis and recommendation. Nothing here changes the code.** Standing rules: public data only, no scraping, no rotation of
IPs/accounts/tokens to evade limits, no personal credentials unless the owner configures them in their own environment, and any
credential stays on the server (never in the extension or browser).

## 1. What the measurements say
All numbers are *unauthenticated* GitHub REST calls (60/hour per IP), measured on 2026-11-01 with the Stage 13 code
([CACHING_AND_RATE_LIMITS.md](CACHING_AND_RATE_LIMITS.md)) unless stated.

| Action | GitHub requests | Notes |
|---|---|---|
| First open of an issue (repository + issue fetched) | **2** | real github.com page, repository never seen before |
| Reopen / reload / navigate away and back | **0** | stored rows + extension cache |
| First **repository health** analysis, measured on its own | **14** | `psf/requests` (earlier run). A whole new repository (issue open **plus** health) cost 14 (`pallets/flask`) and 13 (`pallets/jinja`) in the rate-limit run; the stored analysis is reused for 24 h |
| Readiness (linked-PR lookup) | ~1-2 | partial results when limited |
| Same-day repeat analysis | 0 *billable* | conditional requests answered `304` (mocked tests; not measured against the real API) |

Observed exhaustion: starting from 40 remaining, **three first-time health analyses plus a few issue opens used the whole quota**, after
which GitHub refused and the panel showed "Rate limited… Try again in about 49 min" with the button held and **0 further GitHub requests**
(see [BROWSER_TEST_REPORT.md](BROWSER_TEST_REPORT.md)). One first-time health analysis (`psf/requests`) also did not finish within the 90 s the test waited, and the quota kept dropping
afterwards, which suggests the backend keeps working after the panel stops waiting (it does not cancel work when the client gives
up). This is inferred from quota readings, not traced, but it is a plausible cost-control gap.

## 2. Capacity arithmetic
Unauthenticated: 60 requests/h **per backend IP, shared by every user of that backend**. At 2 requests per new issue and ~13-14 per new
repository with health that is at most **~30 first-opens/hour, or ~4 new repositories with health per hour, for all users combined**.
Authenticated (any valid token; 5,000/h for a user or app token, more for some installations): ~2,500 first-opens/h or ~350 new
repositories with health per hour. Caching multiplies both (repeat views cost 0; a repository is analysed once per 24 h).

## 3. Is a server-side token necessary?
| Deployment | Need a token? | Reason |
|---|---|---|
| Local / single developer (today) | **No** | One user, caching, lazy health: the observed quota is enough for normal use; the UI degrades cleanly when it runs out and recovers in under an hour |
| Small controlled beta (a handful of users) | **Probably not, measure first** | Shared 60/h is thin: a few users each opening several new repos per hour will hit it. Raise TTLs, keep health on demand, and watch the 429 rate before adding a credential |
| **Public multi-user deployment** | **Yes** | The shared unauthenticated budget cannot serve even a small audience; users would see rate-limit errors constantly. Caching helps a lot (popular repositories are shared) but cannot remove the cost of the long tail of new repositories |

So: **prefer unauthenticated access with caching, deduplication and lazy loading for development and early beta; add one server-side
credential when sustained 429s show the budget is the bottleneck (suggested trigger: rate-limit errors on more than ~5% of
issue opens over a week, or any public launch).**

## 4. If a credential is justified
- **Which:** a **GitHub App** with *no permissions* (installation access token, 1-hour expiry, minted from a private key held
  server-side) or a **fine-grained personal access token limited to "Public repositories (read-only)" with no extra permissions**,
  owned by a dedicated machine/organization account, never a person's own account. Both read public data under a 5,000/h budget.
  Classic PATs (broad scopes, long-lived) are not acceptable.
- **Where it lives:** the deployment's secret manager, injected as an environment variable (`COMPASS_GITHUB_TOKEN`, already supported
  and held as a `SecretStr`) or, for a GitHub App, the private key plus a minting step. Never in the repository, `.env.example`, logs,
  error messages, API responses, the extension, or any browser-visible value. Before any launch, add a test that the credential
  cannot appear in logs, errors or responses (the Stage 13 session tests do this for session tokens, not for this credential).
- **Minimum permissions:** none beyond public read. No `repo`, `user`, `workflow` or write scopes. Rotate every 90 days or on suspicion;
  alert on 401 from GitHub (`github_auth_failed` already maps it to 502).
- **One credential, not a pool.** Rotating several tokens/accounts/IPs to get around limits violates GitHub's terms and is excluded.
  If one budget is not enough, the answer is better caching, a request budget per user and queueing, or a higher-limit GitHub App
  installation agreed with GitHub, not circumvention.
- Use conditional requests with the credential (`304`s do not count against the primary limit); persist ETags (Redis) so they survive
  restarts and work across workers (today they are per-process memory).
- Add a per-user request budget (e.g. N new-repository analyses per hour) so one user cannot spend everyone's quota.

## 5. Rejected: calling GitHub with each user's own OAuth token
It would give each user their own 5,000/h budget, but needs either storing every user's GitHub token server-side (a large stored-secret
liability) or sending it from the extension (a credential in a browser). The sign-in design in [OAUTH_DESIGN.md](OAUTH_DESIGN.md)
deliberately keeps no GitHub token. Reconsider only with an encrypted-at-rest design, explicit user consent and a security review.

## 6. Improvements recommended regardless of the token decision (not implemented in Stage 14)
1. Cancel or detach long server-side analyses when the client disconnects, and report progress instead of a silent 90 s wait.
2. Persist the ETag cache; share single-flight across workers (a lock table or Redis).
3. Per-user and per-repository request budgets; show remaining quota to the operator (a health endpoint field).
4. Prefetch nothing; keep health/readiness on demand; consider raising the 24 h snapshot TTL for inactive repositories.
5. Treat 304 reuse against the **real** API as unverified until measured (only mocked tests prove it today).
