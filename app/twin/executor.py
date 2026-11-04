"""Apply a ruleset to a fresh twin and run probes with fixed argv templates (CLAUDE.md §6 steps 4-6).

Model output reaches the twin in exactly one way: as the data file /etc/nettwin/ruleset.nft, bind-mounted
read-only into the router and loaded with `nft -f`. It is never executed on the host, never put in a shell
string and never used as argv.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

import docker
import docker.errors

from app.schemas import AttemptResult, ProbeResult, ResolvedProbe, TestPlan
from app.twin import invariants
from app.twin.topology import NETS, NODES, SINKHOLE_IP, SubnetPool, Twin, TwinError, leftovers, twin_up

log = logging.getLogger(__name__)

MAX_RULESET_BYTES = 20 * 1024
PROBE_TIMEOUT_S = 5  # every tool's own timeout (nc -w 3, curl -m 3, dig 2s, ping 2s) is below this
PROBE_WORKERS = 8
EVIDENCE_MAX_CHARS = 4000
RULESET_PATH_IN_ROUTER = "/etc/nettwin/ruleset.nft"

Emit = Callable[[str, dict], None]


def _no_emit(_event: str, _data: dict) -> None:
    pass


# ---------------------------------------------------------------- static validation (§6 step 4)

class RulesetRejected(ValueError):
    pass


_INCLUDE = re.compile(r"\binclude\b", re.IGNORECASE)
# The executor's header defines $IF_LAN, $NVR, ...; a ruleset must not redefine or shadow them.
_DEFINE = re.compile(r"\b(?:re|un)?define\b", re.IGNORECASE)
# Control, format (bidi overrides, zero-width), private-use, surrogate, unassigned, line/para separators.
_BAD_CATEGORIES = {"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"}


def validate_ruleset(text: str) -> str:
    """Return the cleaned ruleset or raise RulesetRejected. \\r is stripped; \\n and \\t are allowed."""
    text = text.replace("\r", "")
    if len(text.encode("utf-8")) > MAX_RULESET_BYTES:
        raise RulesetRejected(f"ruleset larger than {MAX_RULESET_BYTES // 1024} KB")
    if _INCLUDE.search(text):
        raise RulesetRejected("ruleset must not use 'include'")
    if m := _DEFINE.search(text):
        raise RulesetRejected(f"ruleset must not use '{m.group(0)}': topology variables are set by NetTwin")
    for i, ch in enumerate(text):
        if ch not in "\n\t" and unicodedata.category(ch) in _BAD_CATEGORIES:
            line = text.count("\n", 0, i) + 1
            raise RulesetRejected(f"non-printable character U+{ord(ch):04X} on line {line}")
    if not text.strip():
        raise RulesetRejected("ruleset is empty")
    return text


# ---------------------------------------------------------------- ruleset file

def topology_header(twin: Twin) -> str:
    """Prepended to every ruleset: `flush ruleset` plus topology variables. Subnets change on every
    attempt and interface names aren't guaranteed, so rulesets use these names, never literal IPs."""
    a = twin.addr
    lines = ["flush ruleset", "", "# --- NetTwin topology variables (prepended by the executor) ---"]
    for net in NETS:
        lines.append(f'define IF_{net.upper()} = "{twin.router_ifaces[net]}"')
    for net in NETS:
        lines.append(f"define NET_{net.upper()} = {a.subnets[net]}")
    for net in NETS:
        lines.append(f"define ROUTER_{net.upper()} = {a.router_ip(net)}")
    for node in NODES:
        lines.append(f"define {node.upper()} = {a.node_ip(node)}")
    lines += ["# --- end of NetTwin header ---", ""]
    return "\n".join(lines) + "\n"


def write_ruleset(twin: Twin, ruleset: str) -> Path:
    path = twin.attempt_dir / "ruleset.nft"
    # UTF-8 + LF regardless of platform: nft rejects CRLF, and comments may contain non-ASCII.
    path.write_text(topology_header(twin) + ruleset.rstrip("\n") + "\n", encoding="utf-8", newline="\n")
    path.chmod(0o644)
    return path


def _decode(b: bytes | None) -> str:
    return (b or b"").decode("utf-8", errors="replace").replace("\x00", "")


def apply_ruleset(twin: Twin) -> tuple[str | None, str]:
    """Syntax check, then load. Returns (failed_stage or None, nft output)."""
    router = twin.containers["router"]
    for stage, argv in (
        ("nft_check", ["nft", "-c", "-f", RULESET_PATH_IN_ROUTER]),
        ("nft_load", ["nft", "-f", RULESET_PATH_IN_ROUTER]),
    ):
        code, out = router.exec_run(argv)
        if code != 0:
            return stage, _decode(out)[:EVIDENCE_MAX_CHARS]
    return None, ""


# ---------------------------------------------------------------- probes (§7)

