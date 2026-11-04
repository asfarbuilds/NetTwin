"""Print what went wrong in stored runs: per attempt, the nft error, failing probes with raw output, and the ruleset.

    uv run python scripts/show_run.py <run_id> [<run_id> ...] [--rulesets]
"""

from __future__ import annotations

import argparse
import json
import sys

from app.config import get_settings
from app.store import Store


def show(store: Store, run_id: str, rulesets: bool) -> None:
    run = store.get_run(run_id)
    if not run:
        print(f"=== {run_id}: not found")
        return
    print(f"=== {run_id}  {run['model']}  {run['scenario'] or ''}  status={run['status']}")
    plan = json.loads(run["plan_json"] or '{"probes": []}')["probes"]
    print("plan (as executed):")
    for p in plan:
        where = f"{p['target']}:{p['port']}" if p.get("port") else p["target"]
        print(f"  {p['id']}: {p['source']} {p['kind']} {where} {p.get('query') or ''} expect {p['expect']}")
    for a in run["attempts"]:
        print(f"\n  {a['label']}")
        if a["error"]:
            print(f"    error: {a['error'][:300]}")
        if a["stage_failed"]:
            print(f"    {a['stage_failed']}: {(a['nft_output'] or '').strip()[:600]}")
        for p in a["probes"]:
            if p["passed"]:
                continue
            where = f"{p['target']}:{p['port']}" if p["port"] else p["target"]
            out = (p["stdout"] or p["stderr"] or "").strip().replace("\n", " | ")[:200]
            print(f"    FAIL [{p['origin']}] {p['probe_id']}: {p['source']} {p['kind']} {where} "
                  f"expected {p['expect']}, got {p['actual']} (exit {p['exit_code']}) {out}")
        if rulesets and a["ruleset"]:
            print("    ruleset:\n" + "\n".join("      " + line for line in a["ruleset"].splitlines()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_ids", nargs="+")
    ap.add_argument("--rulesets", action="store_true", help="also print each attempt's ruleset")
    args = ap.parse_args()
    store = Store(get_settings().nettwin_data_dir / "nettwin.db")
    for rid in args.run_ids:
        show(store, rid, args.rulesets)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
