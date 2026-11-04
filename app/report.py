"""Downloads unlocked by Approve: export.nft (the verified ruleset, ready for production values) and
report.md (the evidence report to attach to the change ticket)."""

from __future__ import annotations

import time

from app.twin.topology import NETS, NODES

_ROLE = {"mgmt": "management", "lan": "office LAN", "wan": "internet/WAN"}
_EG_IF = {"mgmt": '"ether1"', "lan": '"bridge-lan"', "wan": '"pppoe-wan"'}
_EG_NET = {"mgmt": "10.0.99.0/24", "lan": "192.168.10.0/24", "wan": "203.0.113.0/29"}
_EG_HOST = {"mgmt": "10.0.99.1", "lan": "192.168.10.1", "wan": "203.0.113.2"}


def export_nft(run: dict, result: dict) -> str:
    """The twin-verified ruleset with a production header. Every variable is CHANGE_ME, so `nft -f` refuses the
    file until the operator fills in real values: no half-configured file loads by accident."""
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(run["updated_at"]))
    timeline = " → ".join(a["label"] for a in result.get("attempts", []))
    lines = [
        f"# NetTwin export · run {run['id']} · approved {when}",
        f"# Request: {run['request'].splitlines()[0][:150]}",
        f"# Verified on a disposable twin: {timeline}. Model: {run['model']} on Vultr Serverless Inference.",
        "# Fill in your production values, check with `nft -c -f <file>`, then load with `nft -f <file>`.",
        "",
        "flush ruleset",
        "",
    ]
    for net in NETS:
        lines.append(f'define IF_{net.upper():<6} = "CHANGE_ME"     # {_ROLE[net]} interface, e.g. {_EG_IF[net]}')
    for net in NETS:
        lines.append(f"define NET_{net.upper():<5} = CHANGE_ME       # {_ROLE[net]} subnet, e.g. {_EG_NET[net]}")
    for net in NETS:
        lines.append(f"define ROUTER_{net.upper():<4} = CHANGE_ME     # router address on the {_ROLE[net]}, "
                     f"e.g. {_EG_HOST[net]}")
    for node in NODES:
        lines.append(f"define {node.upper():<10} = CHANGE_ME     # {node} host address")
    lines += ["", result["final_ruleset"].rstrip(), ""]
    return "\n".join(lines)


def _md(text: str) -> str:
    """Neutralise characters that would break a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def report_md(run: dict, result: dict) -> str:
    attempts = result.get("attempts", [])
    final = next((a for a in reversed(attempts) if a.get("result") and a["result"]["passed"]), None)
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(run["updated_at"]))
    out = [
        f"# NetTwin change report · run `{run['id']}`",
        "",
        f"- **Request:** {_md(run['request'])}",
        f"- **Verdict:** {'PASSED' if result['passed'] else 'FAILED'} · approved by a human operator · {when}",
        f"- **Attempts:** {' → '.join(a['label'] for a in attempts)}",
        f"- **LLM:** `{run['model']}` on Vultr Serverless Inference · twin host: Vultr Cloud Compute",
    ]
    if result.get("conflict_message"):
        out.append(f"- **Safety:** {result['conflict_message']}")
    out += ["", "## Summary", "", result.get("summary", ""), ""]
    if result.get("removed_from_plan"):
        out += ["## Removed from the test plan", ""]
        out += [f"- `{pid}`: {why}" for pid, why in result["removed_from_plan"]]
        out.append("")

    if final:
        res = final["result"]
        for title, rows in (("Evidence (test plan + invariants)", res["probes"]),
                            ("Containment checks (sandbox proof)", res["containment"])):
            out += [f"## {title}", "", "| | Probe | Source → target | Expected | Actual | Detail |",
                    "|---|---|---|---|---|---|"]
            for r in rows:
                tgt = r["target"] + (f":{r['port']}" if r.get("port") else "") + (f" {r['query']}" if r.get("query")
                                                                                  else "")
                out.append(f"| {'✅' if r['passed'] else '❌'} | `{r['id']}` | {_md(r['source'])} {r['kind']} → "
                           f"{_md(tgt)} | {r['expect']} | {r['actual']} | {_md(r.get('detail', ''))[:80]} |")
            out.append("")

    failed = [a for a in attempts if not (a.get("result") and a["result"]["passed"])]
    if failed:
        out += ["## Caught on the twin (never reached production)", ""]
        for a in failed:
            reasons = (a.get("result") or {}).get("failures") or [a.get("error", "")]
            out.append(f"- **{a['label']}**: " + "; ".join(_md(r.splitlines()[0]) for r in reasons if r)[:400])
        out.append("")

    out += ["## Diff (baseline → approved)", "", "```diff"]
    out += [d["text"] if d["op"] == "@" else f"{d['op']}{d['text']}" for d in result.get("diff", [])]
    out += ["```", "", "## Approved ruleset", "", "```nft", result.get("final_ruleset", "").rstrip(), "```", ""]
    if result.get("rationale"):
        out += ["## Generator rationale", "", result["rationale"], ""]
    out += ["---", "", "Pass/fail was computed by code from real probes on an isolated twin network, not by the LLM. "
            "Every twin was destroyed after its attempt.", ""]
    return "\n".join(out)