def build_argv(p: ResolvedProbe) -> list[str]:
    """Fixed templates. Every element is either a literal or a value from trusted topology/schema data."""
    if p.query is not None and p.query.startswith("-"):
        raise ValueError("query must not start with '-'")
    port = str(p.port) if p.port is not None else None
    match p.kind:
        case "tcp":
            return ["nc", "-z", "-w", "3", p.ip, port]
        case "icmp":
            return ["ping", "-c", "1", "-W", "2", p.ip]
        case "http":
            return ["curl", "-s", "-m", "3", "-o", "/dev/null", "-w", "%{http_code}", f"http://{p.ip}:{port}/"]
        case "ssh_banner":
            return ["nc", "-w", "3", p.ip, "22"]
        case "dns":
            return ["dig", "+noall", "+comments", "+answer", "+time=2", "+tries=1", f"@{p.ip}", p.query]
    raise ValueError(f"unknown probe kind {p.kind!r}")


_STATUS = re.compile(r"status:\s*([A-Z]+)")


def parse_dig(stdout: str) -> tuple[str | None, list[str]]:
    """(rcode or None if no reply at all, A records)."""
    m = _STATUS.search(stdout)
    answers = []
    for line in stdout.splitlines():
        parts = line.split()
        if line.startswith(";") or len(parts) < 5:
            continue
        if parts[3] == "A":
            answers.append(parts[4])
    return (m.group(1) if m else None), answers


def evaluate(p: ResolvedProbe, exit_code: int | None, stdout: str) -> tuple[str, bool, str]:
    """Pure Python verdict: (actual, passed, detail). No LLM involvement."""
    detail = ""
    match p.kind:
        case "tcp" | "icmp":
            actual = "allow" if exit_code == 0 else "deny"
        case "http":
            code = stdout.strip()[-3:]
            actual = "allow" if code[:1] in ("2", "3") else "deny"
            detail = f"http {code or '---'}"
        case "ssh_banner":
            first = stdout.split("\n", 1)[0].strip()
            first = "".join(ch for ch in first if ch.isprintable())[:80]
            actual = "allow" if first.startswith("SSH-") else "deny"
            detail = first
        case "dns":
            status, answers = parse_dig(stdout)
            detail = f"{status or 'no reply'} {' '.join(answers)}".strip()
            if status is None:
                actual = "no_answer"
            elif status == "NXDOMAIN":
                actual = "nxdomain"
            elif status == "NOERROR" and answers:
                actual = "sinkholed" if all(a == SINKHOLE_IP for a in answers) else "resolves"
            elif status == "NOERROR":
                actual = "noerror_empty"
            else:
                actual = status.lower()  # refused, servfail, ...
        case _:
            raise ValueError(p.kind)
    return actual, actual == p.expect, detail


def _result(p: ResolvedProbe, **kw) -> ProbeResult:
    return ProbeResult(
        id=p.id, origin=p.origin, source=p.source, kind=p.kind, target=p.target_label,
        port=p.port, query=p.query, expect=p.expect, description=p.description, **kw,
    )


def run_probe(twin: Twin, p: ResolvedProbe) -> ProbeResult:
    argv = build_argv(p)
    t0 = time.monotonic()
    try:
        code, (out, err) = twin.containers[p.source].exec_run(argv, demux=True)
    except Exception as e:  # docker error: recorded as evidence, never a pass
        return _result(p, actual="error", passed=False, detail=f"exec failed: {e}"[:300], argv=argv)
    stdout, stderr = _decode(out), _decode(err)
    actual, passed, detail = evaluate(p, code, stdout)
    return _result(
        p, actual=actual, passed=passed, detail=detail, argv=argv, exit_code=code,
        stdout=stdout[:EVIDENCE_MAX_CHARS], stderr=stderr[:EVIDENCE_MAX_CHARS],
        duration_ms=int((time.monotonic() - t0) * 1000),
    )


def _group_key(i: int, p: ResolvedProbe) -> tuple:
    # Probes to the same SSH service from the same source run in order, so a rate limit sees them sequentially.
    if p.kind == "ssh_banner" or p.port == 22:
        return ("ssh", p.source, p.ip)
    return ("solo", i)


def run_probes(twin: Twin, probes: list[ResolvedProbe], emit: Emit = _no_emit,
               deadline: float | None = None) -> list[ProbeResult]:
    """Run probes in parallel (deny probes only finish by timing out). Results keep input order."""
    groups: dict[tuple, list[int]] = {}
    for i, p in enumerate(probes):
        groups.setdefault(_group_key(i, p), []).append(i)
    results: dict[int, ProbeResult] = {}

    def run_group(idxs: list[int]) -> None:
        for i in idxs:
            results[i] = run_probe(twin, probes[i])
            emit("probe", results[i].model_dump(include={"id", "origin", "expect", "actual", "passed", "detail"}))

    longest = max((len(g) for g in groups.values()), default=0)
    budget = PROBE_TIMEOUT_S * longest + 5
    if deadline is not None:
        budget = max(0.0, min(budget, deadline - time.monotonic()))

    pool = ThreadPoolExecutor(max_workers=PROBE_WORKERS, thread_name_prefix="probe")
    futures = [pool.submit(run_group, idxs) for idxs in groups.values()]
    try:
        for f in as_completed(futures, timeout=budget):
            f.result()
    except FuturesTimeout:
        log.warning("attempt %s: probes still running at deadline", twin.attempt_id)
    finally:
        # Don't wait for stuck execs: they die when the twin is destroyed.
        pool.shutdown(wait=False, cancel_futures=True)

    return [
        results.get(i) or _result(p, actual="timeout", passed=False, detail="did not finish before the deadline",
                                  argv=build_argv(p))
        for i, p in enumerate(probes)
    ]


