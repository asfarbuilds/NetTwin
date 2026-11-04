# CLAUDE.md — NetTwin

> Read this entire file before doing anything. It is the source of truth for this project.
> Re-read it at the start of every session, and read `docs/progress.md` to see where we left off.

---

## 0. Context

- **Event:** Vultr × lablab.ai "Agent Rush" hackathon, **Challenge 1: Blast Radius Zero**.
  Event page: https://lablab.ai/ai-hackathons/vultr-hackathon
- **Build window:** 3–8 Nov 2026. **Hard internal deadline: 7 Nov 2026, 23:00 PKT (UTC+5), fully submitted.**
  Feature freeze: 7 Nov 12:00 PKT. 8 Nov is buffer only, never planned work.
- **Builder:** Ali, a network & cloud engineer (MikroTik, Cisco, Linux, Docker, firewalls). Solo.
  Treat him as senior on networking and Linux. Don't explain basics; do explain non-obvious decisions.
- **Dev environment:** Code is written locally on Windows 11 (`D:\NetTwin`, Claude Code + PowerShell/Git Bash),
  then pushed to GitHub and pulled onto the production Vultr VM (Ubuntu 24.04).
  - **Locally:** writing code, ruff, and unit tests that don't need Docker (schemas, argv construction,
    static validation, evaluation, invariant appending). Twin code must be testable with a mocked Docker client.
  - **On the VM only:** the spike, building images, anything touching real twins, nftables, kernel modules,
    bridges, or the host firewall. Docker Desktop's WSL2 VM has a different kernel, bridge and netfilter setup,
    so local twin results are **not** evidence. Twin integration tests are marked `@pytest.mark.vm` and skipped off-VM.
  - **Line endings:** `.gitattributes` forces `eol=lf` for `*.sh`, `*.nft`, `*.py`, `*.md`, `Dockerfile`, `*.service`,
    `Caddyfile`. A CRLF in a shell script or nft file breaks it on the VM.
  - Paths in code use `pathlib`, never hardcoded `/var/lib/...` outside `config.py` defaults.
- **Judging (1–5 each):** Application of Technology, Presentation, Business Value, Originality.
  The public demo URL must work when a judge clicks it, and the GitHub code is read and scored.

---

## 1. What we are building

**NetTwin: every firewall change is proven on a disposable twin before it touches production.**

**The problem.** Network engineers push firewall changes blind. A bad push locks you out of the router,
takes the office offline, or silently leaves a port open. Today, "testing" means hoping.
LLMs can draft configs, but an unverified LLM-written ruleset applied to a real router is the
classic blast-radius problem.

**The flow:**

1. The user types a change request in plain English, e.g. *"Expose the NVR web UI on WAN port 8443, nothing else."*
2. The agent writes a **test plan** first (which source should reach which target, allow or deny).
   The user can review and edit it.
3. The agent writes an **nftables ruleset**.
4. The executor spins up a **fresh, isolated twin network** (router, admin, staff, hr, nvr, attacker containers),
   loads the ruleset into the twin router, and runs **real probes**: TCP connects, SSH banner grabs, HTTP, DNS, ICMP.
5. **Pass/fail is computed by code, never by the LLM.** Failures plus raw evidence go back to the agent.
   It retries, up to 4 attempts, each on a brand-new twin.
6. **Safety invariants** that the generator cannot see or edit (e.g. "admin must keep SSH to the router")
   act as an independent change-control gate. Catching a lockout is the demo's **containment moment**.
7. The result is a config diff, an evidence table, and the attempt history. A human clicks **Approve**,
   which unlocks the download of the `.nft` file and the evidence report. Every twin is destroyed.

**Non-goals (do not build):** MikroTik/RouterOS emulation, browser automation, multi-user auth,
pushing configs to real devices, Kubernetes, any agent framework.

---

## 2. Hard event requirements (non-negotiable)

1. **Every agent LLM call** goes to **Vultr Serverless Inference** (OpenAI-compatible) at
   `https://api.vultrinference.com/v1`. No other LLM provider in the runtime path.
   No local models (no GPUs are available at this event).
2. **The backend runs on a Vultr Cloud Compute VM** and serves the web app. Vultr is the control plane,
   not just static hosting.
3. **Containment, per Vultr's brief:**
   - *Process isolation:* model output never executes on the host or in the control plane.
   - *Secret hygiene:* no API keys or credentials inside any twin container.
   - *Resource limits:* time and memory caps on every run.
   - *Lifecycle discipline:* destroy the environment after every attempt.
4. **Public GitHub repo** (MIT license) with a README explaining the Vultr services used, the architecture, and the use case.
5. **The demo video shows at least one containment moment.**

---

