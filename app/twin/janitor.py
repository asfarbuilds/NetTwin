"""Remove orphaned twins: any container or network labelled nettwin=1 older than 10 minutes.
Runs at startup and every 5 minutes (CLAUDE.md §5). A normal attempt cleans up after itself in `finally`;
this catches crashes and kills."""

from __future__ import annotations

import logging
import threading
import time

import docker
import docker.errors

from app.twin.topology import LABEL

log = logging.getLogger(__name__)

MAX_AGE_S = 600
INTERVAL_S = 300


def _age_ok(labels: dict | None, now: float, max_age_s: float) -> bool:
    created = int((labels or {}).get(f"{LABEL}.created", "0") or 0)
    return now - created >= max_age_s


def sweep(client: docker.DockerClient, max_age_s: float = MAX_AGE_S, now: float | None = None) -> dict[str, int]:
    now = time.time() if now is None else now
    sel = {"label": f"{LABEL}=1"}
    removed = {"containers": 0, "networks": 0}
    for c in client.containers.list(all=True, filters=sel):
        if _age_ok(c.labels, now, max_age_s):
            try:
                c.remove(force=True)
                removed["containers"] += 1
            except docker.errors.APIError as e:
                log.warning("janitor: container %s: %s", c.name, e)
    for n in client.networks.list(filters=sel):  # after containers, so networks have no endpoints
        if _age_ok(n.attrs.get("Labels"), now, max_age_s):
            try:
                n.remove()
                removed["networks"] += 1
            except docker.errors.APIError as e:
                log.warning("janitor: network %s: %s", n.name, e)
    if any(removed.values()):
        log.info("janitor removed %(containers)d containers, %(networks)d networks", removed)
    return removed


def run_forever(client: docker.DockerClient, stop: threading.Event, interval_s: float = INTERVAL_S) -> None:
    while not stop.is_set():
        try:
            sweep(client)
        except Exception:
            log.exception("janitor sweep failed")
        stop.wait(interval_s)
