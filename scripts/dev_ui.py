"""Local UI preview with a scripted LLM and fake twins: no Docker, no Vultr key, no credits.

    uv run python scripts/dev_ui.py        then open http://127.0.0.1:8000

Every run replays the S1 story: plan with a conflicting probe, attempt #1 locks the admin out (INV-1),
attempt #2 passes. For UI work only; real evidence comes from the VM.
"""

import sys
import tempfile
import time
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main  # noqa: E402
from tests.fake_llm import j  # noqa: E402
from tests.test_agent import FIXED, LOCKOUT, S1_PLAN, make_deps  # noqa: E402

SUMMARY = ("The request asked to drop all inbound traffic. Attempt #1 did exactly that and locked the admin out, "
           "which INV-1 caught on the twin. Attempt #2 keeps a single exception, SSH from the admin to the "
           "management interface, and every probe and containment check passed.")


def runtime() -> main.Runtime:
    tmp = Path(tempfile.mkdtemp(prefix="nettwin-dev-"))
    deps, fake = make_deps(tmp, [], verdicts=[False, True] * 50)  # every run: #1 lockout, #2 passes
    fake.replies = [j(S1_PLAN), j(LOCKOUT), j(FIXED), SUMMARY] * 50
    slow = deps.execute

    def execute(*a, **kw):  # pretend twins take a few seconds, and emit probe events like the real executor
        emit = kw.get("emit") or (lambda *_: None)
        emit("twin_up", {"attempt_id": a[3], "subnets": {"mgmt": "10.231.0.0/24", "lan": "10.231.1.0/24",
                                                          "wan": "10.231.2.0/24"}})
        emit("nft_check", {"ok": True, "stage": None, "output": ""})
        res = slow(*a, **kw)
        for p in res.probes + res.containment:
            time.sleep(0.3)
            emit("probe", p.model_dump(include={"id", "origin", "expect", "actual", "passed", "detail"}))
        emit("teardown", {"attempt_id": a[3], "clean": True})
        emit("attempt_verdict", {"attempt_id": a[3], "passed": res.passed, "failures": res.failures})
        return res

    deps.execute = execute
    deps.execute.calls = slow.calls
    deps.docker_client = type("FakeDocker", (), {"ping": lambda self: True})()
    return main.Runtime(deps, max_concurrent=2, rate_limit_per_hour=1000)


if __name__ == "__main__":
    uvicorn.run(main.create_app(runtime, background=False), host="127.0.0.1", port=8000)