## 3. Tech stack (use exactly this; ask Ali before adding any dependency)

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | Managed with `uv` (fallback: venv + pip) |
| API server | FastAPI + Uvicorn | Bind `127.0.0.1:8000` only; Caddy faces the internet |
| Validation | Pydantic v2 + pydantic-settings | All model output parsed into closed schemas |
| LLM client | `openai` Python SDK with `base_url` set to Vultr | No LangChain, no agent frameworks |
| Sandbox | Docker Engine + Docker SDK for Python (`docker`) | Docker calls are sync, so run them in a threadpool |
| Live updates | Server-Sent Events (`StreamingResponse`, `text/event-stream`) | Browser uses `EventSource` |
| Storage | SQLite via stdlib `sqlite3` | Tables: runs, attempts, probe_results |
| Frontend | One `index.html` + vanilla JS + one CSS file | No build step, no React, no CDN frameworks |
| Diff | Python `difflib` (unified diff) | Rendered server-side as structured JSON |
| TLS / proxy | Caddy | Automatic HTTPS |
| Process mgmt | systemd unit | `Restart=always` |
| Quality | pytest + ruff | Both run green before every commit |
| Twin images | `alpine:3.20` (pinned) | See §5 |

**Explicitly excluded:** n8n, LangChain, LlamaIndex, Pinecone, Ollama, Discord/Slack bots, Kubernetes,
React/Next.js, Tailwind. The agent loop is roughly 150 lines of readable Python, and judges will read it.

---

## 4. Architecture and trust boundaries

```
Browser ──HTTPS──▶ Caddy ──▶ FastAPI control plane (127.0.0.1:8000)      [TRUSTED]
                              ├─ agent loop ──HTTPS──▶ Vultr Serverless Inference
                              ├─ executor (Docker SDK, fixed argv templates)
                              ├─ SQLite (/var/lib/nettwin/nettwin.db)
                              └─ API key lives ONLY here (/etc/nettwin/env, chmod 600)
                                   │
                                   ▼ creates → probes → destroys, per attempt
                 ┌──────────── twin (per attempt) ──────────────────────────┐ [UNTRUSTED]
                 │  mgmt net:  admin ─────────┐                              │
                 │  lan net:   staff, hr, nvr ├── router (nftables, dnsmasq, │
                 │  wan net:   attacker ──────┘           dropbear)          │
                 │  all networks internal=True → zero egress                 │
                 └───────────────────────────────────────────────────────────┘
```

**Rules:**

- **Untrusted:** everything the model produces (ruleset text, test plan JSON) and everything inside a twin.
- Model output is only ever used in two ways:
  - (a) parsed by Pydantic into a closed schema, or
  - (b) written as a **data file** on the host, bind-mounted **read-only** into the twin router, and loaded there with `nft`.
- Model output is **never** executed on the host, **never** interpolated into a shell string,
  and **never** used as raw argv. All `exec_run` calls use argv lists built from fixed templates.
- The **agent never gets a shell.** It can only emit config text and a test plan.

---

## 5. Twin topology

**Networks.** Create three per attempt, each with:
- a unique name `nettwin-<attempt_id>-{mgmt,lan,wan}`
- `internal=True`, IPv6 off
- the bridge name set via option `com.docker.network.bridge.name` = `nt<8-char id><m|l|w>`
  (max 15 chars; the `nt` prefix is used by the host firewall rule in §9)
- the label `nettwin=1` plus a creation timestamp

**Subnets.** Allocate from a dedicated pool (default `10.231.0.0/16`, carved into /24s).
At startup, check the pool against the host's routes, because Vultr VPC also uses 10.x ranges.
Fail loudly on overlap.

**Docker gotcha:** Docker gives the gateway address (`.1`) to the host bridge.
That's why the router uses `.254`, and why the host must be firewalled from the twins (§9).

**Docker gotcha 2 (spike 4 Nov: not hit, because `br_netfilter` is not loaded on the VM; the control plane refuses to
start if `net.bridge.bridge-nf-call-iptables=1`):** for `internal=True` networks Docker installs isolation rules that drop
traffic whose destination is outside that bridge's subnet. With `br_netfilter` loaded, this can also drop
lan→wan packets that are being *bridged* to the router container's MAC, which breaks routing through the twin router.
If the spike shows this, the fallback is: non-internal bridge networks with
`com.docker.network.bridge.enable_ip_masquerade=false` and `com.docker.network.bridge.gateway_mode_ipv4=isolated`
(or equivalent), and egress blocked by the host rule in §9 (`iifname "nt*"` dropped on both input **and** forward).
Report to Ali before switching.

