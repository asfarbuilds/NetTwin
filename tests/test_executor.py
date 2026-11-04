from ipaddress import IPv4Network
from pathlib import Path

import pytest

from app.schemas import ResolvedProbe
from app.twin.executor import (
    MAX_RULESET_BYTES,
    RulesetRejected,
    build_argv,
    evaluate,
    failure_lines,
    parse_dig,
    topology_header,
    validate_ruleset,
)
from app.twin.topology import Addressing, Twin

FIXTURES = Path(__file__).parent / "fixtures"
ADDR = Addressing({n: IPv4Network(f"10.231.{i}.0/24") for i, n in enumerate(("mgmt", "lan", "wan"))})


def rp(kind, **kw):
    base = dict(id="p", origin="plan", source="staff", kind=kind, ip="10.231.1.50", target_label="nvr", expect="allow")
    return ResolvedProbe(**{**base, **kw})


# ------------------------------------------------------------- argv construction

def test_argv_templates_exact():
    assert build_argv(rp("tcp", port=8443)) == ["nc", "-z", "-w", "3", "10.231.1.50", "8443"]
    assert build_argv(rp("icmp")) == ["ping", "-c", "1", "-W", "2", "10.231.1.50"]
    assert build_argv(rp("http", port=8443)) == [
        "curl", "-s", "-m", "3", "-o", "/dev/null", "-w", "%{http_code}", "http://10.231.1.50:8443/"]
    assert build_argv(rp("ssh_banner")) == ["nc", "-w", "3", "10.231.1.50", "22"]
    assert build_argv(rp("dns", ip="10.231.1.254", query="intranet.local", expect="resolves")) == [
        "dig", "+noall", "+comments", "+answer", "+time=2", "+tries=1", "@10.231.1.254", "intranet.local"]


def test_argv_is_list_of_str_without_shell():
    for kind, kw in [("tcp", {"port": 1}), ("icmp", {}), ("http", {"port": 80}), ("ssh_banner", {}),
                     ("dns", {"query": "a.b", "expect": "resolves"})]:
        argv = build_argv(rp(kind, **kw))
        assert all(isinstance(a, str) for a in argv)
        assert argv[0] not in ("sh", "bash", "/bin/sh")


def test_argv_refuses_option_like_query():
    with pytest.raises(ValueError):
        build_argv(rp("dns", query="-f", expect="resolves"))


def test_argv_unknown_kind():
    with pytest.raises(ValueError):
        build_argv(rp("exec"))


# ------------------------------------------------------------- static validation

def test_include_rejected_any_case():
    for text in ['include "/etc/passwd"', "INCLUDE \"x\"", "table inet t {}\n  Include \"y\""]:
        with pytest.raises(RulesetRejected, match="include"):
            validate_ruleset(text)


@pytest.mark.parametrize("text", ["define NVR = 1.1.1.1", "redefine IF_WAN = lo", "undefine NVR", "  DEFINE X = 1"])
def test_define_rejected(text):
    with pytest.raises(RulesetRejected, match="topology variables"):
        validate_ruleset(f"{text}\ntable inet t {{}}")


def test_size_cap():
    with pytest.raises(RulesetRejected, match="KB"):
        validate_ruleset("#" * (MAX_RULESET_BYTES + 1))


@pytest.mark.parametrize("bad", ["\x00", "\x1b[31m", "‮", "​", " ", "\x7f"])
def test_non_printable_rejected(bad):
    with pytest.raises(RulesetRejected, match="non-printable"):
        validate_ruleset(f"table inet t {{ }}{bad}")


def test_crlf_stripped_tabs_and_unicode_text_allowed():
    out = validate_ruleset("table inet t {\r\n\tchain c { }\r\n}\r\n# keeps mgmt — reachable\n")
    assert "\r" not in out and "\t" in out and "—" in out


def test_empty_rejected():
    with pytest.raises(RulesetRejected):
        validate_ruleset("  \n")


def test_fixture_passes_validation():
    validate_ruleset((FIXTURES / "s3_ruleset.nft").read_text(encoding="utf-8"))


