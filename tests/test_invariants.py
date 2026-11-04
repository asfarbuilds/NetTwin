from ipaddress import IPv4Network

from app.schemas import Probe, TestPlan
from app.twin.invariants import (
    MAX_SSH_PROBES_PER_SOURCE,
    check_plan,
    containment_probes,
    final_probes,
)
from app.twin.topology import Addressing

ADDR = Addressing({n: IPv4Network(f"10.231.{i}.0/24") for i, n in enumerate(("mgmt", "lan", "wan"))})


def P(id, source, kind, target, expect, **kw):
    return Probe(id=id, source=source, kind=kind, target=target, expect=expect, **kw)


S1_PLAN = TestPlan(probes=[
    P("admin-ssh-blocked", "admin", "ssh_banner", "router_mgmt", "deny"),  # literal reading of S1
    P("admin-ssh-tcp", "admin", "tcp", "router_mgmt", "deny", port=22),
    P("staff-ssh", "staff", "ssh_banner", "router_lan", "deny"),
    P("wan-ssh", "attacker", "tcp", "router_wan", "deny", port=22),
    P("admin-ping", "admin", "icmp", "router_mgmt", "deny"),
])


def test_s1_conflicting_probes_removed_and_flagged():
    check = check_plan(S1_PLAN)
    removed = {p.id for p, _ in check.removed}
    assert removed == {"admin-ssh-blocked", "admin-ssh-tcp"}
    assert all("INV-1" in reason for _, reason in check.removed)
    assert check.conflicts == ["INV-1"]
    assert [p.id for p in check.kept] == ["staff-ssh", "wan-ssh", "admin-ping"]


def test_agreeing_probe_is_not_a_conflict():
    check = check_plan(TestPlan(probes=[P("a", "admin", "ssh_banner", "router_mgmt", "allow")]))
    assert check.conflicts == [] and len(check.kept) == 1


def test_invariants_always_appended_even_after_user_edits():
    edited = TestPlan(probes=[])  # user deleted everything
    probes = final_probes(edited, ADDR)
    assert [p.id for p in probes] == ["INV-1"]
    inv = probes[0]
    assert (inv.origin, inv.source, inv.kind, inv.ip, inv.expect) == (
        "invariant", "admin", "ssh_banner", "10.231.0.254", "allow")

    probes = final_probes(S1_PLAN, ADDR)
    assert probes[-1].id == "INV-1"
    assert "admin-ssh-blocked" not in {p.id for p in probes}


def test_ssh_probe_cap_counts_invariant_first():
    many = TestPlan(probes=[P(f"s{i}", "admin", "tcp", "router_mgmt", "allow", port=22) for i in range(5)])
    check = check_plan(many)
    assert len(check.kept) == MAX_SSH_PROBES_PER_SOURCE - 1  # INV-1 already uses one slot
    assert len(check.removed) == 5 - (MAX_SSH_PROBES_PER_SOURCE - 1)
    other = TestPlan(probes=[P(f"s{i}", "staff", "tcp", "router_lan", "deny", port=22) for i in range(3)])
    assert len(check_plan(other).kept) == 3


def test_containment_targets_outside_model_enum():
    cc = {p.id: p for p in containment_probes(ADDR)}
    assert cc["CC-1a"].ip == "1.1.1.1" and cc["CC-1a"].expect == "deny"
    assert cc["CC-1b"].kind == "dns" and cc["CC-1b"].ip == "1.1.1.1" and cc["CC-1b"].expect == "no_answer"
    assert cc["CC-2a"].ip == "10.231.2.1" and cc["CC-2b"].ip == "10.231.0.1"
    assert all(p.origin == "containment" for p in cc.values())
