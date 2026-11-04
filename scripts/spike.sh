#!/usr/bin/env bash
# NetTwin Phase 0 spike: stand up ONE twin by hand with the docker CLI and verify the assumptions in
# CLAUDE.md §5/§12 on the real Vultr VM. Run from the repo root:
#     ./scripts/spike.sh 2>&1 | tee spike-output.log
# Step 5 applies the host nft rule with sudo (you'll be prompted). It always tears the twin down on exit.
set -uo pipefail   # no -e: we want every check to run and report

REPO="$(cd "$(dirname "$0")/.." && pwd)"
P=nettwin-spike
M=10.231.250   # mgmt
L=10.231.251   # lan
W=10.231.252   # wan
R_M=$M.254; R_L=$L.254; R_W=$W.254
ADMIN=$M.10; STAFF=$L.21; HR=$L.22; NVR=$L.50; ATTACKER=$W.66
NODES=(admin staff hr nvr attacker)
ROUTER_IMG=nettwin/router:1
NODE_IMG=nettwin/node:1
ATT="${SPIKE_DIR:-/tmp/nettwin-spike-attempt}"
NODE_CAPS=()          # option (b) adds NET_ADMIN here if option (a) fails
ROUTE_MODE=a

RESULTS=()
pass() { RESULTS+=("PASS  $1"); echo "  [PASS] $1"; }
fail() { RESULTS+=("FAIL  $1"); echo "  [FAIL] $1"; }
note() { RESULTS+=("INFO  $1"); echo "  [INFO] $1"; }
hdr()  { echo; echo "=== $* ==="; }
dx()   { local c=$1; shift; docker exec "$P-$c" "$@"; }
now()  { date +%s.%N; }
since(){ awk -v a="$1" -v b="$(now)" 'BEGIN{printf "%.1f", b-a}'; }

# expect_ok/expect_fail DESC CMD... : run CMD, PASS if it succeeds/fails. Output shown on surprise.
expect_ok()   { local d=$1; shift; local out; if out=$("$@" 2>&1); then pass "$d"; else fail "$d"; echo "$out" | sed 's/^/         /' | head -20; fi; }
expect_fail() { local d=$1; shift; local out; if out=$("$@" 2>&1); then fail "$d"; echo "$out" | sed 's/^/         /' | head -20; else pass "$d"; fi; }

cleanup() {
    docker ps -aq --filter label=nettwin.spike=1 | xargs -r docker rm -f >/dev/null 2>&1
    docker network ls -q --filter label=nettwin.spike=1 | xargs -r docker network rm >/dev/null 2>&1
}
trap cleanup EXIT

# ---------------------------------------------------------------------------------------------------
hdr "0. Environment"
if [[ "$(uname -s)" != Linux ]] || docker info 2>/dev/null | grep -qi 'Docker Desktop'; then
    echo "This must run on the Vultr VM (Linux Docker Engine), not Docker Desktop. Set FORCE=1 to override."
    [[ "${FORCE:-0}" == 1 ]] || exit 2
fi
echo "kernel:         $(uname -r)"
echo "os:             $(. /etc/os-release && echo "$PRETTY_NAME")"
echo "docker:         $(docker version --format '{{.Server.Version}}')"
echo "docker fw:      $(docker info 2>/dev/null | grep -iE 'firewall|iptables' | xargs || echo n/a)"
echo "br_netfilter:   $(cat /proc/sys/net/bridge/bridge-nf-call-iptables 2>/dev/null || echo 'not loaded')"
echo -n "kernel modules: "
for m in nf_tables nf_conntrack nf_nat nft_ct nft_chain_nat nft_nat nft_limit nft_reject_inet nft_masq; do
    if [[ -d /sys/module/$m ]]; then echo -n "$m=ok "; else echo -n "$m=MISSING "; fi
done; echo

cleanup
if ip -4 route | grep -E "^10\.231\." >/dev/null; then
    fail "subnet pool 10.231.0.0/16 overlaps host routes:"; ip -4 route | grep -E "^10\.231\."
    exit 1
fi
pass "subnet pool 10.231.0.0/16 does not overlap host routes"

for img in "$ROUTER_IMG" "$NODE_IMG"; do
    docker image inspect "$img" >/dev/null 2>&1 || { echo "missing image $img; build first:"; \
        echo "  docker build -t $ROUTER_IMG images/router && docker build -t $NODE_IMG images/node"; exit 1; }
done

# ---------------------------------------------------------------------------------------------------
hdr "Setup: attempt dir, networks, router, nodes"
T0=$(now)
mkdir -p "$ATT"
chmod 0755 "$ATT"   # root-without-caps in the router has no DAC_READ_SEARCH: dir must be world-traversable

