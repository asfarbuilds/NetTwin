# NetTwin progress

## Phase 0: Spike, DONE (4 Nov 2026)
- VM: Vultr vhp-2c-4gb, Ubuntu 24.04.5, Docker 29.9.3. **Spike: 10/10 checks pass.** Details in `docs/spike.md`.
- Decisions: node routing option (a), router caps NET_ADMIN + NET_BIND_SERVICE only, dnsmasq `--no-daemon`,
  parallel probes, br_netfilter startup guard.

## Phase 1: Twin and executor, DONE (4 Nov 2026, accepted on the VM)
- `app/config.py`: settings + startup checks (pool vs `/proc/net/route`, br_netfilter guard).
- `app/schemas.py`: `Probe` (closed enums, no leading `-`, strict ports), `TestPlan`, `ResolvedProbe`,
  `ProbeResult`, `AttemptResult`.
- `app/twin/topology.py`: subnet pool, addressing, network/container creation via the Docker SDK, router interface
  discovery, `twin_up()` context manager that always destroys.
- `app/twin/executor.py`: static validation, topology-variable header, `nft -c`/`nft -f`, fixed argv templates,
  parallel probes (SSH probes per source run in order), pure-Python evaluation incl. dig rcode parsing, CC-3, `run_attempt()`.
- `app/twin/invariants.py`: INV-1, CC-1/CC-2, plan conflict check (S1), max 3 router-SSH probes per source.
- `app/twin/janitor.py`, `app/twin/cli.py`.
- Tests: 96 unit tests (fake Docker client), ruff clean, 3 VM tests in `tests/vm/` (`pytest -m vm`).

**Design decision:** rulesets use nft topology variables (`$IF_LAN`, `$NVR`, ...) because subnets change per attempt.
See CLAUDE.md §6 step 3.

**VM acceptance, PASSED 4 Nov:**
- S3 CLI run: VERDICT PASS in 9.2 s. 9 plan probes + INV-1 pass; CC-1a/1b/2a/2b/3 all pass.
- `pytest -m vm`: 3/3 pass in 19.6 s (S3 passes; literal S1 trips INV-1 while the sandbox holds;
  an nft syntax error becomes evidence, not a crash).
- Zero leftover containers or networks afterwards.

Commands:
```
git pull && uv sync
uv run python -m app.twin.cli run --ruleset tests/fixtures/s3_ruleset.nft --plan tests/fixtures/s3_plan.json
uv run pytest -m vm -v
docker ps -a --filter label=nettwin=1; docker network ls --filter label=nettwin=1   # both empty
```

## Phase 2: Agent loop, CODE DONE locally (4 Nov); live VM acceptance pending
- `app/llm.py`: OpenAI SDK → Vultr Inference; `verify_model()` against `/v1/models`; temp 0.2, 60 s timeout,
  backoff 2/5/10 s on 429/5xx/connection errors; strips `<think>` + code fences; one repair retry; token accounting.
- `app/prompts/`: plan, generate, repair, summarize (`{{placeholder}}` templates; system/user split by `---USER---`).
  The generator prompt never names an invariant; it is told only how to treat `INVARIANT VIOLATION` feedback.
- `app/baselines/default.nft` (topology variables, nft -c OK), `app/scenarios.py` (S1–S3).
- `app/store.py`: SQLite runs / attempts / probe_results (WAL).
- `app/agent.py`: start_run (PLAN) → run_loop (checked plan → generate → execute on a fresh twin → retry ≤4)
  → diff, deterministic conflict message, summary written after the verdict. CLI: `python -m app.agent run S1`.
- Static validation now also rejects `define`/`redefine`/`undefine`.
- Conflict message has two honest variants: "Your request conflicted with INV-1 ..." (plan contradicted an invariant)
  vs "An earlier attempt violated INV-1; NetTwin caught it on the twin ..." (generator mistake caught).
- Tests: 123 unit tests green (fake OpenAI + scripted executor), incl. the full S1 containment path.

**Live S1 on the VM, PASSED 4 Nov (glm-5.3, run 322a59380764):**
- Plan: 17 probes; `mgmt-ssh-blocked` removed as "conflicts with INV-1".
- Timeline `#1 ✗ INV-1 lockout → #2 ✓`; final ruleset keeps exactly
  `iifname $IF_MGMT ip saddr $ADMIN ip daddr $ROUTER_MGMT tcp dport 22 accept`. 22/22 probes + CC-1..3 pass.
- Conflict message shown; summary accurate.
- **Too slow:** plan 91.5 s, generate#1 128.1 s, generate#2 43.0 s, summary 5.8 s (43k output tokens, mostly
  hidden reasoning). About 5 min per run. Model comparison needed.
- Fixed after the run: the diff ignores comment-only lines (baseline header comments showed as removals).

**Still to run:** S2, S3, and the model comparison.
Model choice: compare `glm-5.3`, `deepseek-v4.1-flash-1017`, `qwen3.8-27b` with `--model` on S1–S3.

## Phase 3: Web app, CODE DONE locally (4 Nov); VM browser acceptance pending
- `app/main.py`: FastAPI routes per CLAUDE.md §10. Workers run in threads. A semaphore caps concurrent runs (2),
  there's a per-IP rate limit (6/h), and Execute is an atomic compare-and-set (no double runs). The SSE event bus
  supports Last-Event-ID resume and 15 s keepalive pings.
  The LLM wrapper is per run (separate token accounting). Startup fails loudly if the key, model, Docker or images are bad.
- `app/report.py`: `export.nft` (production header with CHANGE_ME values, so nft refuses it until filled in) and
  `report.md` (evidence tables, caught-on-twin section, diff, ruleset).
- `app/static/`: single-page UI, vanilla JS. All server/model text goes in via textContent (a test asserts no innerHTML).
  Editable plan with 🔒 invariant rows, struck-out conflicting probes, live log, attempt timeline, evidence with raw
  output, containment panel, diff, Approve → downloads, footer with the model.
- `scripts/dev_ui.py`: local preview with a scripted LLM and fake twins (no Docker or key needed).
- 148 tests green (12 API tests incl. the full S1 flow, SSE replay/resume, rate limit, validation).

**VM acceptance (to run):** `uv run uvicorn app.main:app --host 127.0.0.1 --port 8000`, then an SSH tunnel
(`ssh -L 8000:127.0.0.1:8000 ali@<vm>`), then run S1–S3 in the browser, edit a plan, approve, download.

## Known issues
- The host nft rule is loaded by hand on the VM and **won't survive a reboot** until the Phase 5 systemd unit exists.
  After a reboot: `sudo nft -f deploy/nettwin-host.nft` (otherwise CC-2 fails, correctly).
- The S2 rate limit (`limit rate 3/minute`, default burst 5) isn't probed directly; capped at 3 SSH probes per source.
- Per-probe 5 s cap relies on each tool's own timeout (all ≤3 s) plus a per-attempt deadline; a hung `docker exec`
  is abandoned at the deadline and dies with the twin.
- CLI evidence table: long target labels push columns out of alignment (cosmetic).
