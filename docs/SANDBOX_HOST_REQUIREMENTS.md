# Sandbox execution host: prerequisites (Stage 14, PLANNING ONLY)

**Status: requirements only. No sandbox host exists, no container has been created, no untrusted code has been executed, and
isolation has NOT been shown to be secure.** Isolation may be claimed only after the host is built **in the intended environment**
and the abuse tests in [SANDBOX_DESIGN.md](SANDBOX_DESIGN.md) §4 have actually been run and recorded in
[VALIDATION_STATUS.md](VALIDATION_STATUS.md). Everything below is a requirement or an *untested illustration*.

## 1. This machine is not a candidate
The current development machine runs Windows 11 with no Docker. It holds the developer's personal files, browser profiles and
credentials; a Windows/WSL2 or Docker Desktop VM would also share state with the user session. **Hostile code must never run
here.** The host must be a separate, single-purpose machine.

## 2. Host requirements
| Area | Requirement |
|---|---|
| Machine | A dedicated **Linux VM or bare-metal server** that runs nothing else: no API, no database, no CI, no developer access beyond break-glass. Rebuilt from an image on a schedule and after any suspected incident ("cattle, not a pet") |
| OS / kernel | A current LTS Linux with a supported, promptly patched kernel (e.g. Ubuntu LTS or Debian stable), **cgroup v2**, user namespaces enabled, seccomp and AppArmor (or SELinux) available. Automatic security updates plus a defined reboot window; container-runtime and kernel CVE monitoring (an unpatched kernel or runtime defeats every other control) |
| Stronger isolation layer (**one required**) | **gVisor (`runsc`)**: a user-space kernel that intercepts syscalls, installed as a Docker/containerd runtime; or **Kata Containers**: each container in a lightweight VM with its own kernel (needs hardware virtualization, KVM, and nested virtualization if the host is itself a VM). Plain `runc` is not acceptable for untrusted code. Prefer the VM-per-job option (Kata/Firecracker) for the highest-risk workloads; gVisor reduces the kernel attack surface but is not a VM |
| Container engine | Docker Engine or containerd, **rootless or with user-namespace remapping** (`userns-remap`); no `docker.sock` or engine API exposed to jobs, the network or the control plane |
| Control plane | A small **runner agent** on the host pulls jobs over an authenticated, outbound-only channel and posts size-capped results. The API server never has Docker access and never connects *into* the host. The agent's credential can claim jobs and post results only |
| Secrets | **None on the host that matter**: no GitHub token, no `COMPASS_AUTH_SECRET`, no database credentials, no cloud instance role, no SSH keys of other systems. The agent's own credential is the only secret and is scoped and rotatable. Job environments start from an explicit empty environment |
| Storage | Per-job scratch on a **size-limited** volume (xfs project quota, a loop device, or a size-capped `tmpfs`), mounted `noexec,nosuid,nodev`; deleted with the job. Images stored read-only; image layers pinned by digest. Separate volume for logs with rotation and a cap |
| Network | Default **no network for jobs**. Egress, if ever allowed for a dependency-fetch phase, goes through an allow-listing proxy; host firewall (nftables) **denies** RFC1918 ranges, link-local, loopback of the host, the control plane and the **cloud metadata address 169.254.169.254** (and IMDSv2 hop limit 1 if on a cloud VM). No inbound connections to jobs. DNS only through the proxy. Host management access via a bastion with MFA |
| Monitoring | Resource, kill and OOM events; unexpected processes; outbound connection attempts (denied ones are signal); image/runtime versions; failed-cleanup and leaked-container alarms; central, tamper-resistant log shipping off the host |
| Time | NTP; wall-clock timeouts are enforced by the runner agent, not by code inside the job |

## 3. Per-job limits (all mandatory; values are starting points to be tuned, not validated numbers)
| Control | Setting |
|---|---|
| Lifetime | One container (and, in the strict tier, one microVM) **per job**; destroyed on completion, timeout or error; never reused; janitor removes anything older than the maximum |
| User | Non-root fixed high UID/GID, `--security-opt no-new-privileges`, no setuid binaries, user namespace |
| Privileges | **No `--privileged`**, `--cap-drop=ALL`, no added capabilities, default-deny seccomp profile, AppArmor/SELinux profile, no `--pid=host`, `--ipc=host`, `--net=host`, `--userns=host`, no `--device`, no `/sys` or extra `/proc` mounts |
| Filesystem | `--read-only` root; one small `tmpfs`/volume for scratch; **no host bind mounts**; never the Docker socket; the repository is cloned inside the sandbox |
| CPU / memory / processes | e.g. 1 CPU quota, 1 GiB memory hard limit with swap off (`--memory-swap` = `--memory`), `--pids-limit 256`, `--ulimit nofile/nproc`, a CPU-time cap |
| Disk | hard cap on scratch (e.g. 1 GiB) and on file count; repository size/file-count limit at clone time |
| Time | wall-clock limit (e.g. 10 minutes) enforced by the agent with SIGKILL and forced removal; plus an output-idle timeout |
| Output | stdout/stderr byte cap (e.g. 1 MiB) and line cap; stored as text, rendered as text only |
| Admission | per-user and global concurrency, queue depth, daily CPU-seconds budget; commands come from an allow-list shown to the user before any run; execution is always an explicit user action |
| Secrets in the job | none (no token, no inherited environment, no mounted credentials); private repositories out of scope |
| Cleanup | `finally` path: kill, remove container, network, volumes and temp files; verify removal; alarm on leaks |

### Untested illustration (do not rely on it)
```
# /etc/docker/daemon.json  (host, after installing gVisor):  { "runtimes": { "runsc": { "path": "/usr/local/bin/runsc" } } }
docker run --rm --runtime=runsc --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 256 --memory 1g --memory-swap 1g --cpus 1 --user 10001:10001 \
  --tmpfs /work:rw,noexec,nosuid,size=1g -e HOME=/work --workdir /work  <image@sha256:digest>  <command>
```
This shows the shape of the flags only. It was not run, the runtime is not installed anywhere, and a correct deployment needs the
host, firewall, quota, agent and cleanup layers around it.

## 4. Why "Docker alone" is not enough, again
Containers share the host kernel; a single kernel or runtime flaw (or one wrong flag such as a mounted socket) gives a job the host.
gVisor/Kata add a boundary that a kernel bug in the guest does not automatically cross, and a disposable, secret-free, network-isolated
VM limits what a successful escape can reach. Defence in depth is the point: no single layer is trusted.

## 5. Acceptance before any claim of isolation
Run on the real host, with results recorded: the abuse suite of SANDBOX_DESIGN.md §4 (fork/memory/disk bombs, timeouts, output caps,
reads of host paths and the Docker socket, metadata and RFC1918 access, DNS exfiltration, raw sockets, `ptrace`, mounts, module loads,
setuid, escape PoCs for the *pinned* runtime versions); concurrent jobs cannot see each other; cleanup after an agent crash; the host
holds no usable secrets (a credential-scan of the image and environment); and a review of the runner agent's code and permissions.
Until all of that passes, the Project Lab stays disabled by default and refuses to run without the required runtime.

## 6. Decisions needed from the owner
Where the host runs (cloud VM vs own hardware), which isolation tier (gVisor vs Kata/Firecracker) given budget and risk, who patches
and monitors it, and whether dependency installation (network) is in scope for the first version (the safest first version is
**no network at all** and only repositories whose commands need none).
