# Phase 0 spike findings

**Status:** scripts written locally on 4 Nov 2026; **not yet run on the VM.**
Run on the VM: `./scripts/spike.sh 2>&1 | tee spike-output.log`, then record results here.

| # | Check | Result | Notes / impact on spec |
|---|---|---|---|
| 1 | nft loads in router (NAT, limit, reject, masq) | pending | |
| 2 | Node default route via router (option a vs b) | pending | |
| 3 | Routing through router across subnets + DNAT | pending | Docker internal-network isolation risk (§5 gotcha 2) |
| 4 | Zero egress | pending | |
| 5 | Host bridge `.1` before/after host rule | pending | |
| 6 | dnsmasq local records, no upstream (rcode for unknown names) | pending | |
| 7 | dropbear on read-only rootfs, SSH banner probe | pending | |
| 8 | ping with `cap_drop=ALL`, busybox-extras httpd | pending | |
| 9 | Timing: 25 parallel deny probes, twin lifecycle | pending | Must fit 90 s budget |
| 10 | Clean teardown | pending | |

## Local pre-checks (Docker Desktop, 4 Nov). These only catch image bugs; they are NOT evidence about networking
- Both images build on `alpine:3.20`. `nc` is netcat-openbsd (`/usr/bin/nc`); httpd comes from `busybox-extras`.
- **Bug found and fixed:** dnsmasq with `--user=root --group=root` dies under `cap_drop=ALL`
  (`failed to change group-id to root: Operation not permitted`). Switched to `--no-daemon`, which skips the privilege drop.
- With `cap_drop=ALL` + `NET_ADMIN` + `NET_BIND_SERVICE`, read-only rootfs and no-new-privileges:
  dropbear serves its banner (`SSH-2.0-dropbear_2024.85`), dnsmasq serves on :53, and nft loads limit/reject/DNAT.
- dnsmasq answers unknown names with `REFUSED`, not NXDOMAIN. That confirms CC-1 must use `@1.1.1.1`.
- `ping` from a node with `cap_drop=ALL` works.
- nft `limit rate 3/minute` defaults to `burst 5 packets`. That's relevant for S2 vs INV-1.
- The SSH banner read returns the banner line plus binary KEX bytes, so the evaluator must check the first line only.
- `docker cp` into the router fails (read-only rootfs), as expected. The executor uses the bind mount.

## Environment
- Kernel / Docker version / firewall backend / br_netfilter: pending

## Router capabilities actually required
- `NET_ADMIN`: load nft ruleset, sysctl forwarding
- `NET_BIND_SERVICE`: dnsmasq :53, dropbear :22
- Others: pending (only add if the spike proves it)
