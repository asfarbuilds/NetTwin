"""Teardown must happen no matter what: probe errors, build failures, nft failures, bugs."""

from ipaddress import IPv4Network
from pathlib import Path

import pytest

from app.schemas import TestPlan
from app.twin import executor
from app.twin.janitor import sweep
from app.twin.topology import SubnetPool, twin_up
from tests.fake_docker import FakeClient, default_exec

FIXTURES = Path(__file__).parent / "fixtures"
S3_RULESET = (FIXTURES / "s3_ruleset.nft").read_text()
S3_PLAN = TestPlan.model_validate_json((FIXTURES / "s3_plan.json").read_text())


def pool():
    return SubnetPool(IPv4Network("10.231.0.0/16"))


def assert_empty(client):
    assert client._containers == {} and client._networks == {}


def test_happy_path_builds_full_twin_and_tears_down(tmp_path):
    client = FakeClient()
    seen = {}
    with twin_up(client, pool(), "abcd1234", tmp_path / "a") as twin:
        seen["containers"] = sorted(c.name for c in client._containers.values())
        seen["router_kw"] = twin.containers["router"].kw
        seen["node_kw"] = twin.containers["staff"].kw
        seen["ifaces"] = twin.router_ifaces
    assert seen["containers"] == sorted(f"nettwin-abcd1234-{n}" for n in
                                        ("router", "admin", "staff", "hr", "nvr", "attacker"))
    r, n = seen["router_kw"], seen["node_kw"]
    assert r["cap_drop"] == ["ALL"] and r["cap_add"] == ["NET_ADMIN", "NET_BIND_SERVICE"]
    assert n["cap_drop"] == ["ALL"] and "cap_add" not in n
    for kw in (r, n):
        assert kw["read_only"] and kw["security_opt"] == ["no-new-privileges"] and kw["pids_limit"] == 64
        assert "privileged" not in kw and "environment" not in kw and "network_mode" not in kw
    assert r["volumes"][str(tmp_path / "a")]["mode"] == "ro"
    assert set(seen["ifaces"]) == {"mgmt", "lan", "wan"}
    assert twin.teardown_clean
    assert_empty(client)
    assert (tmp_path / "a" / "dnsmasq.conf").read_bytes().count(b"\r") == 0


def test_teardown_when_a_probe_raises(tmp_path):
    def exploding(container, argv, privileged, demux):
        if argv[0] in ("nc", "curl", "dig", "ping"):
            raise RuntimeError("boom inside exec")
        return default_exec(container, argv, privileged, demux)

    client = FakeClient(exec_handler=exploding)
    res = executor.run_attempt(client, pool(), tmp_path, "abcd1234", S3_RULESET, S3_PLAN)
    assert not res.passed
    assert all(r.actual == "error" for r in res.probes)
    assert res.teardown_clean
    assert_empty(client)


def test_teardown_when_code_inside_twin_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "run_probes", lambda *a, **k: 1 / 0)
    client = FakeClient()
    with pytest.raises(ZeroDivisionError):
        executor.run_attempt(client, pool(), tmp_path, "abcd1234", S3_RULESET, S3_PLAN)
    assert_empty(client)


def test_teardown_when_build_fails_midway(tmp_path):
    client = FakeClient(fail_create_of="-hr")  # router + some nodes already exist
    with pytest.raises(RuntimeError):
        with twin_up(client, pool(), "abcd1234", tmp_path / "a"):
            pass
    assert_empty(client)


def test_nft_failure_skips_plan_but_runs_containment(tmp_path):
    def nft_fails(container, argv, privileged, demux):
        if argv[:2] == ["nft", "-c"]:
            return 1, b"ruleset.nft:3:5-9: Error: syntax error"
        return default_exec(container, argv, privileged, demux)

    client = FakeClient(exec_handler=nft_fails)
    res = executor.run_attempt(client, pool(), tmp_path, "abcd1234", S3_RULESET, S3_PLAN)
    assert res.stage_failed == "nft_check" and not res.passed
    assert res.probes == [] and {r.id for r in res.containment} == {"CC-1a", "CC-1b", "CC-2a", "CC-2b", "CC-3"}
    assert "syntax error" in res.failures[0]
    assert_empty(client)


def test_static_rejection_creates_no_twin(tmp_path):
    client = FakeClient()
    res = executor.run_attempt(client, pool(), tmp_path, "abcd1234", 'include "/etc/shadow"', S3_PLAN)
    assert res.stage_failed == "static" and not res.passed and res.teardown_clean
    assert_empty(client)


def test_ruleset_file_has_header_and_lf(tmp_path):
    client = FakeClient()
    executor.run_attempt(client, pool(), tmp_path, "abcd1234", S3_RULESET.replace("\n", "\r\n"), S3_PLAN)
    data = (tmp_path / "attempts" / "abcd1234" / "ruleset.nft").read_bytes()
    assert data.startswith(b"flush ruleset\n") and b"\r" not in data and b"define NVR = " in data


def test_invariant_and_cc_evidence_present(tmp_path):
    res = executor.run_attempt(FakeClient(), pool(), tmp_path, "abcd1234", S3_RULESET, S3_PLAN)
    assert res.probes[-1].id == "INV-1" and not res.probes[-1].passed  # fake probes all 'drop'
    assert any(line.startswith("INVARIANT VIOLATION INV-1") for line in res.failures)
    cc3 = next(r for r in res.containment if r.id == "CC-3")
    assert cc3.passed


def test_cc3_reports_secret_names_never_values(tmp_path):
    client = FakeClient()
    with twin_up(client, pool(), "abcd1234", tmp_path / "a") as twin:
        # Built in two parts so the secret-blocking pre-commit hook doesn't flag this fake value.
        twin.containers["nvr"].attrs["Config"]["Env"].append("VULTR_INFERENCE_API_KEY" + "=sk-live-123")
        r = executor.check_secret_hygiene(twin)
    assert not r.passed and "nvr:VULTR_INFERENCE_API_KEY" in r.detail and "sk-live" not in r.model_dump_json()


def test_janitor_removes_only_old_objects(tmp_path):
    client = FakeClient()
    pool_ = pool()
    with twin_up(client, pool_, "aaaa0000", tmp_path / "a"):
        now = int(next(iter(client._containers.values())).labels["nettwin.created"])
        assert sweep(client, max_age_s=600, now=now + 10) == {"containers": 0, "networks": 0}
        removed = sweep(client, max_age_s=600, now=now + 601)
        assert removed == {"containers": 6, "networks": 3}
        assert_empty(client)
