import pytest
from pydantic import ValidationError

from app.schemas import Probe, TestPlan

GOOD = dict(id="wan-nvr", source="attacker", kind="tcp", target="router_wan", port=8443, expect="allow")


def probe(**over):
    return Probe(**{**GOOD, **over})


def test_valid_probe():
    p = probe()
    assert p.service_port == 8443


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "a; rm -rf /"),
        ("id", "$(curl x)"),
        ("id", "../etc"),
        ("id", "abc\n"),  # trailing newline must not slip past ^...$
        ("id", "-rf"),
        ("id", "ａbc"),  # fullwidth unicode
        ("id", "a" * 33),
        ("source", "router"),
        ("source", "admin; ls"),
        ("target", "10.0.0.1"),
        ("target", "router_mgmt\n"),
        ("kind", "sh"),
        ("port", 0),
        ("port", 70000),
        ("port", "22"),  # strings never become ports
        ("port", True),
        ("expect", "maybe"),
        ("description", "line1\nline2"),
        ("description", "x" * 201),
    ],
)
def test_rejects_injection_and_out_of_enum(field, value):
    with pytest.raises(ValidationError):
        probe(**{field: value})


@pytest.mark.parametrize(
    "query",
    ["-f", "-x", "-ffile", "example.com\n", "a b", "$(id)", "exa mple", "../x", "x" * 254, "ex‮ample"],
)
def test_dns_query_rejected(query):
    with pytest.raises(ValidationError):
        Probe(id="d", source="staff", kind="dns", target="router_lan", query=query, expect="resolves")


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        Probe(**GOOD, command="nc -e /bin/sh")


@pytest.mark.parametrize(
    "over",
    [
        dict(kind="tcp", port=None),
        dict(kind="http", port=None),
        dict(kind="icmp", port=22),
        dict(kind="ssh_banner", port=2222),
        dict(kind="tcp", query="example.com"),
        dict(kind="tcp", expect="resolves"),
        dict(kind="dns", port=None, query="example.com", expect="allow"),
        dict(kind="dns", port=None, query=None, expect="resolves", target="router_lan"),
        dict(kind="dns", port=None, query="example.com", expect="resolves", target="nvr"),
        dict(source="nvr", target="nvr"),
    ],
)
def test_kind_consistency(over):
    with pytest.raises(ValidationError):
        probe(**over)


def test_dns_ok():
    p = Probe(id="d", source="staff", kind="dns", target="router_lan", query="intranet.local", expect="resolves")
    assert p.service_port is None


def test_ssh_banner_service_port_is_22():
    assert probe(kind="ssh_banner", port=None, expect="deny").service_port == 22


def test_plan_limits():
    many = [probe(id=f"p{i}").model_dump() for i in range(21)]
    with pytest.raises(ValidationError):
        TestPlan.model_validate({"probes": many})
    with pytest.raises(ValidationError):
        TestPlan.model_validate({"probes": [GOOD, GOOD]})  # duplicate id
    assert len(TestPlan.model_validate({"probes": many[:20]}).probes) == 20


def test_plan_from_json_is_strict_about_ports():
    with pytest.raises(ValidationError):
        TestPlan.model_validate_json('{"probes":[{"id":"a","source":"staff","kind":"tcp","target":"nvr",'
                                     '"port":"22","expect":"deny"}]}')
