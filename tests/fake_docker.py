"""A minimal in-memory stand-in for docker.DockerClient, enough to exercise topology/executor lifecycles."""

from __future__ import annotations

from types import SimpleNamespace


class FakeContainer:
    def __init__(self, client, name, labels, networks, **kw):
        self.client, self.name, self.labels, self.kw = client, name, labels, kw
        self.ips = dict(networks)  # netname -> ip
        self.status = "created"
        self.attrs = {"Config": {"Env": ["PATH=/usr/sbin:/usr/bin:/sbin:/bin"]}}

    def start(self):
        self.status = "running"

    def reload(self):
        pass

    def logs(self, tail=20):
        return b""

    def remove(self, force=False):
        self.client._containers.pop(self.name, None)

    def exec_run(self, argv, privileged=False, demux=False):
        return self.client.exec_handler(self, argv, privileged, demux)


class FakeNetwork:
    def __init__(self, client, name, labels, ipam, **kw):
        self.client, self.name, self.kw = client, name, kw
        self.attrs = {"Labels": labels, "IPAM": {"Config": [{"Subnet": ipam["Config"][0]["Subnet"]}]}}

    def connect(self, container, ipv4_address=None):
        container.ips[self.name] = ipv4_address

    def remove(self):
        self.client._networks.pop(self.name, None)


def _match(labels, filters):
    if not filters:
        return True
    k, _, v = filters["label"].partition("=")
    return labels.get(k) == v


class FakeClient:
    def __init__(self, exec_handler=None, fail_create_of: str | None = None):
        self._containers: dict[str, FakeContainer] = {}
        self._networks: dict[str, FakeNetwork] = {}
        self.exec_handler = exec_handler or default_exec
        self.fail_create_of = fail_create_of
        self.api = SimpleNamespace(create_endpoint_config=lambda ipv4_address=None: ipv4_address)
        self.containers = SimpleNamespace(create=self._create_container, list=self._list_containers)
        self.networks = SimpleNamespace(create=self._create_network, list=self._list_networks)

    def _create_network(self, name, labels=None, ipam=None, **kw):
        n = FakeNetwork(self, name, labels or {}, ipam, **kw)
        self._networks[name] = n
        return n

    def _create_container(self, image, name, labels=None, network=None, networking_config=None, **kw):
        if self.fail_create_of and name.endswith(self.fail_create_of):
            raise RuntimeError(f"simulated create failure for {name}")
        c = FakeContainer(self, name, labels or {}, {network: networking_config[network]}, image=image, **kw)
        self._containers[name] = c
        return c

    def _list_containers(self, all=False, filters=None):
        return [c for c in self._containers.values() if _match(c.labels, filters)]

    def _list_networks(self, filters=None):
        return [n for n in self._networks.values() if _match(n.attrs["Labels"], filters)]


def default_exec(container, argv, privileged, demux):
    """Router answers setup commands; probes 'fail' like a drop (exit 1)."""
    if argv[:2] == ["ip", "-o"]:
        lines = [f"{i + 2}: eth{i}    inet {ip}/24 scope global eth{i}" for i, ip in enumerate(container.ips.values())]
        return 0, "\n".join(lines).encode()
    if argv[0] in ("pidof", "nft") or argv[:2] == ["ip", "route"]:
        return (0, (b"", b"")) if demux else (0, b"")
    return 1, (b"", b"")