def test_header_starts_with_flush_and_defines_topology():
    twin = Twin("abcd1234", ADDR, Path("."), router_ifaces={"mgmt": "eth0", "lan": "eth2", "wan": "eth1"})
    h = topology_header(twin)
    assert h.splitlines()[0] == "flush ruleset"
    assert 'define IF_LAN = "eth2"' in h
    assert "define NET_WAN = 10.231.2.0/24" in h
    assert "define NVR = 10.231.1.50" in h
    assert "define ROUTER_MGMT = 10.231.0.254" in h


# ------------------------------------------------------------- evaluation

def test_tcp_icmp():
    assert evaluate(rp("tcp", port=1), 0, "")[:2] == ("allow", True)
    assert evaluate(rp("tcp", port=1, expect="deny"), 1, "")[:2] == ("deny", True)
    assert evaluate(rp("icmp"), 1, "")[:2] == ("deny", False)


def test_http():
    assert evaluate(rp("http", port=80), 0, "200")[:2] == ("allow", True)
    assert evaluate(rp("http", port=80), 0, "301")[:2] == ("allow", True)
    assert evaluate(rp("http", port=80), 28, "000")[:2] == ("deny", False)
    assert evaluate(rp("http", port=80, expect="deny"), 0, "404")[:2] == ("deny", True)


def test_ssh_banner_first_line_only():
    out = "SSH-2.0-dropbear_2024.85\r\n\x00\x00\x01\x14curve25519-sha256..."
    actual, passed, detail = evaluate(rp("ssh_banner"), 0, out)
    assert (actual, passed, detail) == ("allow", True, "SSH-2.0-dropbear_2024.85")
    assert evaluate(rp("ssh_banner"), 1, "")[:2] == ("deny", False)
    assert evaluate(rp("ssh_banner"), 0, "HTTP/1.0 400\r\nSSH-2.0-x")[0] == "deny"


DIG_NOERROR = """;; Got answer:
;; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 4242
;; flags: qr aa rd ra; QUERY: 1, ANSWER: 1, AUTHORITY: 0, ADDITIONAL: 1

intranet.local.		0	IN	A	10.231.1.50
"""
DIG_SINKHOLE = DIG_NOERROR.replace("10.231.1.50", "0.0.0.0")
DIG_NX = ";; ->>HEADER<<- opcode: QUERY, status: NXDOMAIN, id: 1\n"
DIG_REFUSED = ";; ->>HEADER<<- opcode: QUERY, status: REFUSED, id: 27925\n"
DIG_TIMEOUT = ";; communications error to 1.1.1.1#53: timed out\n;; no servers could be reached\n"


def test_parse_dig():
    assert parse_dig(DIG_NOERROR) == ("NOERROR", ["10.231.1.50"])
    assert parse_dig(DIG_NX) == ("NXDOMAIN", [])
    assert parse_dig(DIG_TIMEOUT) == (None, [])


@pytest.mark.parametrize(
    "out,expect,actual,passed",
    [
        (DIG_NOERROR, "resolves", "resolves", True),
        (DIG_SINKHOLE, "sinkholed", "sinkholed", True),
        (DIG_SINKHOLE, "resolves", "sinkholed", False),
        (DIG_NX, "nxdomain", "nxdomain", True),
        (DIG_REFUSED, "nxdomain", "refused", False),  # REFUSED never counts as NXDOMAIN
        (DIG_TIMEOUT, "nxdomain", "no_answer", False),
        (DIG_TIMEOUT, "no_answer", "no_answer", True),  # CC-1b
        (DIG_REFUSED, "no_answer", "refused", False),  # any reply means packets got out
    ],
)
def test_dns_evaluation(out, expect, actual, passed):
    got = evaluate(rp("dns", query="x.y", expect=expect), 0, out)
    assert got[:2] == (actual, passed)


def test_failure_lines_spell_out_invariants():
    from app.schemas import ProbeResult

    inv = ProbeResult(id="INV-1", origin="invariant", source="admin", kind="ssh_banner", target="router_mgmt",
                      expect="allow", actual="deny", passed=False)
    plan = ProbeResult(id="wan-ui", origin="plan", source="attacker", kind="http", target="router_wan", port=8443,
                       expect="allow", actual="deny", passed=False, detail="http 000")
    lines = failure_lines([inv, plan])
    assert lines[0].startswith("INVARIANT VIOLATION INV-1: admin lost SSH to router_mgmt")
    assert "router_wan:8443 expected allow, got deny [http 000]" in lines[1]
