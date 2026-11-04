"""The NetTwin agent loop (CLAUDE.md §6):

    PLAN -> REVIEW -> GENERATE -> STATIC VALIDATION -> EXECUTE on a fresh twin -> EVALUATE -> RETRY (max 4) -> RESULT

The LLM writes the test plan and the ruleset. Code decides pass/fail. Invariants are an independent gate the
generator is never shown up front; it only learns about one when a violation is fed back after a failed attempt.

CLI:  python -m app.agent run S1 [--model glm-5.3] [-v]      python -m app.agent models
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings, run_startup_checks
from app.llm import LLM, LLMError
from app.scenarios import BY_ID
from app.schemas import AttemptRecord, AttemptResult, DiffLine, RulesetProposal, RunResult, TestPlan
from app.store import Store
from app.twin import executor, invariants
from app.twin.topology import NODES, SubnetPool

log = logging.getLogger(__name__)

DEFAULT_BASELINE = (Path(__file__).parent / "baselines" / "default.nft").read_text(encoding="utf-8")
Emit = Callable[[str, dict], None]

TOPOLOGY = f"""\
Networks (each has its own router interface):
- mgmt: management network. Router interface $IF_MGMT, subnet $NET_MGMT, router address $ROUTER_MGMT.
- lan:  office LAN. Router interface $IF_LAN, subnet $NET_LAN, router address $ROUTER_LAN.
- wan:  the internet side. Router interface $IF_WAN, subnet $NET_WAN, router address $ROUTER_WAN (the public IP).
Hosts (name: network, variable, role):
- admin: mgmt, $ADMIN, the network engineer's management workstation.
- staff: lan, $STAFF, a normal office user.
- hr: lan, $HR, an HR user.
- nvr: lan, $NVR, a network video recorder with a web UI on TCP 8443 (HTTP).
- attacker: wan, $ATTACKER, an arbitrary host on the internet.
The router runs SSH on 22 and a DNS resolver on 53 (UDP and TCP) for internal clients; it is the default gateway
for every host. Probe sources: {", ".join(NODES)}. Probe targets: router_mgmt, router_lan, router_wan, or a host."""


@dataclass
class Deps:
    settings: Settings
    llm: LLM
    store: Store
    docker_client: object
    pool: SubnetPool
    execute: Callable[..., AttemptResult] = executor.run_attempt  # swapped in tests


def _no_emit(_e: str, _d: dict) -> None:
    pass


# ---------------------------------------------------------------- steps

def make_plan(llm: LLM, request: str, baseline: str) -> TestPlan:
    plan, _raw = llm.json_call("plan", TestPlan, label="plan", request=request, topology=TOPOLOGY, baseline=baseline)
    return plan


def generate(llm: LLM, request: str, baseline: str, plan: TestPlan, feedback: str, n: int) -> RulesetProposal:
    proposal, _raw = llm.json_call(
        "generate", RulesetProposal, label=f"generate#{n}",
        request=request, topology=TOPOLOGY, baseline=baseline,
        plan=json.dumps([p.model_dump(exclude_none=True) for p in plan.probes], indent=1),
        feedback=feedback,
    )
    return proposal


def feedback_for(prev: AttemptRecord) -> str:
    """What the generator sees on retry: its previous ruleset plus the failures, with raw evidence."""
    lines = [f"FEEDBACK FROM ATTEMPT #{prev.n} (it FAILED on the twin):"]
    if prev.error:
        lines.append(f"- {prev.error}")
    if prev.result:
        lines += [f"- {f}" for f in prev.result.failures]
        for r in prev.result.probes:
            if not r.passed and (r.stdout or r.stderr):
                lines.append(f"  raw evidence {r.id}: {(r.stdout or r.stderr).strip()[:300]}")
    if prev.proposal:
        lines += ["", "YOUR PREVIOUS RULESET:", "```", prev.proposal.ruleset.strip(), "```"]
    return "\n".join(lines) + "\n"


def violated_invariants(res: AttemptResult | None) -> list[str]:
    return [r.id for r in (res.probes if res else []) if r.origin == "invariant" and not r.passed]


def timeline_label(rec: AttemptRecord) -> str:
    res = rec.result
    if res and res.passed:
        return f"#{rec.n} ✓"
    if not res:
        return f"#{rec.n} ✗ LLM error"
    if res.stage_failed:
        return f"#{rec.n} ✗ {res.stage_failed.replace('_', ' ')}"
    if inv := violated_invariants(res):
        shorts = [f"{c} {invariants.INVARIANTS_BY_CODE[c].short}" for c in inv]
        return f"#{rec.n} ✗ {', '.join(shorts)}"
    failed = sum(not r.passed for r in res.probes + res.containment)
    return f"#{rec.n} ✗ {failed} probe{'s' if failed != 1 else ''} failed"


def conflict_message(conflicts: list[str], caught: list[str]) -> str:
    """Deterministic, only for a passed run. The LLM never decides whether to show it.
    - The request itself contradicted an invariant (found by the plan check) -> "Your request conflicted ...".
    - Otherwise an attempt broke an invariant on the twin and a later one fixed it -> "caught on the twin"."""
    def preserved(codes: list[str]) -> str:
        return " and ".join(invariants.INVARIANTS_BY_CODE[c].preserves for c in codes)

    if conflicts:
        return f"Your request conflicted with {', '.join(conflicts)}; NetTwin preserved {preserved(conflicts)}."
    if caught:
        return (f"An earlier attempt violated {', '.join(caught)}; NetTwin caught it on the twin and "
                f"preserved {preserved(caught)}.")
    return ""


def unified_diff(baseline: str, final: str) -> list[DiffLine]:
    out = []
    for line in difflib.unified_diff(baseline.splitlines(), final.splitlines(), "baseline.nft", "proposed.nft",
                                     lineterm=""):
        if line.startswith(("---", "+++")):
            continue
        op = "@" if line.startswith("@@") else line[:1] if line[:1] in "+- " else " "
        out.append(DiffLine(op=op, text=line if op == "@" else line[1:]))
    return out


def evidence_text(res: AttemptResult) -> str:
    return "\n".join(
        f"{'PASS' if r.passed else 'FAIL'} {r.id}: {r.source} {r.kind} {r.target}"
        f"{':' + str(r.port) if r.port else ''}{' ' + r.query if r.query else ''}"
        f" expected {r.expect}, got {r.actual} {r.detail}".rstrip()
        for r in res.probes + res.containment
    )


# ---------------------------------------------------------------- the loop

def run_loop(deps: Deps, run_id: str, request: str, baseline: str, plan: TestPlan,
             emit: Emit = _no_emit) -> RunResult:
    """GENERATE -> VALIDATE -> EXECUTE -> EVALUATE, retrying on a brand-new twin each time."""
    check = invariants.check_plan(plan)  # run again on the (possibly user-edited) plan
    result = RunResult(run_id=run_id, request=request, model=deps.llm.model, plan=plan,
                       removed_from_plan=[(p.id, why) for p, why in check.removed], conflicts=list(check.conflicts))
    # Generator and executor only ever see the checked plan: never ask the model to satisfy a probe that
    # contradicts an invariant.
    plan = TestPlan(probes=check.kept)
    deps.store.update_run(run_id, status="running", plan_json=plan.model_dump_json())
    feedback = ""

    for n in range(1, deps.settings.nettwin_max_iterations + 1):
        rec = AttemptRecord(n=n)
        attempt_id = secrets.token_hex(4)
        emit("generating", {"attempt": n})
        try:
            rec.proposal = generate(deps.llm, request, baseline, plan, feedback, n)
        except LLMError as e:
            rec.error = f"LLM ERROR: {e}"
        if rec.proposal:
            rec.result = deps.execute(
                deps.docker_client, deps.pool, deps.settings.nettwin_data_dir, attempt_id,
                rec.proposal.ruleset, plan, emit=emit, timeout_s=deps.settings.nettwin_run_timeout_s,
            )
        rec.label = timeline_label(rec)
        result.attempts.append(rec)
        deps.store.add_attempt(run_id, rec, attempt_id)
        log.info("run %s %s", run_id, rec.label)

        for code in violated_invariants(rec.result):
            if code not in result.caught_invariants:
                result.caught_invariants.append(code)
        if rec.result and rec.result.passed:
            result.passed = True
            break
        feedback = feedback_for(rec)

    final = next((a for a in reversed(result.attempts) if a.result and a.result.passed), None)
    if final:
        result.final_ruleset, result.rationale = final.proposal.ruleset, final.proposal.rationale
        result.diff = unified_diff(baseline, final.proposal.ruleset)
        result.conflict_message = conflict_message(result.conflicts, result.caught_invariants)
    result.summary = summarize(deps.llm, result)
    result.llm_usage = {"calls": deps.llm.usage.calls, "prompt_tokens": deps.llm.usage.prompt_tokens,
                        "completion_tokens": deps.llm.usage.completion_tokens}
    deps.store.update_run(run_id, status="passed" if result.passed else "failed", result_json=result.model_dump_json())
    emit("final", {"run_id": run_id, "passed": result.passed, "timeline": [a.label for a in result.attempts],
                   "conflict_message": result.conflict_message})
    return result


def summarize(llm: LLM, result: RunResult) -> str:
    """Written AFTER the verdict, from the evidence only. Falls back to a plain sentence if the LLM fails."""
    timeline = " → ".join(a.label for a in result.attempts)
    last = next((a for a in reversed(result.attempts) if a.result), None)
    verdict = "PASSED: every probe, invariant and containment check passed" if result.passed else "FAILED"
    try:
        return llm.text_call(
            "summarize", label="summarize", request=result.request, verdict=verdict, timeline=timeline,
            conflict=result.conflict_message or "none", rationale=result.rationale or "n/a",
            evidence=evidence_text(last.result) if last else "no attempt reached the twin",
        )[:2000]
    except LLMError:
        return f"{verdict}. Attempts: {timeline}. {result.conflict_message}".strip()


def start_run(deps: Deps, request: str, baseline: str | None = None, scenario: str | None = None,
              emit: Emit = _no_emit, client_ip: str | None = None) -> tuple[str, TestPlan]:
    """PLAN step: create the run and ask for a test plan. The UI then shows it for review."""
    run_id = secrets.token_hex(6)
    baseline = baseline or DEFAULT_BASELINE
    deps.store.create_run(run_id, request, baseline, scenario=scenario, client_ip=client_ip, model=deps.llm.model)
    emit("planning", {"run_id": run_id})
    plan = make_plan(deps.llm, request, baseline)
    deps.store.update_run(run_id, status="plan_ready", plan_json=plan.model_dump_json())
    emit("plan_ready", {"run_id": run_id, "probes": len(plan.probes)})
    return run_id, plan


# ---------------------------------------------------------------- CLI (auto-approves the plan)

def _print_result(res: RunResult) -> None:
    print(f"\nPlan: {len(res.plan.probes)} probes")
    for pid, why in res.removed_from_plan:
        print(f"  [removed] {pid}: {why}")
    print("Timeline: " + " → ".join(a.label for a in res.attempts))
    for a in res.attempts:
        if a.result and not a.result.passed:
            print(f"\n  {a.label}")
            for f in a.result.failures:
                print(f"    - {f.splitlines()[0][:150]}")
        elif a.error:
            print(f"\n  {a.label}: {a.error[:200]}")
    last = next((a for a in reversed(res.attempts) if a.result), None)
    if last and last.result.probes:
        print("\nFinal evidence:\n" + evidence_text(last.result))
    if res.diff:
        print("\nDiff (baseline → proposed):")
        for d in res.diff:
            print(f"  {d.text}" if d.op == "@" else f"  {d.op} {d.text}")
    if res.conflict_message:
        print(f"\n>>> {res.conflict_message}")
    print(f"\nSummary: {res.summary}")
    print(f"LLM: {res.model} on Vultr Serverless Inference · {res.llm_usage}")
    print(f"VERDICT: {'PASS' if res.passed else 'FAIL'}   (run {res.run_id})")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m app.agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a scenario end-to-end (plan is auto-approved)")
    r.add_argument("scenario", nargs="?", help="S1, S2 or S3")
    r.add_argument("--request", help="free-text change request instead of a scenario")
    r.add_argument("--model", help="override VULTR_MODEL (must be in /v1/models)")
    r.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("models", help="list models served to this API key")
    args = ap.parse_args(argv)

    settings = get_settings()
    if getattr(args, "model", None):
        settings = settings.model_copy(update={"vultr_model": args.model})
    llm = LLM(settings)
    if args.cmd == "models":
        print("\n".join(llm.list_models()))
        return 0

    if not (args.scenario or args.request):
        ap.error("give a scenario id or --request")
    request = args.request or BY_ID[args.scenario.upper()].request
    run_startup_checks(settings)
    llm.verify_model()

    import docker  # only needed for real runs

    deps = Deps(settings=settings, llm=llm, store=Store(settings.nettwin_data_dir / "nettwin.db"),
                docker_client=docker.from_env(), pool=SubnetPool(settings.nettwin_subnet_pool))
    emit = (lambda ev, d: print(f"[{ev}] {json.dumps(d, ensure_ascii=False)[:160]}")) if args.verbose else _no_emit
    print(f"Request: {request}")
    run_id, plan = start_run(deps, request, scenario=args.scenario, emit=emit)
    res = run_loop(deps, run_id, request, DEFAULT_BASELINE, plan, emit=emit)
    _print_result(res)
    return 0 if res.passed else 1


if __name__ == "__main__":
    sys.exit(main())
