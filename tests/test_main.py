"""API end-to-end with a scripted LLM and executor: create → plan → edit → execute → SSE → approve → download."""

import time

import pytest
from fastapi.testclient import TestClient

from app import main
from tests.fake_llm import j
from tests.test_agent import FIXED, LOCKOUT, S1_PLAN, make_deps


def client_for(tmp_path, replies, verdicts, rate_limit=6):
    deps, fake = make_deps(tmp_path, replies, verdicts)
    app = main.create_app(lambda: main.Runtime(deps, max_concurrent=2, rate_limit_per_hour=rate_limit),
                          background=False)
    deps.docker_client = type("D", (), {"ping": lambda self: True})()
    return TestClient(app), deps, fake


def wait_status(c, run_id, want, timeout=5):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        st = c.get(f"/api/runs/{run_id}").json()
        if st["status"] in want:
            return st
        time.sleep(0.05)
    raise AssertionError(f"run stuck in {st['status']}")


def test_full_flow_s1(tmp_path):
    c, deps, fake = client_for(tmp_path, [j(S1_PLAN), j(LOCKOUT), j(FIXED), "Summary."], verdicts=[False, True])
    with c:
        assert c.get("/").status_code == 200
        h = c.get("/api/health").json()
        assert h["model"] == "glm-5.3" and h["docker"] is True
        assert [s["id"] for s in c.get("/api/scenarios").json()["scenarios"]] == ["S1", "S2", "S3"]

        run_id = c.post("/api/runs", json={"request": "Drop ALL inbound traffic.", "scenario": "S1"}).json()["run_id"]
        st = wait_status(c, run_id, {"plan_ready"})
        assert [p["id"] for p in st["plan"]] == ["admin-ssh-blocked", "staff-ssh-blocked", "wan-ping-blocked"]
        assert st["removed"] == [{"id": "admin-ssh-blocked", "reason": "conflicts with INV-1"}]
        assert st["conflicts"] == ["INV-1"] and st["invariants"][0]["code"] == "INV-1"

        # approve/download are refused before the run passed
        assert c.post(f"/api/runs/{run_id}/approve").status_code == 409
        assert c.get(f"/api/runs/{run_id}/export.nft").status_code == 403

        # user edits the plan: drops one probe
        edited = {"probes": [p for p in st["plan"] if p["id"] != "wan-ping-blocked"]}
        assert c.put(f"/api/runs/{run_id}/plan", json=edited).status_code == 200
        assert c.post(f"/api/runs/{run_id}/execute").status_code == 202
        assert c.post(f"/api/runs/{run_id}/execute").status_code == 409  # double click
        assert c.put(f"/api/runs/{run_id}/plan", json=edited).status_code == 409  # locked once started

        st = wait_status(c, run_id, {"passed", "failed", "error"})
        assert st["status"] == "passed"
        assert [a["label"] for a in st["attempts"]] == ["#1 ✗ INV-1 lockout", "#2 ✓"]
        assert deps.execute.calls[0]["plan_ids"] == ["staff-ssh-blocked"]  # edit applied, conflict stripped
        assert st["result"]["conflict_message"].startswith("Your request conflicted with INV-1")
        assert st["attempts"][0]["plan"][1]["probe_id"] == "INV-1"

        # SSE replays the whole history and ends at "final"
        with c.stream("GET", f"/api/runs/{run_id}/events") as s:
            names = [line.split(": ", 1)[1] for line in s.iter_lines() if line.startswith("event:")]
        assert names[0] == "planning" and "plan_ready" in names and names[-1] == "final"
        assert names.count("generating") == 2

        # resume after a reconnect
        with c.stream("GET", f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "1"}) as s:
            ids = [int(line[4:]) for line in s.iter_lines() if line.startswith("id: ")]
        assert ids[0] == 2

        assert c.post(f"/api/runs/{run_id}/approve").json()["approved"] is True
        nft = c.get(f"/api/runs/{run_id}/export.nft")
        assert nft.status_code == 200 and "attachment" in nft.headers["content-disposition"]
        import re
        assert "flush ruleset" in nft.text and re.search(r'define IF_MGMT\s+= "CHANGE_ME"', nft.text)
        assert "export · run" in nft.content.decode("utf-8")
        assert FIXED["ruleset"].strip() in nft.text
        md = c.get(f"/api/runs/{run_id}/report.md").text
        assert "INV-1" in md and "Caught on the twin" in md and "| ✅ | `INV-1`" in md


def test_rate_limit_per_ip(tmp_path):
    c, _, _ = client_for(tmp_path, [j(S1_PLAN)] * 3, verdicts=[], rate_limit=2)
    with c:
        assert c.post("/api/runs", json={"request": "one"}).status_code == 201
        assert c.post("/api/runs", json={"request": "two"}).status_code == 201
        r = c.post("/api/runs", json={"request": "three"})
        assert r.status_code == 429 and "rate limit" in r.json()["detail"]


@pytest.mark.parametrize("body", [
    {"request": "x"},                                   # too short
    {"request": "a" * 601},                             # too long
    {"request": "drop all\x1b[2J"},                     # control chars
    {"request": "ok request", "extra": 1},              # unknown field
    {"request": "ok request", "scenario": "S9"},        # unknown scenario
    {"request": "ok request", "baseline": 'include "/etc/shadow"'},  # baseline is validated too
])
def test_create_run_validation(tmp_path, body):
    c, _, _ = client_for(tmp_path, [], verdicts=[])
    with c:
        assert c.post("/api/runs", json=body).status_code == 422


def test_plan_put_rejects_injection(tmp_path):
    c, _, _ = client_for(tmp_path, [j(S1_PLAN)], verdicts=[])
    with c:
        run_id = c.post("/api/runs", json={"request": "ok request"}).json()["run_id"]
        wait_status(c, run_id, {"plan_ready"})
        bad = {"probes": [{"id": "x; rm -rf /", "source": "admin", "kind": "tcp", "target": "nvr", "port": 22,
                           "expect": "deny"}]}
        assert c.put(f"/api/runs/{run_id}/plan", json=bad).status_code == 422


def test_planning_llm_failure_surfaces_as_error(tmp_path):
    c, _, _ = client_for(tmp_path, ["not json", "still not json"], verdicts=[])
    with c:
        run_id = c.post("/api/runs", json={"request": "ok request"}).json()["run_id"]
        st = wait_status(c, run_id, {"error"})
        assert "repair" in st["error"]
        with c.stream("GET", f"/api/runs/{run_id}/events") as s:
            names = [line.split(": ", 1)[1] for line in s.iter_lines() if line.startswith("event:")]
        assert names[-1] == "error"


def test_unknown_run_404(tmp_path):
    c, _, _ = client_for(tmp_path, [], verdicts=[])
    with c:
        assert c.get("/api/runs/000000000000").status_code == 404
        assert c.get("/api/runs/000000000000/events").status_code == 404


def test_ui_never_uses_innerhtml():
    js = (main.STATIC / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js.replace("never innerHTML", "") and "insertAdjacentHTML" not in js
