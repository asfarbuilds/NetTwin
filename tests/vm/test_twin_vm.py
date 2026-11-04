"""Real twins. Only meaningful on the Vultr VM:  uv run pytest -m vm -v"""

import secrets
from pathlib import Path

import pytest

from app.config import get_settings, run_startup_checks
from app.schemas import TestPlan
from app.twin import executor
from app.twin.topology import LABEL, SubnetPool

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.vm

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture(scope="module")
def client():
    run_startup_checks(get_settings())
    return docker.from_env()


@pytest.fixture
def pool():
    return SubnetPool(get_settings().nettwin_subnet_pool)


def no_leftovers(client):
    sel = {"label": f"{LABEL}=1"}
    return not client.containers.list(all=True, filters=sel) and not client.networks.list(filters=sel)


def run(client, pool, tmp_path, ruleset_file, plan):
    return executor.run_attempt(
        client, pool, tmp_path, secrets.token_hex(4), (FIXTURES / ruleset_file).read_text(), plan, timeout_s=90
    )


def test_s3_reference_ruleset_passes(client, pool, tmp_path):
    plan = TestPlan.model_validate_json((FIXTURES / "s3_plan.json").read_text())
    res = run(client, pool, tmp_path, "s3_ruleset.nft", plan)
    failed = [f"{r.id}: expected {r.expect} got {r.actual} {r.detail}" for r in res.probes + res.containment
              if not r.passed]
    assert res.passed, "\n".join(failed + res.failures)
    assert res.teardown_clean and no_leftovers(client)
    assert res.duration_s < 60


def test_s1_literal_ruleset_trips_inv1_and_sandbox_holds(client, pool, tmp_path):
    res = run(client, pool, tmp_path, "s1_literal_ruleset.nft", TestPlan(probes=[]))
    assert not res.passed
    inv = next(r for r in res.probes if r.id == "INV-1")
    assert not inv.passed and inv.actual == "deny"
    assert res.failures[0].startswith("INVARIANT VIOLATION INV-1: admin lost SSH to router_mgmt")
    assert all(r.passed for r in res.containment), [r.id for r in res.containment if not r.passed]
    assert res.teardown_clean and no_leftovers(client)


def test_nft_syntax_error_is_evidence_not_crash(client, pool, tmp_path):
    res = executor.run_attempt(client, pool, tmp_path, secrets.token_hex(4),
                               "table inet filter { chain input { type filter hook input priority 0; policy dorp; } }",
                               TestPlan(probes=[]))
    assert res.stage_failed == "nft_check" and "Error" in res.nft_output
    assert all(r.passed for r in res.containment)
    assert res.teardown_clean and no_leftovers(client)
