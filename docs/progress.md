# NetTwin progress

## Phase 0: Spike (in progress)

**Done (locally, 4 Nov 2026):**
- Repo skeleton: git, `.gitignore`, `.gitattributes` (LF), secret pre-commit hook (`scripts/githooks/pre-commit`),
  LICENSE (MIT), `.env.example`, `pyproject.toml` (uv, deps from CLAUDE.md §3 only).
- Images: `images/router` (nftables, dnsmasq, dropbear, fixed entrypoint), `images/node`.
- `deploy/bootstrap_vm.sh`: Docker Engine, kernel modules, disables stock nftables.service, data dirs, uv.
- `deploy/nettwin-host.nft`: host isolation rule (own table only, never `flush ruleset`).
- `scripts/spike.sh`: one twin by hand, checks 1–10 from CLAUDE.md §12, auto-teardown.

**Next (on the VM):**
1. `git clone <repo> && cd nettwin`
2. `sudo ./deploy/bootstrap_vm.sh`, then log out/in (docker group)
3. `docker build -t nettwin/router:1 images/router && docker build -t nettwin/node:1 images/node`
4. `./scripts/spike.sh 2>&1 | tee spike-output.log` and paste the output back
5. Record findings in `docs/spike.md`; STOP and review before Phase 1.

**Known issues / open questions:**
- Docker internal networks may block routing through the router container (spike check 3).
- dnsmasq with `--user=root` under `cap_drop=ALL` is unverified (check 1/6).
- The host nft systemd unit is Phase 5; the spike loads the rule manually with sudo.
