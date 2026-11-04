"""FastAPI control plane (CLAUDE.md §10). Binds 127.0.0.1:8000; Caddy faces the internet.

    uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1

The agent and Docker SDK are synchronous, so runs execute in worker threads. A semaphore caps concurrent runs;
live progress goes to the browser over Server-Sent Events from a small in-memory event bus.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StringConstraints

from app import agent, report, scenarios
from app.llm import LLM, LLMError
from app.schemas import TestPlan
from app.store import Store
from app.twin import invariants
from app.twin.executor import RulesetRejected, validate_ruleset

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
TERMINAL_EVENTS = {"final", "error"}
FINISHED = {"passed", "failed", "error"}
MAX_EVENTS_PER_RUN = 3000


# ---------------------------------------------------------------- live events

class EventBus:
    """Thread-safe append-only event log per run. Workers emit; SSE handlers read by index (= SSE id)."""

    def __init__(self) -> None:
        self._events: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def emit(self, run_id: str, event: str, data: dict) -> None:
        with self._lock:
            events = self._events.setdefault(run_id, [])
            if len(events) < MAX_EVENTS_PER_RUN or event in TERMINAL_EVENTS:
                events.append({"id": len(events), "event": event, "data": data, "t": round(time.time(), 2)})

    def since(self, run_id: str, idx: int) -> list[dict]:
        with self._lock:
            return list(self._events.get(run_id, [])[idx:])

    def has(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._events

    def emitter(self, run_id: str) -> Callable[[str, dict], None]:
        return lambda event, data: self.emit(run_id, event, data)


# ---------------------------------------------------------------- request bodies

RequestText = Annotated[str, StringConstraints(min_length=3, max_length=600, pattern=r"^[^\x00-\x08\x0b-\x1f\x7f]*$")]


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: RequestText
    baseline: Annotated[str, StringConstraints(max_length=20_480)] | None = None
    scenario: str | None = None


# ---------------------------------------------------------------- app

class Runtime:
    """Everything a request handler needs. Built in the lifespan (production) or injected (tests)."""

    def __init__(self, deps: agent.Deps, max_concurrent: int, rate_limit_per_hour: int):
        self.deps = deps
        self.events = EventBus()
        self.workers = ThreadPoolExecutor(max_workers=8, thread_name_prefix="run")
        self.twin_slots = threading.BoundedSemaphore(max_concurrent)
        self.rate_limit = rate_limit_per_hour

    def run_deps(self) -> agent.Deps:
        """Fresh LLM wrapper per run (own token accounting), sharing the HTTP client."""
        d = self.deps
        return replace(d, llm=LLM(d.settings, client=d.llm.client))


def create_app(runtime_factory: Callable[[], Runtime] | None = None, background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.rt = (runtime_factory or _production_runtime)()
        stop = threading.Event()
        if background:
            from app.twin import janitor

            threading.Thread(target=janitor.run_forever, args=(app.state.rt.deps.docker_client, stop),
                             daemon=True, name="janitor").start()
        yield
        stop.set()
        app.state.rt.workers.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="NetTwin", lifespan=lifespan, docs_url=None, redoc_url=None)
    _routes(app)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _production_runtime() -> Runtime:
    import docker

    from app.config import get_settings, run_startup_checks
    from app.twin import janitor
    from app.twin.topology import NODE_IMAGE, ROUTER_IMAGE, SubnetPool

    settings = get_settings()
    run_startup_checks(settings)
    llm = LLM(settings)
    llm.verify_model()  # fail loudly at startup, never at a judge's first click
    client = docker.from_env()
    client.ping()
    for img in (ROUTER_IMAGE, NODE_IMAGE):
        client.images.get(img)
    janitor.sweep(client)
    deps = agent.Deps(settings=settings, llm=llm, store=Store(settings.nettwin_data_dir / "nettwin.db"),
                      docker_client=client, pool=SubnetPool(settings.nettwin_subnet_pool))
    return Runtime(deps, settings.nettwin_max_concurrent_runs, settings.nettwin_rate_limit_per_hour)


# ---------------------------------------------------------------- workers (threads)

def _plan_worker(rt: Runtime, run_id: str, body: CreateRun, baseline: str) -> None:
    emit = rt.events.emitter(run_id)
    try:
        agent.start_run(rt.run_deps(), body.request, baseline=baseline, scenario=body.scenario, emit=emit,
                        run_id=run_id, created=True)
    except LLMError:
        pass  # start_run already stored the error and emitted it
    except Exception as e:
        log.exception("run %s: planning crashed", run_id)
        rt.deps.store.update_run(run_id, status="error", error=f"internal error: {type(e).__name__}")
        emit("error", {"stage": "plan", "message": "internal error while planning"})


def _execute_worker(rt: Runtime, run_id: str) -> None:
    emit = rt.events.emitter(run_id)
    if not rt.twin_slots.acquire(blocking=False):
        emit("queued", {"message": "waiting for a free twin slot"})
        rt.twin_slots.acquire()
    try:
        run = rt.deps.store.get_run(run_id)
        plan = TestPlan.model_validate_json(run["plan_json"])
        emit("executing", {"run_id": run_id})
        agent.run_loop(rt.run_deps(), run_id, run["request"], run["baseline"], plan, emit=emit)
    except Exception as e:
        log.exception("run %s: execution crashed", run_id)
        rt.deps.store.update_run(run_id, status="error", error=f"internal error: {type(e).__name__}")
        emit("error", {"stage": "execute", "message": "internal error while executing"})
    finally:
        rt.twin_slots.release()


# ---------------------------------------------------------------- state for the UI

def run_state(rt: Runtime, run_id: str) -> dict:
    run = rt.deps.store.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    plan = TestPlan.model_validate_json(run["plan_json"]) if run["plan_json"] else None
    check = invariants.check_plan(plan) if plan else None
    result = json.loads(run["result_json"]) if run["result_json"] else None
    attempts = []
    for a in run["attempts"]:
        probes = [{**p, "argv": json.loads(p["argv_json"] or "[]"), "passed": bool(p["passed"])}
                  for p in a["probes"]]
        for p in probes:
            p.pop("argv_json", None)
            p.pop("attempt_id", None)
        attempts.append({
            "n": a["n"], "label": a["label"], "passed": bool(a["passed"]), "stage_failed": a["stage_failed"],
            "nft_output": a["nft_output"], "failures": json.loads(a["failures_json"] or "[]"), "error": a["error"],
            "ruleset": a["ruleset"], "rationale": a["rationale"], "duration_s": a["duration_s"],
            "teardown_clean": bool(a["teardown_clean"]) if a["teardown_clean"] is not None else None,
            "plan": [p for p in probes if p["origin"] != "containment"],
            "containment": [p for p in probes if p["origin"] == "containment"],
        })
    return {
        "id": run["id"], "status": run["status"], "request": run["request"], "scenario": run["scenario"],
        "model": run["model"], "created_at": run["created_at"], "approved": bool(run["approved"]),
        "error": run["error"], "baseline": run["baseline"],
        "plan": [p.model_dump() for p in plan.probes] if plan else [],
        "removed": [{"id": p.id, "reason": why} for p, why in check.removed] if check else [],
        "conflicts": check.conflicts if check else [],
        "invariants": [{"code": i.code, "description": i.probe.description, **i.probe.model_dump(
            include={"source", "kind", "target", "port", "expect"})} for i in invariants.INVARIANTS],
        "attempts": attempts,
        "result": {k: result[k] for k in ("passed", "diff", "summary", "conflict_message", "final_ruleset",
                                          "rationale", "llm_usage", "removed_from_plan")} if result else None,
    }


# ---------------------------------------------------------------- routes

def _routes(app: FastAPI) -> None:
    def rt(request: Request) -> Runtime:
        return request.app.state.rt

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health(request: Request):
        r = rt(request)
        docker_ok = True
        try:
            r.deps.docker_client.ping()
        except Exception:
            docker_ok = False
        return {"ok": docker_ok, "model": r.deps.llm.model, "llm": "Vultr Serverless Inference",
                "docker": docker_ok, "max_iterations": r.deps.settings.nettwin_max_iterations}

    @app.get("/api/scenarios")
    def get_scenarios():
        return {"scenarios": scenarios.as_dicts(), "baseline": agent.DEFAULT_BASELINE}

    @app.post("/api/runs", status_code=201)
    def create_run(body: CreateRun, request: Request):
        r = rt(request)
        ip = request.client.host if request.client else "unknown"  # real client IP via --proxy-headers
        if r.deps.store.runs_since(ip, time.time() - 3600) >= r.rate_limit:
            raise HTTPException(429, f"rate limit: {r.rate_limit} runs per hour per IP")
        baseline = body.baseline if body.baseline and body.baseline.strip() else agent.DEFAULT_BASELINE
        try:
            baseline = validate_ruleset(baseline)
        except RulesetRejected as e:
            raise HTTPException(422, f"baseline rejected: {e}") from e
        if body.scenario and body.scenario not in scenarios.BY_ID:
            raise HTTPException(422, "unknown scenario")
        run_id = agent.new_run_id()
        r.deps.store.create_run(run_id, body.request, baseline, scenario=body.scenario, client_ip=ip,
                                model=r.deps.llm.model)
        r.workers.submit(_plan_worker, r, run_id, body, baseline)
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, request: Request):
        return run_state(rt(request), run_id)

    @app.put("/api/runs/{run_id}/plan")
    def put_plan(run_id: str, plan: TestPlan, request: Request):
        """Save the user-edited plan. Only LLM probes are editable; invariants are never part of it and are
        always appended again at execution."""
        r = rt(request)
        if r.deps.store.get_status(run_id) != "plan_ready":
            raise HTTPException(409, "the plan can only be edited before the run starts")
        r.deps.store.update_run(run_id, plan_json=plan.model_dump_json())
        check = invariants.check_plan(plan)
        return {"removed": [{"id": p.id, "reason": why} for p, why in check.removed], "conflicts": check.conflicts}

    @app.post("/api/runs/{run_id}/execute", status_code=202)
    def execute(run_id: str, request: Request):
        r = rt(request)
        if not r.deps.store.transition(run_id, "plan_ready", "queued"):
            raise HTTPException(409, "run is not waiting for execution")
        r.workers.submit(_execute_worker, r, run_id)
        return {"run_id": run_id, "status": "queued"}

    @app.post("/api/runs/{run_id}/approve")
    def approve(run_id: str, request: Request):
        r = rt(request)
        status = r.deps.store.get_status(run_id)
        if status is None:
            raise HTTPException(404, "run not found")
        if status != "passed":
            raise HTTPException(409, "only a run that passed every check can be approved")
        r.deps.store.update_run(run_id, approved=1)
        return {"run_id": run_id, "approved": True}

    def _approved(r: Runtime, run_id: str) -> tuple[dict, dict]:
        run = r.deps.store.get_run(run_id)
        if not run:
            raise HTTPException(404, "run not found")
        if not run["approved"]:
            raise HTTPException(403, "approve the run first")
        return run, json.loads(run["result_json"])

    @app.get("/api/runs/{run_id}/export.nft")
    def export(run_id: str, request: Request):
        run, result = _approved(rt(request), run_id)
        return PlainTextResponse(report.export_nft(run, result), headers={
            "Content-Disposition": f'attachment; filename="nettwin-{run_id}.nft"'})

    @app.get("/api/runs/{run_id}/report.md")
    def report_md(run_id: str, request: Request):
        run, result = _approved(rt(request), run_id)
        return PlainTextResponse(report.report_md(run, result), media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="nettwin-{run_id}.md"'})

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request, last_event_id: str | None = Header(None)):
        r = rt(request)
        if r.deps.store.get_status(run_id) is None:
            raise HTTPException(404, "run not found")
        start = int(last_event_id) + 1 if last_event_id and last_event_id.isdigit() else 0
        return StreamingResponse(_sse(r, run_id, start, request), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _sse(r: Runtime, run_id: str, idx: int, request: Request):
    last_ping = time.monotonic()
    while not await request.is_disconnected():
        batch = r.events.since(run_id, idx)
        for ev in batch:
            yield f"id: {ev['id']}\nevent: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"
            idx = ev["id"] + 1
            if ev["event"] in TERMINAL_EVENTS:
                return
        if not batch:
            # Run finished before this process started (events are in memory only): tell the client to reload.
            if not r.events.has(run_id) and r.deps.store.get_status(run_id) in FINISHED:
                yield f"event: final\ndata: {json.dumps({'run_id': run_id, 'replayed': True})}\n\n"
                return
            if time.monotonic() - last_ping > 15:
                yield ": ping\n\n"  # keeps proxies from closing an idle stream during plan review
                last_ping = time.monotonic()
            await asyncio.sleep(0.25)


app = create_app()