_SECRET = re.compile(r"VULTR|KEY|TOKEN|SECRET", re.IGNORECASE)


def check_secret_hygiene(twin: Twin) -> ProbeResult:
    """CC-3: no container's environment (read from the Docker API, not from inside) holds anything secret-like.
    Only variable NAMES are ever reported, never values."""
    hits = []
    for role, c in twin.containers.items():
        c.reload()
        for entry in c.attrs.get("Config", {}).get("Env") or []:
            if _SECRET.search(entry):
                hits.append(f"{role}:{entry.split('=', 1)[0]}")
    ok = not hits
    return ProbeResult(
        id="CC-3", origin="containment", source="control-plane", kind="env", target="all twin containers",
        expect="clean", actual="clean" if ok else "secret-like env", passed=ok,
        detail=f"{len(twin.containers)} containers checked" if ok else ", ".join(hits),
        description="Secret hygiene: no API keys or credentials inside any twin container",
    )


# ---------------------------------------------------------------- one attempt

def failure_lines(results: list[ProbeResult]) -> list[str]:
    """Feedback for the generator. Invariant violations are spelled out (§6 step 7)."""
    lines = []
    for r in results:
        if r.passed:
            continue
        if r.origin == "invariant" and r.id in invariants.INVARIANTS_BY_CODE:
            inv = invariants.INVARIANTS_BY_CODE[r.id]
            lines.append(f"INVARIANT VIOLATION {r.id}: {inv.violation} (expected {r.expect}, got {r.actual})")
        else:
            where = f"{r.target}:{r.port}" if r.port else (f"{r.target} query {r.query}" if r.query else r.target)
            lines.append(f"{r.id}: {r.source} {r.kind} -> {where} expected {r.expect}, got {r.actual}"
                         + (f" [{r.detail}]" if r.detail else ""))
    return lines


def run_attempt(client: docker.DockerClient, pool: SubnetPool, data_dir: Path, attempt_id: str,
                ruleset: str, plan: TestPlan, emit: Emit = _no_emit, timeout_s: float = 90) -> AttemptResult:
    """Validate -> fresh twin -> nft check/load -> probes + containment -> destroy (always)."""
    t0 = time.monotonic()
    deadline = t0 + timeout_s
    result = AttemptResult(attempt_id=attempt_id)

    try:
        ruleset = validate_ruleset(ruleset)
    except RulesetRejected as e:
        result.stage_failed, result.nft_output = "static", str(e)
        result.failures = [f"STATIC VALIDATION: {e}"]
        result.teardown_clean = True  # no twin was created
        return result

    attempt_dir = data_dir / "attempts" / attempt_id
    twin = None
    try:
        with twin_up(client, pool, attempt_id, attempt_dir) as twin:
            emit("twin_up", {"attempt_id": attempt_id, "subnets": {k: str(v) for k, v in twin.addr.subnets.items()}})
            write_ruleset(twin, ruleset)
            stage, out = apply_ruleset(twin)
            emit("nft_check", {"ok": stage is None, "stage": stage, "output": out})
            result.stage_failed, result.nft_output = stage, out

            probes = invariants.final_probes(plan, twin.addr) if stage is None else []
            cc = invariants.containment_probes(twin.addr)
            evidence = run_probes(twin, probes + cc, emit=emit, deadline=deadline)
            result.probes = evidence[: len(probes)]
            result.containment = evidence[len(probes):] + [check_secret_hygiene(twin)]
    except (TwinError, docker.errors.DockerException) as e:
        log.error("attempt %s: twin failed: %s", attempt_id, e)
        result.stage_failed = "twin"
        result.failures = [f"TWIN ERROR (not caused by the ruleset): {e}"]
    finally:
        # twin_up has already destroyed everything here; if the build failed, `twin` was never bound,
        # so verify by label instead.
        if twin is not None:
            result.teardown_clean = twin.teardown_clean
        else:
            try:
                result.teardown_clean = leftovers(client, attempt_id) == 0
            except Exception:
                result.teardown_clean = False
        emit("teardown", {"attempt_id": attempt_id, "clean": result.teardown_clean})

    if result.stage_failed in ("nft_check", "nft_load"):
        result.failures = [f"NFT {result.stage_failed.upper()} FAILED:\n{result.nft_output.strip()}"]
    elif result.stage_failed is None:
        result.failures = failure_lines(result.probes + result.containment)

    result.passed = (
        result.stage_failed is None
        and all(r.passed for r in result.probes)
        and all(r.passed for r in result.containment)
    )
    result.duration_s = round(time.monotonic() - t0, 1)
    emit("attempt_verdict", {"attempt_id": attempt_id, "passed": result.passed, "failures": result.failures})
    return result
