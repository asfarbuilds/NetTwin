"""The whole agent loop with a scripted LLM and a scripted executor (no Docker)."""

from ipaddress import IPv4Network

from pydantic import SecretStr

from app import agent
from app.config import Settings
from app.llm import LLM
from app.schemas import AttemptResult, ProbeResult, TestPlan
from app.store import Store
from app.twin.executor import validate_ruleset
from app.twin.topology import SubnetPool
from tests.fake_llm import FakeOpenAI, j

S1_PLAN = {"probes": [
    {"id": "admin-ssh-blocked", "source": "admin", "kind": "ssh_banner", "target": "router_mgmt", "expect": "deny"},
    {"id": "staff-ssh-blocked", "source": "staff", "kind": "ssh_banner", "target": "router_lan", "expect": "deny"},
    {"id": "wan-ping-blocked", "source": "attacker", "kind": "icmp", "target": "router_wan", "expect": "deny"},
]}
LOCKOUT = {"ruleset": "table inet filter {\n chain input { type filter hook input priority filter; policy drop; }\n}\n",
           "rationale": "Drops everything inbound, as requested."}
FIXED = {"ruleset": "table inet filter {\n chain input { type filter hook input priority filter; policy drop;\n"
                    "  iifname $IF_MGMT tcp dport 22 accept\n }\n}\n",
         "rationale": "Drops all inbound except management SSH; dropping it would lock the admin out."}


def pr(id, origin, passed, actual="deny", expect="deny"):
    return ProbeResult(id=id, origin=origin, source="admin", kind="ssh_banner", target="router_mgmt",
                       expect=expect, actual=actual, passed=passed, stdout="" if passed else "")


def scripted_execute(verdicts):
    """Returns a fake executor.run_attempt that plays back one verdict per attempt and records its inputs."""
    calls = []

    def execute(client, pool, data_dir, attempt_id, ruleset, plan, emit=None, timeout_s=90):
        calls.append({"ruleset": ruleset, "plan_ids": [p.id for p in plan.probes]})
        inv_ok = verdicts[len(calls) - 1]
        inv = pr("INV-1", "invariant", inv_ok, actual="allow" if inv_ok else "deny", expect="allow")
        res = AttemptResult(attempt_id=attempt_id, probes=[pr("staff-ssh-blocked", "plan", True), inv],
                            containment=[pr("CC-3", "containment", True)], teardown_clean=True)
        res.passed = inv_ok
        res.failures = [] if inv_ok else ["INVARIANT VIOLATION INV-1: admin lost SSH to router_mgmt "
                                          "(expected allow, got deny)"]
        return res

    execute.calls = calls
    return execute


def make_deps(tmp_path, replies, verdicts, max_iter=4):
    settings = Settings(vultr_model="glm-5.3", vultr_inference_api_key=SecretStr("t"), nettwin_data_dir=tmp_path,
                        nettwin_max_iterations=max_iter, _env_file=None)
    fake = FakeOpenAI(replies)
    deps = agent.Deps(settings=settings, llm=LLM(settings, client=fake), store=Store(tmp_path / "t.db"),
                      docker_client=None, pool=SubnetPool(IPv4Network("10.231.0.0/16")),
                      execute=scripted_execute(verdicts))
    return deps, fake


def test_s1_containment_moment_end_to_end(tmp_path):
    deps, fake = make_deps(tmp_path, [j(S1_PLAN), j(LOCKOUT), j(FIXED), "Summary text."], verdicts=[False, True])
    run_id, plan = agent.start_run(deps, "Drop ALL inbound traffic to the router. No exceptions.", scenario="S1")
    res = agent.run_loop(deps, run_id, "Drop ALL inbound...", agent.DEFAULT_BASELINE, plan)

    assert res.passed
    assert [a.label for a in res.attempts] == ["#1 ✗ INV-1 lockout", "#2 ✓"]
    assert res.conflicts == ["INV-1"] and res.caught_invariants == ["INV-1"]
    assert res.conflict_message == "Your request conflicted with INV-1; NetTwin preserved management access."
    assert ("admin-ssh-blocked", "conflicts with INV-1") in res.removed_from_plan
    # the conflicting probe never reaches the executor; the invariant is appended there by final_probes
    assert "admin-ssh-blocked" not in deps.execute.calls[0]["plan_ids"]
    assert res.final_ruleset == FIXED["ruleset"]
    assert any(d.op == "+" and "IF_MGMT" in d.text for d in res.diff)
    assert res.summary == "Summary text."

    # the generator never saw the invariant up front, but did get the violation as feedback on retry
    first_gen, second_gen = fake.sent[1], fake.sent[2]
    assert "INV-1" not in first_gen[-1]["content"]
    assert "admin-ssh-blocked" not in first_gen[-1]["content"]  # conflicting probe never shown to the generator
    assert "INVARIANT VIOLATION INV-1" in second_gen[-1]["content"]
    assert "YOUR PREVIOUS RULESET" in second_gen[-1]["content"]

    stored = deps.store.get_run(run_id)
    assert stored["status"] == "passed" and len(stored["attempts"]) == 2
    assert [a["label"] for a in stored["attempts"]] == ["#1 ✗ INV-1 lockout", "#2 ✓"]
    assert {p["probe_id"] for p in stored["attempts"][0]["probes"]} == {"staff-ssh-blocked", "INV-1", "CC-3"}


