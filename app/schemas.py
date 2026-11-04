"""The safety core. The model never supplies IPs, commands or flags: it picks from closed enums,
and the executor maps those to IPs and fixed argv templates (CLAUDE.md §7)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NodeName = Literal["admin", "staff", "hr", "nvr", "attacker"]
Target = Literal["router_mgmt", "router_lan", "router_wan", "admin", "staff", "hr", "nvr"]
ProbeKind = Literal["tcp", "icmp", "http", "ssh_banner", "dns"]
Expect = Literal["allow", "deny", "resolves", "sinkholed", "nxdomain"]

ROUTER_TARGETS = frozenset({"router_mgmt", "router_lan", "router_wan"})
REACH_EXPECT = frozenset({"allow", "deny"})
DNS_EXPECT = frozenset({"resolves", "sinkholed", "nxdomain"})

# First character can't be "-": dig (and most tools) would parse it as an option (-f, -x, ...).
ProbeId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")]
DnsName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.-]{0,252}$")]
Port = Annotated[int, Field(strict=True, ge=1, le=65535)]
# Free text for humans only (never reaches argv); still no control characters.
Description = Annotated[str, StringConstraints(max_length=200, pattern=r"^[^\x00-\x1f\x7f]*$")]


class Probe(BaseModel):
    """One reachability test, as written by the planner (or by invariants.py)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: ProbeId
    source: NodeName
    kind: ProbeKind
    target: Target
    port: Port | None = None
    query: DnsName | None = None
    expect: Expect
    description: Description = ""

    @model_validator(mode="after")
    def _consistent(self) -> Probe:
        if self.kind in ("tcp", "http"):
            if self.port is None:
                raise ValueError(f"{self.kind} probe needs a port")
        elif self.port is not None:
            raise ValueError(f"{self.kind} probe takes no port (ssh_banner is always 22)")

        if self.kind == "dns":
            if self.query is None:
                raise ValueError("dns probe needs a query")
            if self.expect not in DNS_EXPECT:
                raise ValueError(f"dns probe expect must be one of {sorted(DNS_EXPECT)}")
            if self.target not in ROUTER_TARGETS:
                raise ValueError("dns probe target must be a router interface (it is the resolver)")
        else:
            if self.query is not None:
                raise ValueError(f"{self.kind} probe takes no query")
            if self.expect not in REACH_EXPECT:
                raise ValueError(f"{self.kind} probe expect must be allow or deny")

        if self.source == self.target:
            raise ValueError("source and target must differ")
        return self

    @property
    def service_port(self) -> int | None:
        """The L4 port this probe exercises (ssh_banner is always 22)."""
        return 22 if self.kind == "ssh_banner" else self.port


class TestPlan(BaseModel):
    __test__ = False  # not a pytest test class

    model_config = ConfigDict(extra="forbid")

    probes: list[Probe] = Field(max_length=20)

    @model_validator(mode="after")
    def _unique_ids(self) -> TestPlan:
        ids = [p.id for p in self.probes]
        if len(ids) != len(set(ids)):
            raise ValueError("probe ids must be unique")
        return self


Origin = Literal["plan", "invariant", "containment"]


@dataclass(frozen=True)
class ResolvedProbe:
    """A probe bound to concrete addresses of one twin. Built only by trusted code:
    from a validated Probe (topology.resolve) or directly in invariants.py for containment checks,
    which need targets outside the model's enum (1.1.1.1, the bridge gateway .1)."""

    id: str
    origin: Origin
    source: str
    kind: str  # tcp | icmp | http | ssh_banner | dns
    ip: str  # target IP; for dns, the resolver IP
    target_label: str
    expect: str  # Expect, plus "no_answer" for containment DNS
    port: int | None = None
    query: str | None = None
    description: str = ""


class ProbeResult(BaseModel):
    """Raw evidence for one probe. Pass/fail is computed by code, never by the LLM."""

    id: str
    origin: Origin
    source: str
    kind: str
    target: str
    port: int | None = None
    query: str | None = None
    expect: str
    actual: str
    passed: bool
    detail: str = ""
    argv: list[str] = []
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    description: str = ""


class AttemptResult(BaseModel):
    attempt_id: str
    stage_failed: Literal["static", "nft_check", "nft_load", "twin", None] = None
    nft_output: str = ""
    probes: list[ProbeResult] = []  # plan + invariants
    containment: list[ProbeResult] = []  # CC-1..3
    passed: bool = False
    failures: list[str] = []  # human/LLM-readable feedback lines
    duration_s: float = 0.0
    teardown_clean: bool = False
