from ipaddress import IPv4Network

import pytest

from app.config import StartupError, check_br_netfilter, check_subnet_pool, parse_proc_route
from app.twin.topology import (
    NETS,
    Addressing,
    SubnetPool,
    TwinError,
    bridge_name,
    dnsmasq_conf,
    parse_router_ifaces,
)

POOL = IPv4Network("10.231.0.0/16")


def test_subnet_pool_allocates_distinct_and_releases():
    pool = SubnetPool(POOL)
    a, b = pool.allocate(), pool.allocate()
    assert set(a) == set(NETS)
    assert not set(a.values()) & set(b.values())
    pool.release(a)
    assert pool.allocate() == a


def test_subnet_pool_skips_docker_subnets_and_exhausts():
    pool = SubnetPool(IPv4Network("10.231.0.0/22"))  # only 4 /24s
    got = pool.allocate(in_use={IPv4Network("10.231.0.0/24")})
    assert IPv4Network("10.231.0.0/24") not in got.values()
    with pytest.raises(TwinError):
        pool.allocate()


def test_addressing():
    a = Addressing({n: IPv4Network(f"10.231.{i}.0/24") for i, n in enumerate(NETS)})
    assert a.router_ip("lan") == "10.231.1.254"
    assert a.gateway_ip("wan") == "10.231.2.1"
    assert a.node_ip("attacker") == "10.231.2.66"
    assert a.resolver_for("hr") == "10.231.1.254"
    assert a.target_ip("router_mgmt") == "10.231.0.254"
    assert a.target_ip("nvr") == "10.231.1.50"
    assert "address=/intranet.local/10.231.1.50" in dnsmasq_conf(a)
    assert "no-resolv" in dnsmasq_conf(a) and "server=" not in dnsmasq_conf(a)


def test_bridge_name_fits_ifnamsiz():
    assert bridge_name("abcd1234", "mgmt") == "ntabcd1234m"
    assert len(bridge_name("abcd1234", "wan")) <= 15


def test_parse_router_ifaces():
    a = Addressing({n: IPv4Network(f"10.231.{i}.0/24") for i, n in enumerate(NETS)})
    out = (
        "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
        "2: eth1    inet 10.231.0.254/24 brd 10.231.0.255 scope global eth1\n"
        "3: eth0@if9    inet 10.231.1.254/24 brd 10.231.1.255 scope global eth0\n"
        "4: eth2    inet 10.231.2.254/24 brd 10.231.2.255 scope global eth2\n"
    )
    assert parse_router_ifaces(out, a) == {"mgmt": "eth1", "lan": "eth0", "wan": "eth2"}
    with pytest.raises(TwinError):
        parse_router_ifaces(out.splitlines()[0], a)


PROC_ROUTE = """Iface	Destination	Gateway 	Flags	RefCnt	Use	Metric	Mask		MTU	Window	IRTT
enp1s0	00000000	01C0A8C0	0003	0	0	100	00000000	0	0	0
enp1s0	0000A8C0	00000000	0001	0	0	100	00FFFFFF	0	0	0
ntab12cd34m	0000E70A	00000000	0001	0	0	0	00FFFFFF	0	0	0
"""


def test_proc_route_parsing_and_pool_check():
    routes = parse_proc_route(PROC_ROUTE)
    assert ("enp1s0", IPv4Network("192.168.0.0/24")) in routes
    assert ("ntab12cd34m", IPv4Network("10.231.0.0/24")) in routes
    check_subnet_pool(POOL, routes)  # default route and our own nt* bridges are ignored
    with pytest.raises(StartupError, match="overlaps"):
        check_subnet_pool(POOL, routes + [("enp8s0", IPv4Network("10.0.0.0/8"))])  # e.g. a Vultr VPC


def test_br_netfilter_guard(tmp_path):
    f = tmp_path / "bridge-nf-call-iptables"
    check_br_netfilter(f)  # missing -> fine
    f.write_text("0\n")
    check_br_netfilter(f)
    f.write_text("1\n")
    with pytest.raises(StartupError, match="br_netfilter"):
        check_br_netfilter(f)