def test_caught_lockout_without_request_conflict_has_honest_message(tmp_path):
    plan = {"probes": [{"id": "wan-ui", "source": "attacker", "kind": "http", "target": "router_wan", "port": 8443,
                        "expect": "allow"}]}
    deps, _ = make_deps(tmp_path, [j(plan), j(LOCKOUT), j(FIXED), "ok"], verdicts=[False, True])
    run_id, p = agent.start_run(deps, "Expose the NVR")
    res = agent.run_loop(deps, run_id, "Expose the NVR", agent.DEFAULT_BASELINE, p)
    assert res.conflicts == [] and res.caught_invariants == ["INV-1"]
    assert res.conflict_message.startswith("An earlier attempt violated INV-1; NetTwin caught it on the twin")


def test_gives_up_after_max_iterations(tmp_path):
    deps, _ = make_deps(tmp_path, [j(S1_PLAN)] + [j(LOCKOUT)] * 3 + ["sum"], verdicts=[False] * 3, max_iter=3)
    run_id, plan = agent.start_run(deps, "x")
    res = agent.run_loop(deps, run_id, "x", agent.DEFAULT_BASELINE, plan)
    assert not res.passed and len(res.attempts) == 3
    assert res.conflict_message == "" and res.final_ruleset == "" and res.diff == []
    assert deps.store.get_run(run_id)["status"] == "failed"


def test_llm_failure_in_one_attempt_is_recorded_and_retried(tmp_path):
    deps, _ = make_deps(tmp_path, [j(S1_PLAN), "nonsense", "still nonsense", j(FIXED), "sum"], verdicts=[True])
    run_id, plan = agent.start_run(deps, "x")
    res = agent.run_loop(deps, run_id, "x", agent.DEFAULT_BASELINE, plan)
    assert [a.label for a in res.attempts] == ["#1 ✗ LLM error", "#2 ✓"] and res.passed


def test_summary_falls_back_if_llm_fails(tmp_path):
    import httpx2
    import openai

    resp = httpx2.Response(400, request=httpx2.Request("POST", "https://x/v1/chat/completions"))
    summary_error = openai.APIStatusError("bad request", response=resp, body=None)
    deps, _ = make_deps(tmp_path, [j(S1_PLAN), j(FIXED), summary_error], verdicts=[True])
    run_id, plan = agent.start_run(deps, "x")
    res = agent.run_loop(deps, run_id, "x", agent.DEFAULT_BASELINE, plan)
    assert res.passed and res.summary.startswith("PASSED") and "#1 ✓" in res.summary


def test_diff_ignores_comment_only_lines():
    base = "# header comment\ntable inet filter {\n  iifname $IF_LAN accept\n}\n"
    final = "table inet filter {\n  # new comment\n  iifname $IF_MGMT accept\n}\n"
    ops = [(d.op, d.text.strip()) for d in agent.unified_diff(base, final) if d.op in "+-"]
    assert ops == [("-", "iifname $IF_LAN accept"), ("+", "iifname $IF_MGMT accept")]


def test_plan_failure_marks_run_error(tmp_path):
    import pytest

    from app.llm import LLMError

    deps, _ = make_deps(tmp_path, ["no json", "still no json"], verdicts=[])
    with pytest.raises(LLMError):
        agent.start_run(deps, "x")
    (row,) = deps.store._db.execute("SELECT status, error FROM runs").fetchall()
    assert row["status"] == "error" and "repair" in row["error"]


def test_default_baseline_passes_static_validation():
    validate_ruleset(agent.DEFAULT_BASELINE)


def test_plan_is_rechecked_after_user_edit(tmp_path):
    deps, _ = make_deps(tmp_path, [j(FIXED), "sum"], verdicts=[True])
    deps.store.create_run("r1", "x", "b")
    edited = TestPlan.model_validate({"probes": [
        {"id": "user-added", "source": "admin", "kind": "tcp", "target": "router_mgmt", "port": 22, "expect": "deny"}]})
    res = agent.run_loop(deps, "r1", "x", agent.DEFAULT_BASELINE, edited)
    assert res.removed_from_plan == [("user-added", "conflicts with INV-1")]
    assert deps.execute.calls[0]["plan_ids"] == []  # stripped before generation and execution