cat > "$ATT/dnsmasq.conf" <<EOF
no-resolv
no-hosts
bind-dynamic
domain-needed
bogus-priv
log-queries
address=/intranet.local/$NVR
EOF

# Hand-written test ruleset. Uses subnets, not interface names (ethN order isn't guaranteed).
# The limit/reject/masquerade rules exist only to prove those kernel modules are usable.
cat > "$ATT/ruleset.nft" <<EOF
flush ruleset
table inet filter {
    chain input {
        type filter hook input priority 0; policy drop;
        ct state established,related accept
        iif lo accept
        ip saddr $M.0/24 tcp dport 22 ct state new limit rate 30/minute accept
        ip saddr $M.0/24 accept
        ip saddr { $L.0/24, $M.0/24 } udp dport 53 accept
        ip saddr { $L.0/24, $M.0/24 } tcp dport 53 accept
        ip saddr $W.0/24 tcp dport 23 reject with tcp reset
        icmp type echo-request accept
    }
    chain forward {
        type filter hook forward priority 0; policy drop;
        ct state established,related accept
        ip saddr $L.0/24 ip daddr $W.0/24 accept
        ip daddr $NVR tcp dport 8443 ct status dnat accept
    }
}
table ip nat {
    chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;
        ip daddr $R_W tcp dport 8443 dnat to $NVR:8443
    }
    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        ip saddr 192.0.2.0/24 masquerade
    }
}
EOF
chmod 0644 "$ATT"/*

mknet() { # name subnet bridge-suffix
    docker network create --driver bridge --internal \
        --subnet "$2.0/24" --gateway "$2.1" \
        -o com.docker.network.bridge.name="ntspike00$3" \
        --label nettwin=1 --label nettwin.spike=1 --label nettwin.created="$(date +%s)" \
        "$P-$1" >/dev/null
}
mknet mgmt $M m && mknet lan $L l && mknet wan $W w || { fail "network create"; exit 1; }
pass "3 internal networks created (bridges ntspike00m/l/w)"

COMMON=(--label nettwin=1 --label nettwin.spike=1 --label "nettwin.created=$(date +%s)"
        --pids-limit 64 --cpus 0.5 --security-opt no-new-privileges
        --read-only --tmpfs /tmp --tmpfs /run --cap-drop ALL)

docker create --name "$P-router" "${COMMON[@]}" --memory 256m --memory-swap 256m \
    --cap-add NET_ADMIN --cap-add NET_BIND_SERVICE \
    --sysctl net.ipv4.ip_forward=1 \
    --network "$P-mgmt" --ip "$R_M" \
    -v "$ATT:/etc/nettwin:ro" "$ROUTER_IMG" >/dev/null || { fail "router create"; exit 1; }
docker network connect --ip "$R_L" "$P-lan" "$P-router"
docker network connect --ip "$R_W" "$P-wan" "$P-router"
docker start "$P-router" >/dev/null || { fail "router start"; docker logs "$P-router"; exit 1; }
pass "router created on mgmt/lan/wan at .254 (caps: NET_ADMIN, NET_BIND_SERVICE)"

mknodes() {
    local spec name net ip gw cmd
    for spec in "admin mgmt $ADMIN $R_M" "staff lan $STAFF $R_L" "hr lan $HR $R_L" \
                "nvr lan $NVR $R_L" "attacker wan $ATTACKER $R_W"; do
        read -r name net ip gw <<<"$spec"
        cmd=()
        [[ $name == nvr ]] && cmd=(busybox-extras httpd -f -p 8443 -h /srv/www)
        docker run -d --name "$P-$name" "${COMMON[@]}" "${NODE_CAPS[@]}" --memory 128m --memory-swap 128m \
            --network "$P-$net" --ip "$ip" --dns "$gw" "$NODE_IMG" "${cmd[@]}" >/dev/null \
            || { fail "node $name create"; return 1; }
    done
}
mknodes || exit 1
pass "5 nodes created (cap_drop ALL)"
echo "  twin up in $(since "$T0")s"

# ---------------------------------------------------------------------------------------------------
hdr "1. nft loads inside the router (incl. NAT, limit, reject, masquerade)"
expect_ok "nft -c (syntax check) succeeds" dx router nft -c -f /etc/nettwin/ruleset.nft
expect_ok "nft -f (load) succeeds"         dx router nft -f /etc/nettwin/ruleset.nft
if dx router nft list ruleset 2>/dev/null | grep -q "dnat to $NVR:8443"; then pass "DNAT rule present in live ruleset"
else fail "DNAT rule missing from live ruleset"; fi
dx router sh -c 'ps' | sed 's/^/         /'
dx router ps | grep -q '[d]nsmasq' && pass "dnsmasq running under cap_drop ALL" || { fail "dnsmasq not running"; docker logs "$P-router" 2>&1 | tail -20; }
dx router ps | grep -q '[d]ropbear' && pass "dropbear running on read-only rootfs" || fail "dropbear not running"

# ---------------------------------------------------------------------------------------------------
hdr "2. Node default routes via the router .254"
gw_of() { case $1 in admin) echo $R_M;; attacker) echo $R_W;; *) echo $R_L;; esac; }
set_routes() { # $1 = a|b
    local n ok=0
    for n in "${NODES[@]}"; do
        if [[ $1 == a ]]; then
            docker exec --privileged "$P-$n" ip route replace default via "$(gw_of "$n")" >/dev/null 2>&1 || ok=1
        else
            dx "$n" ip route replace default via "$(gw_of "$n")" >/dev/null 2>&1 || ok=1
        fi
    done
    return $ok
}
if set_routes a; then
    pass "option (a): one-time privileged exec 'ip route replace' works with cap_drop ALL + no-new-privileges"
else
    fail "option (a) failed; recreating nodes with option (b): cap_add NET_ADMIN"
    ROUTE_MODE=b; NODE_CAPS=(--cap-add NET_ADMIN)
    for n in "${NODES[@]}"; do docker rm -f "$P-$n" >/dev/null; done
    mknodes || exit 1
    set_routes b && pass "option (b): ip route replace with NET_ADMIN works" || fail "option (b) also failed"
fi
for n in "${NODES[@]}"; do
    r=$(dx "$n" ip -4 route show default)
    [[ $r == *"via $(gw_of "$n")"* ]] && pass "$n default route: $r" || fail "$n default route wrong: '$r'"
done
expect_ok "staff can't change its route after setup (no NET_ADMIN in option a)" \
    bash -c "[[ $ROUTE_MODE == b ]] || ! docker exec $P-staff ip route replace default via $L.1 2>/dev/null"

# ---------------------------------------------------------------------------------------------------
hdr "3. Routing THROUGH the router across subnets (Docker internal-network isolation, §5 gotcha 2)"
docker exec -d "$P-attacker" nc -lk 9000
sleep 1
expect_ok "staff -> attacker:9000 (lan->wan forward allowed by ruleset)" dx staff nc -z -w 3 "$ATTACKER" 9000
code=$(dx attacker curl -s -m 3 -o /dev/null -w '%{http_code}' "http://$R_W:8443/" 2>&1)
[[ $code =~ ^[23] ]] && pass "attacker -> router_wan:8443 DNAT -> nvr returns HTTP $code" || fail "attacker -> router_wan:8443 DNAT got '$code'"
expect_fail "attacker -> staff:22 (wan->lan forward dropped by ruleset)" dx attacker nc -z -w 3 "$STAFF" 22
if ! dx staff nc -z -w 3 "$ATTACKER" 9000 >/dev/null 2>&1; then
    note "if cross-subnet checks failed, see DOCKER isolation rules below"
    sudo iptables-save 2>/dev/null | grep -iE 'isolation|ntspike' | head -30 | sed 's/^/         /'
fi

# ---------------------------------------------------------------------------------------------------
hdr "4. Zero egress from internal networks"
expect_fail "attacker -> 1.1.1.1:443 tcp" dx attacker nc -z -w 3 1.1.1.1 443
expect_fail "router  -> 1.1.1.1 ping"     dx router ping -c 1 -W 2 1.1.1.1
out=$(dx staff dig +noall +comments +answer +time=2 +tries=1 @1.1.1.1 example.com 2>&1)
if echo "$out" | grep -q 'status: NOERROR'; then fail "staff dig @1.1.1.1 got an answer: $out"
else pass "staff dig @1.1.1.1 example.com gets no answer ($(echo "$out" | grep -m1 -oE 'timed out|no servers|status: [A-Z]+' || echo none))"; fi

# ---------------------------------------------------------------------------------------------------
hdr "5. Host bridge gateway .1 reachability, before and after the host nft rule"
before_a=$(dx admin nc -z -w 3 "$M.1" 22 >/dev/null 2>&1 && echo REACHABLE || echo blocked)
before_w=$(dx attacker nc -z -w 3 "$W.1" 22 >/dev/null 2>&1 && echo REACHABLE || echo blocked)
note "before host rule: admin->$M.1:22 $before_a, attacker->$W.1:22 $before_w (REACHABLE proves the rule is needed)"
echo "  applying deploy/nettwin-host.nft with sudo (adds/replaces table inet nettwin_host only)"
if sudo nft -f "$REPO/deploy/nettwin-host.nft"; then
    pass "host rule loaded"
    expect_fail "after: admin -> mgmt gateway $M.1:22"    dx admin nc -z -w 3 "$M.1" 22
    expect_fail "after: attacker -> wan gateway $W.1:22"  dx attacker nc -z -w 3 "$W.1" 22
    expect_fail "after: attacker -> host public IP :22"   dx attacker nc -z -w 3 "$(ip -4 route get 1.1.1.1 | grep -oP 'src \K\S+')" 22
    expect_ok   "Docker's own rules survived (other nft tables still present)" \
        bash -c "sudo nft list tables | grep -v nettwin_host | grep -q table"
else
    fail "host rule failed to load"
fi

# ---------------------------------------------------------------------------------------------------
hdr "6. dnsmasq: local records, no upstream"
out=$(dx staff dig +noall +comments +answer +time=2 +tries=1 "@$R_L" intranet.local 2>&1)
echo "$out" | grep -q "$NVR" && pass "intranet.local resolves to $NVR via router_lan" || { fail "intranet.local did not resolve"; echo "$out" | sed 's/^/         /'; }
out=$(dx staff dig +noall +comments +answer +time=2 +tries=1 "@$R_L" example.com 2>&1)
note "unknown name example.com via dnsmasq -> $(echo "$out" | grep -m1 -oE 'status: [A-Z]+|timed out' || echo "$out")   (spec CC-1 no longer depends on this)"
out=$(dx admin dig +noall +comments +answer +time=2 +tries=1 "@$R_M" intranet.local 2>&1)
echo "$out" | grep -q "$NVR" && pass "admin resolves via router_mgmt too" || fail "admin -> router_mgmt DNS failed"

# ---------------------------------------------------------------------------------------------------
hdr "7. SSH banner probe"
b=$(dx admin nc -w 3 "$R_M" 22 2>/dev/null </dev/null | head -c 64)
[[ $b == SSH-* ]] && pass "admin -> router_mgmt:22 banner: $(echo "$b" | tr -d '\r\n')" || fail "admin banner got: '$b'"
b=$(dx staff nc -w 3 "$R_L" 22 2>/dev/null </dev/null | head -c 64)
[[ $b == SSH-* ]] && fail "staff got an SSH banner; the ruleset should drop it" || pass "staff -> router_lan:22 no banner (ruleset enforced)"
note "nc in node image: $(dx admin sh -c 'readlink -f "$(command -v nc)"; nc -h 2>&1 | head -1' | xargs)"

# ---------------------------------------------------------------------------------------------------
hdr "8. ping with cap_drop ALL, and NVR httpd"
expect_ok "admin ping router_mgmt (ICMP datagram socket, no NET_RAW)" dx admin ping -c 1 -W 2 "$R_M"
note "ping_group_range in node: $(dx admin cat /proc/sys/net/ipv4/ping_group_range | xargs)"
code=$(dx staff curl -s -m 3 -o /dev/null -w '%{http_code}' "http://$NVR:8443/" 2>&1)
[[ $code == 200 ]] && pass "staff -> nvr:8443 HTTP 200 (busybox-extras httpd)" || fail "nvr httpd got '$code'"
expect_ok "attacker nmap connect scan works without NET_RAW" dx attacker nmap -sT -Pn -p 8443 --max-retries 0 "$R_W"

# ---------------------------------------------------------------------------------------------------
hdr "9. Timing: 25 parallel probes that must time out (drop)"
T1=$(now)
pids=()
for i in $(seq 1 25); do
    docker exec "$P-staff" nc -z -w 3 "$R_L" $((10000 + i)) >/dev/null 2>&1 & pids+=($!)
done
denied=0; for p in "${pids[@]}"; do wait "$p" || denied=$((denied + 1)); done
dt=$(since "$T1")
[[ $denied == 25 ]] && pass "25/25 parallel deny probes timed out correctly in ${dt}s" || fail "only $denied/25 were denied (${dt}s)"
docker stats --no-stream --format '  {{.Name}}  mem={{.MemUsage}}  pids={{.PIDs}}' $(docker ps -q --filter label=nettwin.spike=1)

# ---------------------------------------------------------------------------------------------------
hdr "10. Teardown"
T2=$(now)
cleanup
echo "  teardown in $(since "$T2")s; whole twin lifecycle $(since "$T0")s"
[[ -z $(docker ps -aq --filter label=nettwin.spike=1) ]] && pass "no containers left" || fail "containers left behind"
[[ -z $(docker network ls -q --filter label=nettwin.spike=1) ]] && pass "no networks left" || fail "networks left behind"
ip -o link show | grep -q ntspike && fail "bridge interfaces left behind" || pass "no ntspike* bridges left"

# ---------------------------------------------------------------------------------------------------
hdr "SUMMARY (route mode: $ROUTE_MODE)"
printf '%s\n' "${RESULTS[@]}"
nfail=$(printf '%s\n' "${RESULTS[@]}" | grep -c '^FAIL' || true)
echo; echo "$nfail failure(s). Paste this whole output back to Claude."
