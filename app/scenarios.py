"""Seed scenarios (CLAUDE.md §8), from Ali's real incidents. Served by GET /api/scenarios."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Scenario:
    id: str
    priority: str
    title: str
    request: str
    story: str


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="S1",
        priority="P0",
        title="Lock down the router",
        request="Drop ALL inbound traffic to the router on every interface. No exceptions.",
        story="Taken literally this locks the admin out of the router. INV-1 catches the lockout on the twin, "
              "and NetTwin keeps management access. This is the containment moment.",
    ),
    Scenario(
        id="S2",
        priority="P0",
        title="Harden SSH after a brute-force attack",
        request="SSH to the router only from the admin network, rate-limit new SSH connections to 3 per minute, "
                "and drop SSH from LAN and WAN.",
        story="A production MikroTik router logged 58,622 failed SSH logins. This is the hardening that followed.",
    ),
    Scenario(
        id="S3",
        priority="P0",
        title="Expose the NVR to the internet",
        request="Expose the NVR web UI (nvr:8443) to the internet on WAN port 8443. Nothing else reachable from WAN.",
        story="A port forward for the camera recorder, without opening anything else on the WAN side.",
    ),
)
BY_ID = {s.id: s for s in SCENARIOS}


def as_dicts() -> list[dict]:
    return [asdict(s) for s in SCENARIOS]