**Host kernel modules:** the router has no `SYS_MODULE`, so everything nft needs must already be loaded on the host:
`nf_tables`, `nft_ct`, `nft_chain_nat`, `nf_nat`, `nft_nat`, `nft_limit`, `nft_reject_inet`, `nft_masq`.
`deploy/bootstrap_vm.sh` loads them and writes `/etc/modules-load.d/nettwin.conf` so they survive reboot.

| Node | Network(s) | Address | Image | Purpose |
|---|---|---|---|---|
| router | mgmt, lan, wan | `.254` on each | router | nftables + dnsmasq + dropbear (banner only) |
| admin | mgmt | `.10` | node | Management workstation |
| staff | lan | `.21` | node | Normal LAN user |
| hr | lan | `.22` | node | HR user (for P1 DNS-exemption scenario) |
| nvr | lan | `.50` | node | `busybox httpd` on 8443 (from `busybox-extras`), stands in for an NVR web UI |
| attacker | wan | `.66` | node | "Internet" host with nmap, nc, curl |

**Images** (`images/router/Dockerfile`, `images/node/Dockerfile`):
- router: `nftables`, `dnsmasq`, `dropbear`, `iproute2`
  - dropbear host keys are **generated at image build time** (`dropbearkey` into `/etc/dropbear`),
    because the root filesystem is read-only at runtime.
  - dnsmasq runs with `--no-daemon`, which skips its privilege drop. With `cap_drop=ALL`, even
    `--user=root --group=root` fails on `setgroups()`/`setgid()` (verified 4 Nov). We don't add `SETUID`/`SETGID`.
    For unknown names with no upstream, dnsmasq answers `REFUSED`.
  - A small fixed entrypoint script starts dropbear and dnsmasq. It never reads model output.
- node: `nmap`, `netcat-openbsd`, `bind-tools`, `curl`, `iproute2`, `iputils`, `busybox-extras` (for `httpd`)
  - Verify `nc` resolves to netcat-openbsd, not busybox `nc` (call `/usr/bin/nc` explicitly if needed).
  - nmap runs without `NET_RAW`, so connect scans only (`-sT`). That's fine.
- All images built locally and tagged `nettwin/router:1`, `nettwin/node:1`

**Container limits (every container):**
- `mem_limit` 128m (router 256m), with memswap equal to memory
- `pids_limit` 64
- `nano_cpus` 0.5 CPU
- `security_opt=["no-new-privileges"]`
- `read_only=True` with tmpfs for `/tmp` and `/run`
- No env vars beyond image defaults, no host network, no Docker socket, never `privileged`

**Capabilities:**
- Nodes: `cap_drop=["ALL"]`.
- Router: `cap_drop=["ALL"]`, then add only `NET_ADMIN`, `NET_BIND_SERVICE`, plus anything the spike
  **proves** is required. Document every added cap with the reason.

**Router specifics:**
- `sysctls={"net.ipv4.ip_forward": "1"}`.
- The ruleset dir `/var/lib/nettwin/attempts/<id>/` is bind-mounted read-only at `/etc/nettwin`.
- dnsmasq runs with `no-resolv` and **no upstream servers**. Only local records exist,
  e.g. `address=/intranet.local/...`, plus sinkhole entries for P1.

**Node routing:**
- Nodes need their default route via the router `.254`, not the Docker bridge `.1`.
- Spike option (a): a one-time `exec_run(["ip","route","replace","default","via",ROUTER_IP], privileged=True)`
  at setup, before any model output exists. **Chosen: verified on the VM 4 Nov. Nodes keep zero caps.**
- Fallback (b): `cap_add=["NET_ADMIN"]` on nodes. This is acceptable because nodes never run model output;
  document it in the README if used.
- Set each node's `dns=[router_ip_on_that_net]`.

**Lifecycle:**
- Each attempt: create twin → run probes → destroy everything in a `finally` block.
- Janitor: on startup and every 5 minutes, remove any container or network labelled `nettwin=1`
  that is older than 10 minutes.

---

## 6. Agent loop (`app/agent.py`)

The pipeline runs these steps in order:

1. **PLAN.** Send the LLM the user request, the baseline ruleset, and the topology (node names, roles, subnets).
   It returns a `TestPlan` JSON of up to 20 probes. It does not write invariants; the system adds those.
   - **Conflict check (pure Python):** a plan probe *conflicts* with an invariant when it has the same
     `source`, `target` and port/service (e.g. `ssh_banner` to router_mgmt, or `tcp` to router_mgmt:22) but the
     opposite expectation. Conflicting probes are **removed from the executable plan**, shown struck-through in
     the UI with "conflicts with INV-1", and the run is flagged `conflicts=["INV-1"]`.
     Without this, S1 can never pass, because the plan would demand exactly what INV-1 forbids.
