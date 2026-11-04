"""SQLite persistence (stdlib sqlite3). Tables: runs, attempts, probe_results (CLAUDE.md §3)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from app.schemas import AttemptRecord, ProbeResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    client_ip     TEXT,
    scenario      TEXT,
    request       TEXT NOT NULL,
    baseline      TEXT NOT NULL,
    model         TEXT,
    status        TEXT NOT NULL,          -- planning | plan_ready | running | passed | failed | error
    plan_json     TEXT,
    result_json   TEXT,                   -- full RunResult once finished
    approved      INTEGER NOT NULL DEFAULT 0,
    error         TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    id             TEXT PRIMARY KEY,      -- attempt_id (also the twin's docker label)
    run_id         TEXT NOT NULL REFERENCES runs(id),
    n              INTEGER NOT NULL,
    created_at     REAL NOT NULL,
    ruleset        TEXT,
    rationale      TEXT,
    stage_failed   TEXT,
    nft_output     TEXT,
    passed         INTEGER NOT NULL,
    failures_json  TEXT,
    label          TEXT,
    error          TEXT,
    duration_s     REAL,
    teardown_clean INTEGER
);
CREATE TABLE IF NOT EXISTS probe_results (
    attempt_id   TEXT NOT NULL REFERENCES attempts(id),
    probe_id     TEXT NOT NULL,
    origin       TEXT NOT NULL,
    source       TEXT, kind TEXT, target TEXT, port INTEGER, query TEXT,
    expect       TEXT, actual TEXT, passed INTEGER NOT NULL, detail TEXT,
    argv_json    TEXT, exit_code INTEGER, stdout TEXT, stderr TEXT, duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS attempts_run ON attempts(run_id);
CREATE INDEX IF NOT EXISTS probes_attempt ON probe_results(attempt_id);
CREATE INDEX IF NOT EXISTS runs_ip_time ON runs(client_ip, created_at);
"""

RUN_FIELDS = {"status", "model", "plan_json", "result_json", "approved", "error"}


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    def create_run(self, run_id: str, request: str, baseline: str, scenario: str | None = None,
                   client_ip: str | None = None, model: str | None = None) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO runs (id, created_at, updated_at, client_ip, scenario, request, baseline, model, status)"
                " VALUES (?,?,?,?,?,?,?,?, 'planning')",
                (run_id, now, now, client_ip, scenario, request, baseline, model),
            )

    def update_run(self, run_id: str, **fields) -> None:
        bad = set(fields) - RUN_FIELDS
        if bad:
            raise ValueError(f"unknown run fields {bad}")
        cols = ", ".join(f"{k} = ?" for k in fields)  # keys come from the RUN_FIELDS allowlist
        with self._lock:
            self._db.execute(f"UPDATE runs SET {cols}, updated_at = ? WHERE id = ?",  # noqa: S608
                             (*fields.values(), time.time(), run_id))

    def add_attempt(self, run_id: str, rec: AttemptRecord, attempt_id: str) -> None:
        res, prop = rec.result, rec.proposal
        with self._lock, self._db:
            self._db.execute("BEGIN")
            self._db.execute(
                "INSERT INTO attempts (id, run_id, n, created_at, ruleset, rationale, stage_failed, nft_output, passed,"
                " failures_json, label, error, duration_s, teardown_clean) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id, run_id, rec.n, time.time(),
                    prop.ruleset if prop else None, prop.rationale if prop else None,
                    res.stage_failed if res else None, res.nft_output if res else None,
                    int(bool(res and res.passed)), json.dumps(res.failures if res else []),
                    rec.label, rec.error, res.duration_s if res else None,
                    int(res.teardown_clean) if res else None,
                ),
            )
            if res:
                self._db.executemany(
                    "INSERT INTO probe_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [_probe_row(attempt_id, p) for p in res.probes + res.containment],
                )
            self._db.execute("COMMIT")

    def get_run(self, run_id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        run = dict(row)
        run["attempts"] = [dict(a) for a in self._db.execute(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY n", (run_id,))]
        for a in run["attempts"]:
            a["probes"] = [dict(p) for p in self._db.execute(
                "SELECT * FROM probe_results WHERE attempt_id = ?", (a["id"],))]
        return run

    def runs_since(self, client_ip: str, since: float) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM runs WHERE client_ip = ? AND created_at >= ?", (client_ip, since)).fetchone()[0]


def _probe_row(attempt_id: str, p: ProbeResult) -> tuple:
    return (attempt_id, p.id, p.origin, p.source, p.kind, p.target, p.port, p.query, p.expect, p.actual,
            int(p.passed), p.detail, json.dumps(p.argv), p.exit_code, p.stdout, p.stderr, p.duration_ms)
