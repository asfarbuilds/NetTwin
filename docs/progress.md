# NetTwin progress

## Phase 0: Spike, DONE (4 Nov 2026)
- VM: Vultr vhp-2c-4gb, Ubuntu 24.04.5, Docker 29.9.3. **Spike: 10/10 checks pass.** Details in `docs/spike.md`.
- Decisions: node routing option (a), router caps NET_ADMIN + NET_BIND_SERVICE only, dnsmasq `--no-daemon`,
  parallel probes, br_netfilter startup guard.

## Phase 1: Twin and executor, CODE DONE locally (4 Nov); VM acceptance pending
- `app/config.py`: settings + startup checks (pool vs `/proc/net/route`, br_netfilter guard).
- `app/schemas.py`: `Probe` (closed enums, no leading `-`, strict ports), `TestPlan`, `ResolvedProbe`,
  `ProbeResult`, `AttemptResult`.
- `app/twin/topology.py`: subnet pool, addressing, network/container creation via the Docker SDK, router interface
  discovery, `twin_up()` context manager that always destroys.
- `app/twin/executor.py`: static validation, topology-variable header, `nft -c`/`nft -f`, fixed argv templates,
  parallel probes (SSH probes per source run in order), pure-Python evaluation incl. dig rcode parsing, CC-3, `run_attempt()`.
- `app/twin/invariants.py`: INV-1, CC-1/CC-2, plan conflict check (S1), max 3 router-SSH probes per source.
- `app/twin/janitor.py`, `app/twin/cli.py`.
- Tests: 96 unit tests green locally (mocked Docker), ruff clean. 3 VM tests in `tests/vm/` (`pytest -m vm`).
- Verified locally with `nft -c` in the router image: the header plus the S3 fixture parse, incl. `dnat to $NVR:8443`.

**Design decision:** rulesets use nft topology variables (`$IF_LAN`, `$NVR`, ...) because subnets change per attempt.
See CLAUDE.md §6 step 3.

**VM acceptance (to run):**
```
git pull && uv sync
uv run python -m app.twin.cli run --ruleset tests/fixtures/s3_ruleset.nft --plan tests/fixtures/s3_plan.json
uv run pytest -m vm -v
docker ps -a --filter label=nettwin=1; docker network ls --filter label=nettwin=1   # both empty
```

## Known issues
- The host nft rule is loaded by hand on the VM and **won't survive a reboot** until the Phase 5 systemd unit exists.
  After a reboot: `sudo nft -f deploy/nettwin-host.nft` (otherwise CC-2 fails, correctly).
- The S2 rate limit (`limit rate 3/minute`, default burst 5) isn't probed directly; capped at 3 SSH probes per source.
- Per-probe 5 s cap relies on each tool's own timeout (all ≤3 s) plus a per-attempt deadline; a hung `docker exec`
  is abandoned at the deadline and dies with the twin.