2. **REVIEW.** The UI shows the plan. LLM-written probes are editable and deletable.
   Invariants are shown with a lock icon and cannot be edited. CLI/API mode auto-approves the plan.
   The conflict check runs again on the user-edited plan before execution.
   - Cap: at most 3 probes (plan + invariants) may target router port 22 from the same source,
     so a correct SSH rate limit (S2) can't make INV-1 fail. Extra ones are rejected at plan validation.
3. **GENERATE.** Send the LLM the user request, baseline ruleset, topology, and approved plan.
   It returns `{ "ruleset": "...", "rationale": "..." }`.
   - **Topology variables (decided in Phase 1).** Every attempt gets fresh subnets, and the router's interface
     names aren't guaranteed, so rulesets must **never contain literal twin IPs or interface names**.
     The executor prepends `flush ruleset` plus nft `define`s:
     `IF_MGMT/IF_LAN/IF_WAN`, `NET_MGMT/NET_LAN/NET_WAN`, `ROUTER_MGMT/ROUTER_LAN/ROUTER_WAN`,
     `ADMIN/STAFF/HR/NVR/ATTACKER`. The generator and the baseline use `$IF_LAN`, `$NVR` and so on.
     The model never sees or chooses real IPs. For export, the same header is emitted with production values
     as placeholders for the user to fill in.
   - **The generator is NOT told the invariants up front.** They are an independent gate, the same way change-control
     doesn't let the author grade their own change. It only learns of an invariant when a violation is fed back
     on retry (step 7). This is a deliberate design choice and must be explained in the README in exactly these terms.
4. **STATIC VALIDATION.** Reject the ruleset if any of these hold:
   - it is larger than 20 KB
   - it contains the token `include` (case-insensitive)
   - it contains any character that is non-printable, other than `\n` and `\t` (`\r` is stripped first)
   - The executor always prepends `flush ruleset`.
5. **EXECUTE** on a fresh twin:
   - run `nft -c -f /etc/nettwin/ruleset.nft` inside the router (syntax check)
   - then `nft -f ...`
   - then run all probes (plan + invariants + containment checks)
   - **Probes run in parallel** (a thread pool, up to 8 at once). "Deny" probes only finish when they time out,
     so running 25+ of them one after another would blow the 90 s budget.
     Exception: probes that hit the same rate-limited service from the same source run in order.
6. **EVALUATE.** Pure Python compares expected vs actual. No LLM involvement.
7. **RETRY.** On failure, if attempts < `NETTWIN_MAX_ITERATIONS` (4), feed back the nft errors and the failing
   probes with raw evidence, including invariant violations such as
   `INVARIANT VIOLATION INV-1: admin lost SSH to router_mgmt`, then go to step 3.
   - If the user's request directly conflicts with an invariant, the agent must keep the invariant
     and explain the conflict. The UI then shows:
     *"Your request conflicted with INV-1; NetTwin preserved management access."*
   - That message is shown **deterministically**: when the run is flagged with a conflict (step 1), or when any
     attempt violated an INV and the final attempt passed. The LLM never decides whether to show it.
8. **RESULT.** Return:
   - a unified diff (baseline vs final)
   - the evidence table
   - the attempt timeline (e.g. `#1 ✗ INV-1 → #2 ✓`)
   - a plain-English summary, which the LLM writes *after* the verdict, from the evidence only
   - **Approve** is enabled only when everything passes. It unlocks `export.nft` and `report.md`.

**LLM client rules (`app/llm.py`):**

- Use `OpenAI(base_url=VULTR_INFERENCE_BASE_URL, api_key=VULTR_INFERENCE_API_KEY)`.
- **Never hardcode or invent model IDs.** At startup, `GET /v1/models` and verify that `VULTR_MODEL` exists;
  fail loudly if not. Log the model name, and show it in the UI footer and the README.
- Don't assume `response_format`/JSON-schema support on Vultr.
  - Instruct "return only JSON", strip code fences, then parse with Pydantic.
  - On a parse failure, do one repair retry, then fail the attempt cleanly.
- Use temperature 0.2, a 60 s timeout per call, and backoff on 429/5xx.
- Prompts live in `app/prompts/*.md`, not inline strings.

---

## 7. Schemas: the safety core (`app/schemas.py`)

The model never supplies IPs, commands, or flags. It picks from closed enums;
the executor maps them to IPs and fixed argv templates.

