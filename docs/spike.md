# Phase 0 spike findings

**Status:** run on the Vultr VM on 4 Nov 2026. **All 10 checks pass, 0 failures.** Route mode: option (a).

## Environment
- Vultr Cloud Compute vhp-2c-4gb (2 vCPU / 4 GB), Ubuntu 24.04.5 LTS, kernel 6.8.0-147-generic
- Docker Engine 29.9.3, firewall backend `iptables` (iptables-nft)
- `br_netfilter`: **not loaded** (important, see check 3)
- All nft modules present: nf_tables, nf_conntrack, nf_nat, nft_ct, nft_chain_nat, nft_nat, nft_limit, nft_reject_inet, nft_masq
- Subnet pool `10.231.0.0/16` doesn't overlap host routes (no VPC attached)

## Results

| # | Check | Result | Notes / impact on spec |
|---|---|---|---|
| 1 | nft loads in router (NAT, limit, reject, masq) | PASS | `nft -c` and `nft -f` both work; DNAT present in live ruleset |
| 2 | Node default route via router | PASS, **option (a)** | A one-time `exec_run(..., privileged=True)` works with `cap_drop=ALL` + no-new-privileges. Afterwards a node can't change its route. Nodes need **no** caps. |
| 3 | Routing through router across subnets + DNAT | PASS | staff→attacker, attacker→router_wan:8443→nvr (HTTP 200), wan→lan dropped. Docker's internal-network isolation did **not** interfere, *because br_netfilter isn't loaded*. The control plane must check this at startup (below). |
| 4 | Zero egress | PASS | attacker→1.1.1.1:443 denied, router ping 1.1.1.1 denied, `dig @1.1.1.1` timed out |
| 5 | Host bridge `.1` before/after host rule | PASS | **Before the rule, admin and attacker could both reach host sshd on `.1:22`**. That proves the host rule is required. After the rule: all blocked, including the host's public IP. Docker's tables survived. |
| 6 | dnsmasq local records, no upstream | PASS | `intranet.local` resolves on lan and mgmt. Unknown names → `REFUSED` |
| 7 | SSH banner probe | PASS | `SSH-2.0-dropbear_2024.85`. Binary KEX follows the banner line, so the evaluator must read the first line only. LAN banner blocked by ruleset. |
| 8 | ping with `cap_drop=ALL`, httpd | PASS | `ping_group_range = 0 2147483647` (Docker default); busybox-extras httpd serves 200; nmap `-sT` works |
| 9 | Timing | PASS | twin up **1.4 s**, 25 parallel deny probes **4.3 s**, teardown **0.6 s**. The 90 s budget is comfortable. Memory: <1 MB per container idle. |
| 10 | Clean teardown | PASS | no containers, networks or bridges left |

## Decisions for Phase 1
- **Node routing:** option (a). Nodes stay `cap_drop=["ALL"]` with no added caps.
- **Router caps:** exactly `NET_ADMIN` (nft, forwarding) and `NET_BIND_SERVICE` (dnsmasq :53, dropbear :22). Nothing else was needed.
- **dnsmasq:** `--no-daemon` (the `--user=root` variant fails under `cap_drop=ALL`; found during local pre-checks).
- **Startup guard:** if `/proc/sys/net/bridge/bridge-nf-call-iptables` exists and equals `1`, refuse to start
  (or warn loudly). With br_netfilter active, Docker's internal-network isolation rules could start dropping routed twin traffic.
- **Probe concurrency:** parallel execution confirmed. 25 timeouts in 4.3 s with `-w 3`.
- **Host rule:** currently loaded by hand. It is **not persistent across reboot** until the Phase 5 systemd unit exists.

## Local pre-checks (Docker Desktop, 4 Nov). These only caught image bugs
- dnsmasq `--user=root --group=root` dies under `cap_drop=ALL` (`failed to change group-id to root`). Fixed with `--no-daemon`.
- nft `limit rate 3/minute` defaults to `burst 5 packets`. That's relevant for S2 vs INV-1.
