# NetTwin progress

## Phase 0: Spike, DONE (4 Nov 2026)
- Repo skeleton, images, `deploy/bootstrap_vm.sh`, `deploy/nettwin-host.nft`, `scripts/spike.sh`.
- VM: Vultr vhp-2c-4gb, Ubuntu 24.04.5, Docker 29.9.3. **Spike: 10/10 checks pass.** Details in `docs/spike.md`.
- Decisions: node routing option (a), router caps NET_ADMIN + NET_BIND_SERVICE only, dnsmasq `--no-daemon`,
  parallel probes, br_netfilter startup guard.

## Phase 1: Twin and executor (next, awaiting Ali's go-ahead)
Planned: `config.py`, `schemas.py` (Probe/SystemProbe), `twin/topology.py`, `twin/executor.py`,
`twin/invariants.py`, `twin/janitor.py`, `twin/cli.py`, unit tests (mocked Docker) + `@pytest.mark.vm` integration test.
Acceptance (on VM): a hand-written S3 ruleset + plan prints a correct evidence table, all CC pass, zero leftovers.

## Known issues
- The host nft rule is loaded by hand on the VM and **won't survive a reboot** until the Phase 5 systemd unit exists.
  After a reboot: `sudo nft -f deploy/nettwin-host.nft`.
- The S2 rate limit (`limit rate 3/minute`, default burst 5) isn't probed directly; capped at 3 SSH probes per source.
