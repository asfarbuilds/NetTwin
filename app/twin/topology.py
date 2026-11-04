"""Create and destroy one isolated twin network per attempt (CLAUDE.md §5).

Everything here runs BEFORE any model output exists, or never touches it. All exec_run calls use
fixed argv lists.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from ipaddress import IPv4Network
from pathlib import Path

import docker
import docker.errors
from docker.types import IPAMConfig, IPAMPool

from app.schemas import Probe, ResolvedProbe

log = logging.getLogger(__name__)

ROUTER_IMAGE = "nettwin/router:1"
NODE_IMAGE = "nettwin/node:1"
NETS = ("mgmt", "lan", "wan")
ROUTER_HOST = 254  # Docker gives .1 to the host bridge
GATEWAY_HOST = 1
NODES: dict[str, tuple[str, int]] = {
    "admin": ("mgmt", 10),
    "staff": ("lan", 21),
    "hr": ("lan", 22),
    "nvr": ("lan", 50),
    "attacker": ("wan", 66),
}
NVR_CMD = ["busybox-extras", "httpd", "-f", "-p", "8443", "-h", "/srv/www"]
SINKHOLE_IP = "0.0.0.0"  # noqa: S104 - a DNS answer value, not a bind address
LABEL = "nettwin"

# Applied to every container (§5). No environment, no host network, no docker socket, never privileged.
_COMMON = dict(
    pids_limit=64,
    nano_cpus=500_000_000,
    security_opt=["no-new-privileges"],
    read_only=True,
    tmpfs={"/tmp": "rw,size=16m", "/run": "rw,size=8m"},  # noqa: S108 - tmpfs inside the container
    cap_drop=["ALL"],
)


class TwinError(RuntimeError):
    """The twin could not be built. Never caused by model output."""


@dataclass(frozen=True)
class Addressing:
    subnets: dict[str, IPv4Network]

    def _host(self, net: str, host: int) -> str:
        return str(self.subnets[net].network_address + host)

    def router_ip(self, net: str) -> str:
        return self._host(net, ROUTER_HOST)

    def gateway_ip(self, net: str) -> str:
        return self._host(net, GATEWAY_HOST)

    def node_ip(self, node: str) -> str:
        net, host = NODES[node]
        return self._host(net, host)

    def resolver_for(self, node: str) -> str:
        """Each node's router interface: its default gateway and its DNS server."""
        return self.router_ip(NODES[node][0])

    def target_ip(self, target: str) -> str:
        if target.startswith("router_"):
            return self.router_ip(target.removeprefix("router_"))
        return self.node_ip(target)


def resolve(probe: Probe, addr: Addressing, origin: str, probe_id: str | None = None) -> ResolvedProbe:
    return ResolvedProbe(
        id=probe_id or probe.id,
        origin=origin,  # type: ignore[arg-type]
        source=probe.source,
        kind=probe.kind,
        ip=addr.target_ip(probe.target),
        target_label=probe.target,
        expect=probe.expect,
        port=probe.port,
        query=probe.query,
        description=probe.description,
    )


class SubnetPool:
    """Hands out 3 distinct /24s per attempt from NETTWIN_SUBNET_POOL. Thread-safe."""

    def __init__(self, pool: IPv4Network):
        self._all = list(pool.subnets(new_prefix=24))
        self._used: set[IPv4Network] = set()
        self._lock = threading.Lock()

    def allocate(self, in_use: set[IPv4Network] | frozenset = frozenset()) -> dict[str, IPv4Network]:
        with self._lock:
            free = [s for s in self._all if s not in self._used and not any(s.overlaps(u) for u in in_use)]
            if len(free) < len(NETS):
                raise TwinError("subnet pool exhausted")
            picked = dict(zip(NETS, free[: len(NETS)], strict=True))
            self._used.update(picked.values())
            return picked

    def release(self, subnets: dict[str, IPv4Network]) -> None:
        with self._lock:
            self._used.difference_update(subnets.values())


