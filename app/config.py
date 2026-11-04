"""Settings and startup checks. Fail loudly when the host can't safely run twins."""

from __future__ import annotations

import socket
import struct
import sys
from functools import lru_cache
from ipaddress import IPv4Network
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Later files win: /etc/nettwin/env (production) overrides a local .env.
    model_config = SettingsConfigDict(env_file=(".env", "/etc/nettwin/env"), extra="ignore")

    vultr_inference_api_key: SecretStr = SecretStr("")
    vultr_inference_base_url: str = "https://api.vultrinference.com/v1"
    vultr_model: str = ""
    nettwin_data_dir: Path = Path("/var/lib/nettwin")
    nettwin_subnet_pool: IPv4Network = IPv4Network("10.231.0.0/16")
    nettwin_max_concurrent_runs: int = 2
    nettwin_max_iterations: int = 4
    nettwin_run_timeout_s: int = 90
    nettwin_rate_limit_per_hour: int = 6

    @field_validator("vultr_inference_api_key", mode="before")
    @classmethod
    def _clean_key(cls, v):
        """Tolerate copy-paste debris in the env file: whitespace, CR from Windows editors, stray quotes."""
        if isinstance(v, str):
            return v.strip().strip("'\"").strip()
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


class StartupError(RuntimeError):
    """The host is not in a state where twins are safe or correct."""


PROC_ROUTE = Path("/proc/net/route")
BR_NF_CALL_IPTABLES = Path("/proc/sys/net/bridge/bridge-nf-call-iptables")


def _hex_ip(h: str) -> str:
    return socket.inet_ntoa(struct.pack("<L", int(h, 16)))


def parse_proc_route(text: str) -> list[tuple[str, IPv4Network]]:
    """Parse /proc/net/route into (iface, network). Pure Python: no subprocess."""
    routes = []
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 8:
            continue
        iface, dest, mask = cols[0], cols[1], cols[7]
        routes.append((iface, IPv4Network(f"{_hex_ip(dest)}/{_hex_ip(mask)}", strict=False)))
    return routes


def check_subnet_pool(pool: IPv4Network, routes: list[tuple[str, IPv4Network]]) -> None:
    """Vultr VPC also uses 10.x ranges, so the twin pool must not overlap any host route.

    Routes on our own twin bridges (nt*) and the default route are ignored.
    """
    clashes = [
        f"{net} dev {iface}"
        for iface, net in routes
        if net.prefixlen > 0 and not iface.startswith("nt") and net.overlaps(pool)
    ]
    if clashes:
        raise StartupError(f"NETTWIN_SUBNET_POOL {pool} overlaps host routes: {', '.join(clashes)}")


def check_br_netfilter(path: Path = BR_NF_CALL_IPTABLES) -> None:
    """With br_netfilter active, Docker's internal-network isolation rules can drop traffic that is
    bridged to the twin router, which breaks routing. The spike (docs/spike.md) passed only with it off."""
    if path.exists() and path.read_text().strip() == "1":
        raise StartupError(
            "net.bridge.bridge-nf-call-iptables=1 (br_netfilter loaded): twin routing is unverified in this "
            "state. Unload br_netfilter or set the sysctl to 0. See docs/spike.md."
        )


def run_startup_checks(settings: Settings) -> None:
    """Host checks. They only make sense on the Linux VM; skipped elsewhere (e.g. Windows dev)."""
    if not sys.platform.startswith("linux"):
        return
    check_subnet_pool(settings.nettwin_subnet_pool, parse_proc_route(PROC_ROUTE.read_text()))
    check_br_netfilter()
