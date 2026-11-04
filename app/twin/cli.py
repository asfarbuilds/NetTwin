"""Run one attempt by hand, without the LLM.

    python -m app.twin.cli run --ruleset tests/fixtures/s3_ruleset.nft --plan tests/fixtures/s3_plan.json
    python -m app.twin.cli janitor [--all]
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import sys
from pathlib import Path

import docker

from app.config import get_settings, run_startup_checks
from app.schemas import AttemptResult, ProbeResult, TestPlan
from app.twin import executor, invariants, janitor
from app.twin.topology import NODE_IMAGE, ROUTER_IMAGE, SubnetPool


def _fmt_probe(r: ProbeResult) -> str:
    what = f"{r.source} {r.kind} {r.target}"
    if r.port:
        what += f":{r.port}"
    if r.query:
        what += f" {r.query}"
    mark = "PASS" if r.passed else "FAIL"
    return f"  {mark}  {r.id:<16} {what:<44} expect {r.expect:<9} got {r.actual:<14} {r.detail[:40]}"


def print_report(res: AttemptResult, removed: list[tuple[str, str]]) -> None:
    print(f"\nAttempt {res.attempt_id}  ({res.duration_s}s)")
    for pid, reason in removed:
        print(f"  [removed from plan] {pid}: {reason}")
    if res.stage_failed:
        print(f"  stage failed: {res.stage_failed}\n{res.nft_output}")
    if res.probes:
        print("\nEvidence (plan + invariants):")
        for r in res.probes:
            print(_fmt_probe(r))
    print("\nContainment checks:")
    for r in res.containment:
        print(_fmt_probe(r))
    if res.failures:
        print("\nFeedback for the generator:")
        for line in res.failures:
            print(f"  - {line}")
    print(f"\nTeardown clean: {res.teardown_clean}")
    print(f"VERDICT: {'PASS' if res.passed else 'FAIL'}")


def cmd_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    run_startup_checks(settings)
    client = docker.from_env()
    for img in (ROUTER_IMAGE, NODE_IMAGE):
        client.images.get(img)  # raises ImageNotFound with a clear message

    plan = TestPlan.model_validate_json(Path(args.plan).read_text(encoding="utf-8"))
    check = invariants.check_plan(plan)
    removed = [(p.id, reason) for p, reason in check.removed]
    if check.conflicts:
        print(f"Plan conflicts with {', '.join(check.conflicts)}; conflicting probes were removed.")

    janitor.sweep(client)
    res = executor.run_attempt(
        client,
        SubnetPool(settings.nettwin_subnet_pool),
        Path(args.data_dir or settings.nettwin_data_dir),
        secrets.token_hex(4),
        Path(args.ruleset).read_text(encoding="utf-8"),
        plan,
        emit=(lambda ev, data: print(f"[{ev}] {json.dumps(data)[:160]}")) if args.verbose else executor._no_emit,
        timeout_s=settings.nettwin_run_timeout_s,
    )
    print_report(res, removed)
    if args.json:
        Path(args.json).write_text(res.model_dump_json(indent=2), encoding="utf-8")
    return 0 if res.passed else 1


def cmd_janitor(args: argparse.Namespace) -> int:
    removed = janitor.sweep(docker.from_env(), max_age_s=0 if args.all else janitor.MAX_AGE_S)
    print(f"removed {removed['containers']} containers, {removed['networks']} networks")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m app.twin.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="apply a ruleset to a fresh twin and run a plan")
    r.add_argument("--ruleset", required=True)
    r.add_argument("--plan", required=True)
    r.add_argument("--data-dir", help="override NETTWIN_DATA_DIR")
    r.add_argument("--json", help="also write the AttemptResult as JSON here")
    r.add_argument("-v", "--verbose", action="store_true", help="print live events")
    r.set_defaults(fn=cmd_run)
    j = sub.add_parser("janitor", help="remove orphaned twin objects")
    j.add_argument("--all", action="store_true", help="remove every nettwin object regardless of age")
    j.set_defaults(fn=cmd_janitor)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