def docker_subnets(client: docker.DockerClient) -> set[IPv4Network]:
    """Subnets already used by any Docker network (e.g. another process's twin)."""
    used = set()
    for net in client.networks.list():
        for cfg in (net.attrs.get("IPAM") or {}).get("Config") or []:
            if subnet := cfg.get("Subnet"):
                try:
                    used.add(IPv4Network(subnet))
                except ValueError:
                    pass  # IPv6 subnets
    return used


def bridge_name(attempt_id: str, net: str) -> str:
    """nt<8-char id><m|l|w>; the nt prefix is matched by the host firewall rule (§9)."""
    name = f"nt{attempt_id}{net[0]}"
    assert len(name) <= 15, name  # noqa: S101 - kernel IFNAMSIZ invariant
    return name


LOCAL_DNS_NAMES = ("intranet.local", "nvr.local")  # the only names the twin resolver answers (no upstream)


def dnsmasq_conf(addr: Addressing) -> str:
    """Local records only, no upstream servers. Written by trusted code from fixed topology data."""
    return "\n".join(
        [
            "no-resolv",
            "no-hosts",
            "bind-dynamic",
            "domain-needed",
            "bogus-priv",
            "log-queries",
            *(f"address=/{name}/{addr.node_ip('nvr')}" for name in LOCAL_DNS_NAMES),
            "",
        ]
    )


_ADDR_LINE = re.compile(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/")


def parse_router_ifaces(ip_addr_output: str, addr: Addressing) -> dict[str, str]:
    """Map net -> interface name from `ip -o -4 addr show` inside the router.
    Docker usually names them eth0/1/2 in connect order, but that's not guaranteed."""
    by_ip = {addr.router_ip(net): net for net in NETS}
    ifaces = {}
    for line in ip_addr_output.splitlines():
        if m := _ADDR_LINE.match(line.strip()):
            name, ip = m.group(1).split("@")[0], m.group(2)
            if ip in by_ip:
                ifaces[by_ip[ip]] = name
    if set(ifaces) != set(NETS):
        raise TwinError(f"router interfaces not found for {set(NETS) - set(ifaces)}")
    return ifaces


@dataclass
class Twin:
    attempt_id: str
    addr: Addressing
    attempt_dir: Path
    networks: dict = field(default_factory=dict)  # net -> docker Network
    containers: dict = field(default_factory=dict)  # "router" | node name -> docker Container
    router_ifaces: dict[str, str] = field(default_factory=dict)
    teardown_clean: bool = False

    def name(self, suffix: str) -> str:
        return f"nettwin-{self.attempt_id}-{suffix}"


def _labels(attempt_id: str) -> dict[str, str]:
    return {LABEL: "1", f"{LABEL}.attempt": attempt_id, f"{LABEL}.created": str(int(time.time()))}


def _exec_ok(container, argv: list[str], privileged: bool = False) -> str:
    code, out = container.exec_run(argv, privileged=privileged)
    text = (out or b"").decode(errors="replace")
    if code != 0:
        raise TwinError(f"{container.name}: {' '.join(argv)} exited {code}: {text.strip()[:300]}")
    return text


def _build(client: docker.DockerClient, twin: Twin) -> None:
    labels = _labels(twin.attempt_id)
    addr = twin.addr

    for net in NETS:
        subnet = addr.subnets[net]
        twin.networks[net] = client.networks.create(
            twin.name(net),
            driver="bridge",
            internal=True,  # zero egress
            enable_ipv6=False,
            ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=str(subnet), gateway=addr.gateway_ip(net))]),
            options={"com.docker.network.bridge.name": bridge_name(twin.attempt_id, net)},
            labels=labels,
        )

    mgmt = twin.name("mgmt")
    router = client.containers.create(
        ROUTER_IMAGE,
        name=twin.name("router"),
        labels=labels,
        mem_limit="256m",
        memswap_limit="256m",
        cap_add=["NET_ADMIN", "NET_BIND_SERVICE"],  # nft + forwarding; dnsmasq :53, dropbear :22
        sysctls={"net.ipv4.ip_forward": "1"},
        volumes={str(twin.attempt_dir): {"bind": "/etc/nettwin", "mode": "ro"}},
        network=mgmt,
        networking_config={mgmt: client.api.create_endpoint_config(ipv4_address=addr.router_ip("mgmt"))},
        **_COMMON,
    )
    twin.containers["router"] = router
    for net in ("lan", "wan"):
        twin.networks[net].connect(router, ipv4_address=addr.router_ip(net))
    router.start()

    for node, (net, _host) in NODES.items():
        netname = twin.name(net)
        c = client.containers.create(
            NODE_IMAGE,
            command=NVR_CMD if node == "nvr" else None,
            name=twin.name(node),
            labels=labels,
            mem_limit="128m",
            memswap_limit="128m",
            dns=[addr.resolver_for(node)],
            network=netname,
            networking_config={netname: client.api.create_endpoint_config(ipv4_address=addr.node_ip(node))},
            **_COMMON,
        )
        twin.containers[node] = c
        c.start()

    _wait_router_ready(router)
    twin.router_ifaces = parse_router_ifaces(_exec_ok(router, ["ip", "-o", "-4", "addr", "show"]), addr)

    # Spike option (a): one-time privileged exec, before any model output exists. Nodes keep zero caps.
    for node in NODES:
        _exec_ok(
            twin.containers[node],
            ["ip", "route", "replace", "default", "via", addr.resolver_for(node)],
            privileged=True,
        )