```python
from typing import Annotated, Literal
from pydantic import BaseModel, Field, StringConstraints

NodeName = Literal["admin", "staff", "hr", "nvr", "attacker"]
Target   = Literal["router_mgmt", "router_lan", "router_wan", "admin", "staff", "hr", "nvr"]

ProbeId  = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")]
# First char must not be "-", otherwise dig would parse the query as an option (-f, -x, ...).
DnsName  = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.-]{0,252}$")]

class Probe(BaseModel):
    id: ProbeId
    source: NodeName
    kind: Literal["tcp", "icmp", "http", "ssh_banner", "dns"]
    target: Target
    port: Annotated[int, Field(ge=1, le=65535)] | None = None
    query: DnsName | None = None   # dns only
    expect: Literal["allow", "deny", "resolves", "sinkholed", "nxdomain"]
    description: Annotated[str, StringConstraints(max_length=200)]
    # model_validator: enforce kind↔port/query/expect consistency
```

Containment checks need targets outside this enum (`1.1.1.1`, the bridge gateway `.1`).
Executable probes are an internal `ResolvedProbe` (concrete IPs) that the model can never produce: built from a
validated `Probe` by `topology.resolve`, or directly in `invariants.py` for CC checks.

**Fixed argv templates (executor):**

| kind | argv (run inside the source container) | Pass condition |
|---|---|---|
| tcp | `["nc","-z","-w","3",IP,PORT]` | exit 0 = allow |
| icmp | `["ping","-c","1","-W","2",IP]` | exit 0 = allow (relies on Docker's default `ping_group_range`; verify in spike) |
| http | `["curl","-s","-m","3","-o","/dev/null","-w","%{http_code}",f"http://{IP}:{PORT}/"]` | 2xx/3xx = allow |
| ssh_banner | `["nc","-w","3",IP,"22"]` | output starts with `SSH-` = allow |
| dns | `["dig","+noall","+comments","+answer","+time=2","+tries=1",f"@{RESOLVER}",QUERY]` | parse `status:` and the A records (below) |

**DNS evaluation.** Don't use `+short`: it hides the rcode, and a timeout prints `;; connection timed out` to stdout.
- `resolves`: status `NOERROR` with at least one A record that is not the sinkhole IP
- `sinkholed`: status `NOERROR` and every A record equals the sinkhole IP
- `nxdomain`: status `NXDOMAIN`
- `REFUSED`/`SERVFAIL`/timeout: recorded as the actual result, and never counted as a match for any of the above

Store raw stdout, stderr, exit code, and duration for every probe. That raw data is the evidence.

**Invariants (`app/twin/invariants.py`):** system-defined, always appended, can't be edited.

- **INV-1 Management access:** admin → router_mgmt `ssh_banner` expects allow.

**Containment checks:** run on every attempt and shown in their own UI panel.
These prove the sandbox, not the ruleset.

- **CC-1 No egress:** attacker → `1.1.1.1:443` tcp expects deny; staff → `dig @1.1.1.1 example.com` expects timeout.
  (Querying the twin's dnsmasq would only test dnsmasq's config, not egress.)
- **CC-2 Host isolation:** attacker → wan bridge gateway `.1:22` tcp expects deny; admin → mgmt gateway `.1:22` expects deny.
- **CC-3 Secret hygiene:** for every twin container, `container.attrs["Config"]["Env"]` (read from the Docker API, not by
  running `env` inside) contains no key or value matching `VULTR|KEY|TOKEN|SECRET`.

---

## 8. Baseline and demo scenarios

**Baseline:** `app/baselines/default.nft` holds a realistic, permissive SOHO ruleset:
- input accepts mgmt and lan, plus established/related; WAN input drops
- forward accepts lan→wan, plus established/related

The user may paste their own baseline instead.

**Seed scenarios** come from `GET /api/scenarios` and appear as UI buttons. They come from Ali's real incidents.

| ID | Priority | Request | Key probes |
|---|---|---|---|
| S1 | P0 | "Drop ALL inbound traffic to the router on every interface. No exceptions." | Literal compliance kills SSH, so **INV-1 fires** → agent revises and explains the conflict. **This is the containment moment.** |
| S2 | P0 | "SSH to the router only from the admin network, rate-limit new SSH connections to 3 per minute, and drop SSH from LAN and WAN." (Story: the 58,622 failed-login brute-force attack.) | admin ssh allow; staff ssh deny; attacker ssh deny. The rate limit itself is not probed; the README says so. The 3-probe cap from §6 keeps INV-1 from tripping it. |
| S3 | P0 | "Expose the NVR web UI (nvr:8443) to the internet on WAN port 8443. Nothing else reachable from WAN." | attacker http router_wan:8443 allow; attacker tcp router_wan:22 deny; attacker tcp router_wan:80 deny |
| S4 | P1 | "Sinkhole facebook.com, instagram.com and tiktok.com for LAN, but exempt HR." Needs a generated dnsmasq sinkhole config and an nft DNAT of HR's DNS to an unfiltered instance. | staff dns sinkholed; hr dns resolves |

**Start S4 only if S1–S3 are fully working by 6 Nov 12:00 PKT.**

---

## 9. Security checklist (implement, test, and document in the README)

- [ ] No `shell=True`, no `sh -c` containing model content, and argv lists only.
- [ ] All model output goes through Pydantic closed schemas. The ruleset reaches the router only as a read-only mounted file.
- [ ] `include` is rejected, there is a 20 KB cap, and the executor always prepends `flush ruleset`.
- [ ] The API key exists only in the control-plane env, and CC-3 proves it isn't inside any twin.
- [ ] Uvicorn binds `127.0.0.1`. Only Caddy (80/443) and SSH are exposed publicly.
- [ ] A **host nftables rule** blocks twins from reaching the host:
      `table inet nettwin_host { chain input { type filter hook input priority -10; iifname "nt*" drop; } }`.
      Install it through `deploy/nettwin-host.nft` plus a systemd unit. CC-2 verifies it.
  - The file starts with `table inet nettwin_host` + `delete table inet nettwin_host` (an idempotent reload),
    **never** `flush ruleset`, because that would wipe Docker's rules.
  - Disable Ubuntu's stock `nftables.service`: its `/etc/nftables.conf` begins with `flush ruleset`.
  - The unit is ordered `After=docker.service` and is re-applied if Docker restarts (`PartOf=docker.service`).
- [ ] The Docker socket is used only by the control-plane user. No container mounts it.
      The README states honestly that the `docker` group is root-equivalent on the host.
- [ ] Uvicorn runs with `--proxy-headers --forwarded-allow-ips=127.0.0.1`, and the rate limit keys on the
      client IP from `X-Forwarded-For` set by Caddy. Otherwise every visitor looks like `127.0.0.1` and shares one limit.
- [ ] Every Docker object is labelled `nettwin=1`, and the janitor runs.
- [ ] Concurrency is capped (`NETTWIN_MAX_CONCURRENT_RUNS=2`, asyncio semaphore plus a queue).
- [ ] Per-IP rate limit on run creation (`NETTWIN_RATE_LIMIT_PER_HOUR=6`) protects the credits.
- [ ] A hard wall-clock kill applies per attempt (`NETTWIN_RUN_TIMEOUT_S=90`), with a per-probe timeout of 5 s.
- [ ] Logs never contain secrets.
- [ ] pytest covers:
  - schema rejection of injection strings (`; rm -rf /`, `$(curl x)`, `../`, newlines incl. a trailing `\n`,
    unicode tricks, leading `-` in `query`/`id`)
  - argv construction
  - plan/invariant conflict detection (the S1 plan with `admin ssh deny` is stripped and flagged)
  - DNS output parsing (NOERROR, NXDOMAIN, REFUSED, timeout)
  - `include` rejection
  - invariants still appended after the user edits the plan
  - teardown happening even when a probe raises

---

## 10. API and UI

**Endpoints:**

- `POST /api/runs` `{request, baseline?}` → `{run_id}` (starts PLAN)
- `GET /api/runs/{id}` gives the full state. `PUT /api/runs/{id}/plan` saves the edited plan. `POST /api/runs/{id}/execute` starts the run.
- `GET /api/runs/{id}/events` is the SSE stream. Events: `planning`, `plan_ready`, `generating`, `twin_up`,
  `nft_check`, `probe`, `attempt_verdict`, `teardown`, `final`, `error`.
- `POST /api/runs/{id}/approve` works only if the run passed.
- `GET /api/runs/{id}/export.nft` and `GET /api/runs/{id}/report.md` are available only once approved.
- `GET /api/scenarios` and `GET /api/health` (health includes the model name and the Docker check).

**UI** (single page, dark technical aesthetic, monospace for configs, readable on a laptop and on video):

- Request box with scenario buttons, plus a collapsible baseline editor.
- Test plan table. Invariants carry a 🔒 and are not editable.
- Live log stream over SSE.
- Attempt timeline, e.g. `#1 ✗ INV-1 lockout → #2 ✓`.
- Evidence table: probe, expected, actual, and an expandable raw output.
- A **Containment checks** panel (CC-1..3) that shows green on every attempt.
- Diff view, plus an **Approve** button that unlocks the downloads.
- Footer: "LLM: <model> on Vultr Serverless Inference · Twin host: Vultr Cloud Compute".

---

## 11. Repo layout

```
nettwin/
├── CLAUDE.md  README.md  LICENSE (MIT)  .env.example  pyproject.toml
├── app/
│   ├── main.py            # FastAPI routes + SSE
│   ├── config.py          # pydantic-settings
│   ├── llm.py             # Vultr Serverless Inference client
│   ├── agent.py           # plan → generate → validate → execute → evaluate loop
│   ├── schemas.py         # Probe, TestPlan, RulesetProposal, AttemptResult, RunResult
│   ├── store.py           # SQLite
│   ├── prompts/           # plan.md, generate.md, repair.md, summarize.md
│   ├── baselines/default.nft
│   ├── scenarios.py
│   ├── twin/
│   │   ├── topology.py    # create/destroy networks + containers, subnet pool
│   │   ├── executor.py    # apply ruleset, fixed argv probe runner
│   │   ├── invariants.py  # INV-* and CC-* definitions
│   │   ├── janitor.py
│   │   └── cli.py         # python -m app.twin.cli run --ruleset x.nft --plan plan.json
│   └── static/            # index.html, app.js, style.css
├── images/router/Dockerfile  images/node/Dockerfile
├── deploy/                # Caddyfile, nettwin.service, nettwin-host.nft, bootstrap_vm.sh
├── scripts/spike.sh
├── tests/
└── docs/                  # architecture.md (Mermaid), spike.md, progress.md
```

**`.env.example`:**

```
VULTR_INFERENCE_API_KEY=
VULTR_INFERENCE_BASE_URL=https://api.vultrinference.com/v1
VULTR_MODEL=
NETTWIN_DATA_DIR=/var/lib/nettwin
NETTWIN_SUBNET_POOL=10.231.0.0/16
NETTWIN_MAX_CONCURRENT_RUNS=2
NETTWIN_MAX_ITERATIONS=4
NETTWIN_RUN_TIMEOUT_S=90
NETTWIN_RATE_LIMIT_PER_HOUR=6
```

---

## 12. Phases: do them in order, stop at each checkpoint

**Phase 0: Spike (target: 4 Nov night).**
Locally: `git init`, `.gitignore` (incl. `.env`), `.gitattributes` (LF), the pre-commit secret grep, the images,
and `scripts/spike.sh`. Push to GitHub. **On the VM:** run `deploy/bootstrap_vm.sh` (Docker Engine, kernel modules),
build both images, and run `scripts/spike.sh` to stand up one twin by hand and verify each of these:

1. nft loads inside the router, including a NAT chain (DNAT) and `limit rate` (the kernel modules are present).
2. Node default routes go via the router (option a vs b from §5).
3. **Routing through the router works across subnets:** staff → attacker, and attacker → router_wan:8443 DNAT → nvr.
   This is where Docker gotcha 2 (§5) shows up.
4. Internal networks have zero egress.
5. Twins can or cannot reach the host bridge IP, before and after the host rule.
6. dnsmasq starts under `cap_drop=ALL` and answers local records with no upstream. Record its rcode for unknown names.
7. dropbear starts on the read-only rootfs, and the SSH banner probe works.
8. `ping` works from a node with `cap_drop=ALL`. `busybox httpd` serves on nvr:8443.
9. Time for one full twin up → 25 parallel probes → down. It must fit within 90 s with margin.
10. Teardown is clean.

Record the findings in `docs/spike.md`.
**STOP and report to Ali before Phase 1.** If anything in this spec is wrong in practice, say so here.

**Phase 1: Twin and executor.** Build `topology.py`, `executor.py`, `invariants.py`, `janitor.py`, and the CLI.
Acceptance: a hand-written S3 ruleset plus a plan prints a correct evidence table, all CC checks pass,
and there are zero leftover Docker objects. Tests green.

**Phase 2: Agent loop.** Build `llm.py` (with the model check), the prompts, and `agent.py`.
Acceptance: the CLI runs S1, S2 and S3 end-to-end, S1 shows INV-1 firing and then recovering,
and every attempt is stored in SQLite.

**Phase 3: Web app.** Build the FastAPI routes, SSE, and UI.
Acceptance: Ali can run all three scenarios in the browser, edit the plan, approve, and download.

**Phase 4: Hardening.** Work through the §9 checklist, concurrency, rate limiting, timeouts, and the full test suite.

**Phase 5: Deploy.** Set up Caddy, the systemd unit, and the host nft rule.
For the domain, use a subdomain Ali provides, or `<ip>.sslip.io`.
Acceptance: a smoke test from outside the VM passes, and a reboot brings everything back.

**Phase 6: Docs (written incrementally, finalized here).** README, `docs/architecture.md` (Mermaid), LICENSE.
Freeze features at 7 Nov 12:00 PKT.

Phases 1–2 are written locally but their acceptance runs **on the VM** (`git pull` there, then run the CLI).

After every phase:
- run `ruff` and `pytest` locally; on the VM also run `pytest -m vm`
- commit with a clear message
- update `docs/progress.md` with what's done, what's next, and known issues, so it survives context resets

---

## 13. Working rules for Claude Code

- **Plan before each phase.** Show the plan in a few lines, then implement.
- **Small and readable beats clever.** No abstractions that exist "for later".
- **Ask before adding any dependency** not listed in §3.
- **Don't guess Vultr specifics.** Model IDs, API features, and limits are verified against `/v1/models` and the docs URLs below.
- **If reality contradicts this spec** (Docker networking behaviour, capability needs, Vultr API quirks),
  stop, explain, and propose a fix. Never silently work around it.
- **Never commit secrets.** `.env` is gitignored; add a pre-commit grep for `VULTR_INFERENCE_API_KEY=` followed by a value.
- **Never weaken a containment control to make a test pass.**
- Use `sudo` only for system setup steps (Docker install, host nft rule, Caddy, systemd), and say what each sudo command does.
- Claude Code works on the Windows checkout. Anything that must run on the VM is written as a script
  (`deploy/*.sh`, `scripts/*.sh`) for Ali to run over SSH, or he pastes the output back. Never claim a twin or
  network behaviour is verified based on a local Docker Desktop run.

---

## 14. README requirements (it is judged)

1. **Problem.** Open with the real story: a production MikroTik router hit by 58,622+ failed login attempts,
   and the hardening changes that followed. Every one of those changes was pushed blind.
2. **What NetTwin does,** plus the demo URL, the video link, and a screenshot.
3. **Architecture diagram** (Mermaid).
4. **How Vultr powers it:**
   - the Cloud Compute VM is the control plane and twin host
   - Serverless Inference handles every agent call (name the model)
   - the OpenAI-compatible endpoint
5. **Containment model:** a table mapping Vultr's four containment rules to the exact implementation,
   plus INV/CC definitions and why the generator never sees the invariants.
6. **The agent loop,** step by step.
7. **Honest limitations and roadmap:**
   - Docker namespaces, not microVMs. Roadmap: run each twin in a microVM per Vultr's sandboxing guide.
   - Linux nftables, not RouterOS. Roadmap: RouterOS export.
   - Single-tenant demo.
8. **Business value:** change-failure outages, MSPs managing hundreds of client firewalls,
   and compliance evidence attached to every change.
9. **Setup instructions** (fresh Ubuntu 24.04 VM to running app) and the license.

---

## 15. Reference URLs (fetch these when you need specifics instead of guessing)

**Event**
- Event page: https://lablab.ai/ai-hackathons/vultr-hackathon
- Rule book: https://lablab.ai/hackathon-rules
- Submission guide: https://lablab.ai/delivering-your-hackathon-solution
- Vultr Community showcase (optional): https://community.vultr.com/e/vultr-agent-rush-hackathon

**Vultr**
- Serverless Inference base URL: https://api.vultrinference.com/v1
- Live model list: https://api.vultrinference.com/v1/models
- Serverless Inference provisioning: https://docs.vultr.com/products/serverless/inference/provisioning
- Serverless Inference FAQ: https://docs.vultr.com/products/serverless/inference/faq
- Agent sandboxing on Vultr (Microsandbox; README roadmap reference): https://docs.vultr.com/how-to-set-up-agent-sandboxing-on-vultr-cloud-compute
- Vultr API: https://www.vultr.com/api/

**Python / backend**
- FastAPI: https://fastapi.tiangolo.com/
- Pydantic v2: https://docs.pydantic.dev/latest/
- pydantic-settings: https://docs.pydantic.dev/latest/concepts/pydantic_settings/
- OpenAI Python SDK: https://github.com/openai/openai-python
- uv: https://docs.astral.sh/uv/
- Ruff: https://docs.astral.sh/ruff/

**Docker**
- Docker Engine on Ubuntu: https://docs.docker.com/engine/install/ubuntu/
- Docker SDK for Python: https://docker-py.readthedocs.io/en/stable/
- Bridge networks (incl. `internal`, bridge name option): https://docs.docker.com/engine/network/drivers/bridge/
- Resource constraints: https://docs.docker.com/engine/containers/resource_constraints/

**Networking**
- nftables wiki: https://wiki.nftables.org/
- nft man page: https://www.netfilter.org/projects/nftables/manpage.html
- dnsmasq man page: https://thekelleys.org.uk/dnsmasq/docs/dnsmasq-man.html

**Deploy / frontend**
- Caddy reverse proxy: https://caddyserver.com/docs/quick-starts/reverse-proxy
- sslip.io: https://sslip.io/
- EventSource (SSE client): https://developer.mozilla.org/en-US/docs/Web/API/EventSource
- Mermaid: https://mermaid.js.org/
