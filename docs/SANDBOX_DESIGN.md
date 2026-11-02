# Project Lab sandbox: security design (PLANNING ONLY)

**Status: design document. Nothing described here is implemented, built, run or tested. No untrusted code has been executed and
the Docker sandbox is NOT complete.** Docker is not installed on the development machine; the existing `execution_jobs`,
`execution_results` and `execution_events` tables (migration 0001) are an unused schema stub. The extension's readiness analysis
*detects* README/test/CI files by reading GitHub metadata and never runs anything.

## 1. Threat model
A job runs **hostile, attacker-chosen code** (a public repository's build/test commands, chosen by whoever authored the repo or a
PR). Assume it will try to: escape the container; read the host, the backend's database or other jobs' data; steal credentials
(GitHub token, `COMPASS_AUTH_SECRET`, user tokens); use the machine for mining, spam or DoS; attack the internal network or cloud
metadata endpoints; fill disk/memory/PIDs; outlive its timeout; poison results shown to users; leak data through DNS/HTTP.

## 2. Why Docker alone is not a sufficient boundary
A container is a set of Linux namespaces + cgroups + seccomp filters around **processes that share the host kernel**. Any kernel
or runtime vulnerability (container-escape CVEs in runc/containerd/the kernel appear regularly), a mis-set flag, a writable mount
or a privileged capability turns "code in a container" into "code on the host". Docker's defaults are tuned for convenience, not
for hostile tenants. Therefore the design is **defence in depth with an additional isolation layer**, and Docker is only the
packaging/orchestration layer:

- **Required for any untrusted code in production:** run containers inside a **dedicated, disposable, network-isolated VM** (or use
  a user-space/VM-based runtime such as **gVisor `runsc`** or **Kata Containers / Firecracker microVMs**), on hosts that hold no
  secrets and no other service. A kernel exploit then compromises a throw-away VM, not the platform.
- Plain `docker run` on the application server is acceptable only for **trusted** code (the user's own, in development).
- Until the stronger layer exists, the Project Lab must stay disabled by default and refuse to run on a host without it.

## 3. Design

### 3.1 Architecture
```
API (FastAPI)  --enqueue-->  job queue (DB rows, status machine)  -->  scheduler  -->  sandbox host (separate machine/VM, no secrets)
                                                                        |                 runner agent (small, audited)
                                                                        |                   -> create container (rootless, gVisor/Kata)
                                                                        |                   -> stream bounded logs, enforce limits, destroy
                                                                        <---results (size-capped, sanitized)---
```
- The **API never runs user code and never mounts the Docker socket.** The runner agent is a separate process on a separate host
  and authenticates to the API with its own narrowly-scoped credential; it can only fetch jobs and post results.
- Jobs are rows in `execution_jobs` (status checked by a DB constraint already in place), results in `execution_results`, an
  append-only audit trail in `execution_events`. A job is claimed with a conditional UPDATE (same single-use pattern as pairing
  codes) so it runs once.

### 3.2 Per-job environment (all mandatory)
| Control | Setting |
|---|---|
| Lifetime | **Disposable**: new container (and, in production, new VM/microVM) per job; destroyed on completion, timeout or error; **never reused**, never persisted |
| Identity | **Non-root** user (fixed high UID), `no-new-privileges`, no `setuid` binaries, user namespaces/rootless engine |
| Privileges | **No privileged mode**, `--cap-drop=ALL` (add back nothing), default-deny seccomp profile, AppArmor/SELinux profile, no `--pid=host`/`--ipc=host`/`--net=host`/`--userns=host` |
| Filesystem | Read-only root filesystem; a single size-capped `tmpfs` for scratch (`noexec,nosuid`); **no host bind mounts**; **never mount the Docker socket**, `/proc` extras, `/sys` or devices; the repository is cloned *inside* the sandbox by the runner into an anonymous volume that is deleted with the container |
| Resources | CPU quota (e.g. 1 core), memory hard limit with swap disabled (e.g. 1 GiB), `pids-limit` (e.g. 256), `ulimit` for files/processes, disk quota on the scratch volume, **wall-clock timeout** (e.g. 10 min) enforced by the *runner* (not by the code inside) with SIGKILL and container removal, plus a CPU-time limit |
| Output | stdout/stderr captured with a **byte cap** (e.g. 1 MiB) and line cap, UTF-8 sanitized, stored as data and **rendered as text only** (never HTML), secrets-pattern scrubbing as a courtesy (not a guarantee) |
| Networking | Default **none** (`--network none`). If a job needs dependencies: a **pre-fetch phase** (trusted runner, allow-listed package registries through a caching proxy, then the network is removed before any repository code runs) or an egress proxy with an explicit allow-list. Block RFC1918, link-local and **cloud metadata (169.254.169.254)**, the host, and the API; DNS only via the proxy; no inbound |
| Secrets | **Nothing secret enters a sandbox**: no GitHub token, no user token, no environment inheritance (explicit minimal env), no cloud credentials/instance role on the sandbox host. Private repositories are out of scope |
| Images | Pinned by digest, built from minimal trusted bases, scanned; the repository's own Dockerfile/compose is **never** executed on the host; no `docker build` of untrusted contexts outside the sandbox |
| Cleanup | Always run in `finally`: kill, remove container, volumes, networks, temp dirs; a janitor removes anything older than the max lifetime; leaked resource alarms |
| Admission | Per-user and global concurrency/rate limits, queue depth cap, per-user daily CPU-seconds budget, repository size/file-count caps at clone time, allow-list of what commands may be requested (detected from the repo as *suggestions*, shown to the user before any run) |
| Audit | Every state change in `execution_events` with who/when/limits/image digest; results immutable once written; user-visible "this ran untrusted code in a sandbox" labelling |
| Result trust | Results are **evidence about one run in one environment**, not facts about the project; they must not feed ML labels or scores without explicit, separate design (see the data-leakage rules in [ML.md](ML.md)) |

### 3.3 Things deliberately *not* done
No executing code on the API host or in the developer's own Docker; no `docker.sock` exposure; no shared build caches between
users; no network for code that has not been explicitly pre-approved; no automatic running on page view (execution is always an
explicit user action, queued, and shown as such in the extension).

## 4. Validation plan (to do when a suitable environment exists)
1. Stand up a sandbox host (VM) with gVisor or Kata and a rootless engine; record versions.
2. An **escape/abuse test suite** run *as the job*: fork bomb, memory bomb, disk fill, infinite loop, long sleep (timeout kill),
   huge output (cap), writing outside scratch, reading `/proc/1`, `/etc/shadow`, the Docker socket, host env, cloud metadata,
   RFC1918 scans, DNS exfiltration, raw sockets, `ptrace`, mounting, loading modules, setuid binaries, CVE-style escape PoCs for the
   pinned runtime version. Each must fail and leave no resources behind (checked by the janitor test).
3. Concurrency: N parallel jobs cannot see each other; cleanup after crash of the runner agent.
4. Only then implement the job API and the extension "Run" action. **The sandbox is not complete until 1-3 have actually been run
   and their results recorded in [VALIDATION_STATUS.md](VALIDATION_STATUS.md).**

## 5. Open questions for the owner
Hosting target (single VM vs cloud with Firecracker), which package ecosystems to support first, per-user quotas, whether
private repositories will ever be in scope (this design says no), and the legal/abuse-handling process for hostile repositories.