def _wait_router_ready(router, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if all(router.exec_run(["pidof", d])[0] == 0 for d in ("dnsmasq", "dropbear")):
            return
        time.sleep(0.1)
    router.reload()
    raise TwinError(f"router daemons not ready (status {router.status}): {router.logs(tail=20)!r}")


def leftovers(client: docker.DockerClient, attempt_id: str) -> int:
    sel = {"label": f"{LABEL}.attempt={attempt_id}"}
    return len(client.containers.list(all=True, filters=sel)) + len(client.networks.list(filters=sel))


def destroy(client: docker.DockerClient, twin: Twin) -> bool:
    """Remove everything belonging to this attempt. Never raises. Returns True if nothing is left."""
    sel = {"label": f"{LABEL}.attempt={twin.attempt_id}"}
    for c in list(twin.containers.values()):
        _quietly(c.remove, force=True)
    for c in client.containers.list(all=True, filters=sel):  # anything we lost track of
        _quietly(c.remove, force=True)
    for n in list(twin.networks.values()):
        _quietly(n.remove)
    for n in client.networks.list(filters=sel):
        _quietly(n.remove)
    try:
        left = leftovers(client, twin.attempt_id)
    except Exception:
        log.exception("attempt %s: could not verify teardown", twin.attempt_id)
        return False
    if left:
        log.error("attempt %s: %d docker objects left after teardown", twin.attempt_id, left)
    return not left


def _quietly(fn, **kwargs) -> None:
    try:
        fn(**kwargs)
    except docker.errors.NotFound:
        pass
    except Exception:  # teardown must never raise
        log.exception("teardown step failed")


@contextmanager
def twin_up(client: docker.DockerClient, pool: SubnetPool, attempt_id: str, attempt_dir: Path) -> Iterator[Twin]:
    """Create a fresh twin, yield it, and ALWAYS destroy it (lifecycle discipline, §2.3)."""
    subnets = pool.allocate(docker_subnets(client))
    twin = Twin(attempt_id=attempt_id, addr=Addressing(subnets), attempt_dir=attempt_dir)
    try:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        # Root inside the router has no DAC_READ_SEARCH: the dir must be world-traversable, files readable.
        attempt_dir.chmod(0o755)
        conf = attempt_dir / "dnsmasq.conf"
        conf.write_text(dnsmasq_conf(twin.addr), encoding="utf-8", newline="\n")  # LF even if run on Windows
        conf.chmod(0o644)
        _build(client, twin)
        yield twin
    finally:
        twin.teardown_clean = destroy(client, twin)
        pool.release(subnets)
