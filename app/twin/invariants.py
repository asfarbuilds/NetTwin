"""System-defined checks the generator cannot see or edit (CLAUDE.md §7).

- Invariants (INV-*): properties every ruleset must keep. An independent change-control gate.
- Containment checks (CC-*): prove the sandbox, not the ruleset. Run on every attempt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas import REACH_EXPECT, ROUTER_TARGETS, Probe, ResolvedProbe, TestPlan
from app.twin.topology import LOCAL_DNS_NAMES, Addressing, resolve

MAX_SSH_PROBES_PER_SOURCE = 3  # so a correct S2 rate limit ("3/minute", burst 5) can't trip INV-1


@dataclass(frozen=True)
class Invariant:
    code: str
    probe: Probe
    violation: str  # fed back to the generator on retry
    preserves: str  # for the UI: "NetTwin preserved <preserves>."
    short: str  # timeline label, e.g. "#1 ✗ INV-1 lockout"


INVARIANTS: tuple[Invariant, ...] = (
    Invariant(
        code="INV-1",
        preserves="management access",
        short="lockout",
        probe=Probe(
            id="inv-1",
            source="admin",
            kind="ssh_banner",
            target="router_mgmt",
            expect="allow",
            description="Management access: admin keeps SSH to the router",
        ),
        violation="admin lost SSH to router_mgmt",
    ),
)
INVARIANTS_BY_CODE = {inv.code: inv for inv in INVARIANTS}


def conflicts_with(p: Probe, inv: Invariant) -> bool:
    """Same source, target and service as the invariant, but the opposite expectation."""
    q = inv.probe
    return (
        p.source == q.source
        and p.target == q.target
        and p.expect in REACH_EXPECT
        and q.expect in REACH_EXPECT
        and p.service_port == q.service_port  # icmp: None == None; ssh_banner and tcp/22 both 22
        and p.expect != q.expect
    )


def _is_router_ssh(p: Probe) -> bool:
    return p.target in ROUTER_TARGETS and p.service_port == 22


PRIVATE_HOSTS = ("admin", "staff", "hr", "nvr")


def unverifiable(p: Probe) -> str | None:
    """Why no ruleset could ever make this probe pass on the twin, or None. Such probes are removed:
    left in, they fail every attempt and the generator burns its whole output budget trying to fix them."""
    if p.kind == "dns":
        if p.expect != "resolves":
            return f"the twin resolver has no {p.expect} records"
        if not any(p.query == n or p.query.endswith("." + n) for n in LOCAL_DNS_NAMES):
            return f"the twin has no internet DNS; only {' and '.join(LOCAL_DNS_NAMES)} resolve"
    if p.source == "attacker" and p.target in PRIVATE_HOSTS and p.expect == "allow":
        return "the internet can't route to a private address; test the port forward on router_wan"
    return None


@dataclass
class PlanCheck:
    kept: list[Probe]
    removed: list[tuple[Probe, str]] = field(default_factory=list)  # (probe, reason) for the UI
    conflicts: list[str] = field(default_factory=list)  # invariant codes the request conflicts with


def check_plan(plan: TestPlan) -> PlanCheck:
    """Pure Python. Runs on the LLM plan and again on the user-edited plan before execution.

    1. Drop probes that contradict an invariant (otherwise S1 could never pass) and flag the conflict.
    2. Drop probes that can't pass on the twin whatever the ruleset (see unverifiable).
    3. Cap probes hitting router :22 per source; invariants are counted first.
    """
    check = PlanCheck(kept=[])
    ssh_count: dict[str, int] = {}
    for inv in INVARIANTS:
        if _is_router_ssh(inv.probe):
            ssh_count[inv.probe.source] = ssh_count.get(inv.probe.source, 0) + 1

    for p in plan.probes:
        hit = next((inv for inv in INVARIANTS if conflicts_with(p, inv)), None)
        if hit:
            check.removed.append((p, f"conflicts with {hit.code}"))
            if hit.code not in check.conflicts:
                check.conflicts.append(hit.code)
            continue
        if why := unverifiable(p):
            check.removed.append((p, why))
            continue
        if _is_router_ssh(p):
            n = ssh_count.get(p.source, 0)
            if n >= MAX_SSH_PROBES_PER_SOURCE:
                check.removed.append((p, f"more than {MAX_SSH_PROBES_PER_SOURCE} router SSH probes from {p.source}"))
                continue
            ssh_count[p.source] = n + 1
        check.kept.append(p)
    return check


def final_probes(plan: TestPlan, addr: Addressing) -> list[ResolvedProbe]:
    """The executable list: checked plan probes plus every invariant, ALWAYS appended,
    whatever the user did to the plan."""
    kept = check_plan(plan).kept
    probes = [resolve(p, addr, "plan") for p in kept]
    probes += [resolve(inv.probe, addr, "invariant", probe_id=inv.code) for inv in INVARIANTS]
    return probes


def containment_probes(addr: Addressing) -> list[ResolvedProbe]:
    """CC-1 and CC-2. CC-3 (secret hygiene) is not a network probe; see executor.check_secret_hygiene."""
    return [
        ResolvedProbe(
            id="CC-1a", origin="containment", source="attacker", kind="tcp", ip="1.1.1.1", port=443,
            target_label="internet 1.1.1.1", expect="deny", description="No egress: attacker can't reach the internet",
        ),
        ResolvedProbe(
            id="CC-1b", origin="containment", source="staff", kind="dns", ip="1.1.1.1", query="example.com",
            target_label="internet resolver 1.1.1.1", expect="no_answer",
            description="No egress: a public resolver gets no packets",
        ),
        ResolvedProbe(
            id="CC-2a", origin="containment", source="attacker", kind="tcp", ip=addr.gateway_ip("wan"), port=22,
            target_label="host (wan bridge .1)", expect="deny", description="Host isolation from the WAN twin net",
        ),
        ResolvedProbe(
            id="CC-2b", origin="containment", source="admin", kind="tcp", ip=addr.gateway_ip("mgmt"), port=22,
            target_label="host (mgmt bridge .1)", expect="deny", description="Host isolation from the mgmt twin net",
        ),
    ]
