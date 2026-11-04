// NetTwin UI. Vanilla JS, no build step.
// SECURITY: every server/model-provided string is inserted with textContent (via el()), never innerHTML.
"use strict";

const NODES = ["admin", "staff", "hr", "nvr", "attacker"];
const TARGETS = ["router_mgmt", "router_lan", "router_wan", "admin", "staff", "hr", "nvr"];
const KINDS = ["tcp", "http", "icmp", "ssh_banner", "dns"];
const EXPECT = { tcp: ["allow", "deny"], http: ["allow", "deny"], icmp: ["allow", "deny"],
                 ssh_banner: ["allow", "deny"], dns: ["resolves", "sinkholed", "nxdomain"] };
const EVENTS = ["planning", "plan_ready", "queued", "executing", "generating", "twin_up", "nft_check",
                "probe", "teardown", "attempt_verdict", "final", "error"];

const $ = (id) => document.getElementById(id);
const S = { run: null, plan: [], es: null, selected: null, baseline: "", health: null };

function el(tag, props = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "value") e.value = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    let msg = typeof data === "string" ? data : data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(-2).join(".")}: ${d.msg}`).join("; ");
    throw new Error(msg || `HTTP ${res.status}`);
  }
  return data;
}

function setMsg(id, text, err = false) { const m = $(id); m.textContent = text || ""; m.classList.toggle("err", err); }
const show = (id, on = true) => $(id).classList.toggle("hidden", !on);

// ------------------------------------------------------------------ init

async function init() {
  const [sc, health] = await Promise.all([api("GET", "/api/scenarios"), api("GET", "/api/health").catch(() => null)]);
  S.baseline = sc.baseline;
  $("baseline").value = sc.baseline;
  S.health = health;
  if (health) $("footer").textContent = `LLM: ${health.model} on Vultr Serverless Inference · Twin host: Vultr Cloud Compute`;
  for (const s of sc.scenarios) {
    $("scenarios").append(el("button", { class: "ghost", title: s.story, onclick: () => { $("request").value = s.request; $("request").dataset.scenario = s.id; } },
      el("b", {}, s.id), s.title));
  }
  $("request").addEventListener("input", () => delete $("request").dataset.scenario);
  $("plan-btn").addEventListener("click", startPlan);
  $("add-probe").addEventListener("click", addProbe);
  $("run-btn").addEventListener("click", runOnTwin);
  $("approve-btn").addEventListener("click", approve);
  const m = location.hash.match(/run=([0-9a-f]{12})/);
  if (m) loadRun(m[1]);
}

async function startPlan() {
  const request = $("request").value.trim();
  if (request.length < 3) return setMsg("request-msg", "Describe the change first.", true);
  const baseline = $("baseline").value.trim() === S.baseline.trim() ? null : $("baseline").value;
  $("plan-btn").disabled = true;
  setMsg("request-msg", "Creating run…");
  try {
    const { run_id } = await api("POST", "/api/runs", { request, baseline, scenario: $("request").dataset.scenario || null });
    $("log").textContent = "";
    S.selected = null;
    loadRun(run_id);
  } catch (e) {
    setMsg("request-msg", e.message, true);
    $("plan-btn").disabled = false;
  }
}

function loadRun(id) {
  history.replaceState(null, "", `#run=${id}`);
  S.planDirty = false;
  if (S.es) S.es.close();
  S.es = new EventSource(`/api/runs/${id}/events`);
  for (const name of EVENTS) S.es.addEventListener(name, (ev) => onEvent(name, JSON.parse(ev.data)));
  refresh(id);
}

// ------------------------------------------------------------------ live events

function logLine(text, cls = "") {
  const log = $("log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  log.append(el("span", { class: cls }, `${new Date().toLocaleTimeString()}  ${text}\n`));
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function onEvent(name, d) {
  show("live-panel");
  switch (name) {
    case "planning": logLine(`Planning tests with ${d.model || "the LLM"} on Vultr Serverless Inference…`, "info"); setMsg("request-msg", "Planning… (reasoning models can take a minute)"); break;
    case "plan_ready": logLine(`Test plan ready: ${d.probes} probes. Review it, then run on the twin.`, "info"); refresh(); break;
    case "queued": logLine("Queued: waiting for a free twin slot.", "dim"); break;
    case "executing": logLine("Execution started.", "info"); refresh(); break;
    case "generating": logLine(`Attempt #${d.attempt}: generating nftables ruleset…`, "info"); markPending(d.attempt); break;
    case "twin_up": logLine(`Twin ${d.attempt_id} up (isolated, zero egress): ${Object.entries(d.subnets).map(([k, v]) => `${k} ${v}`).join(", ")}`); break;
    case "nft_check": d.ok ? logLine("nft -c ✓ syntax OK, ruleset loaded in the twin router", "ok")
                           : logLine(`nft rejected the ruleset (${d.stage}): ${d.output.split("\n")[0]}`, "bad"); break;
    case "probe": logLine(`${d.passed ? "PASS" : "FAIL"}  ${d.id.padEnd(26)} expect ${d.expect.padEnd(9)} got ${d.actual} ${d.detail || ""}`, d.passed ? "ok" : "bad"); break;
    case "teardown": logLine(`Twin ${d.attempt_id} destroyed${d.clean ? " (clean)" : " (LEFTOVERS!)"}`, "dim"); break;
    case "attempt_verdict":
      logLine(`Attempt verdict: ${d.passed ? "PASS" : "FAIL"}`, d.passed ? "ok" : "bad");
      for (const f of d.failures || []) logLine(`  ${f.split("\n")[0]}`, "bad");
      refresh(); break;
    case "final": logLine(d.replayed ? "Run already finished." : `Run finished: ${d.passed ? "PASS" : "FAIL"}`, d.passed ? "ok" : "info");
      S.es?.close(); refresh(); break;
    case "error": logLine(`Error (${d.stage}): ${d.message}`, "bad"); S.es?.close(); refresh(); break;
  }
}

function markPending(n) {
  const tl = $("timeline");
  if (![...tl.children].some((c) => c.dataset.n === String(n))) tl.append(el("span", { class: "chip pending", "data-n": n }, `#${n} …`));
}

// ------------------------------------------------------------------ state rendering

async function refresh(id = S.run?.id) {
  if (!id) return;
  try { S.run = await api("GET", `/api/runs/${id}`); } catch (e) { return setMsg("request-msg", e.message, true); }
  const r = S.run;
  $("run-badge").textContent = `run ${r.id} · ${r.status}`;
  show("run-badge");
  if (!$("request").value) $("request").value = r.request;
  $("plan-btn").disabled = !["passed", "failed", "error"].includes(r.status);
  setMsg("request-msg", r.status === "planning" ? "Planning… (reasoning models can take a minute)" : r.error ? `Error: ${r.error}` : "", !!r.error);
  if (r.status === "plan_ready" && !S.planDirty) S.plan = r.plan.map((p) => ({ ...p }));
  if (r.plan.length || r.status === "plan_ready") renderPlan();
  if (r.attempts.length || ["queued", "running"].includes(r.status)) { show("live-panel"); renderTimeline(); renderAttempt(); }
  renderResult();
}

function renderPlan() {
  const r = S.run, editable = r.status === "plan_ready";
  show("plan-panel");
  $("plan-hint").textContent = editable ? "· written by the LLM, edit freely; 🔒 rows are system invariants" : "· locked (run started)";
  show("add-probe", editable); show("run-btn", editable);
  const removed = Object.fromEntries(r.removed.map((x) => [x.id, x.reason]));
  const conflicts = r.conflicts || [];
  $("conflict-note").textContent = conflicts.length
    ? `Your request conflicts with ${conflicts.join(", ")}. Conflicting probes are struck out; NetTwin will keep the invariant.` : "";
  show("conflict-note", conflicts.length > 0);

  const head = el("tr", {}, ["", "id", "source", "kind", "target", "port / query", "expect", "description", ""].map((h) => el("th", {}, h)));
  const rows = (editable ? S.plan : r.plan).map((p, i) => editable ? editRow(p, i, removed[p.id]) : viewRow(p, removed[p.id]));
  const inv = r.invariants.map((v) => el("tr", { class: "inv" },
    el("td", {}, el("span", { class: "lock", title: "System invariant: cannot be edited or removed" }, "🔒")),
    el("td", { class: "mono" }, v.code), el("td", {}, v.source), el("td", {}, v.kind), el("td", {}, v.target),
    el("td", {}, v.port ?? (v.kind === "ssh_banner" ? "22" : "")), el("td", {}, v.expect), el("td", {}, v.description), el("td", {})));
  $("plan-table").replaceChildren(el("thead", {}, head), el("tbody", {}, rows, inv));
}

function viewRow(p, reason) {
  return el("tr", { class: reason ? "removed" : "" }, el("td", {}),
    el("td", { class: "mono" }, p.id), el("td", {}, p.source), el("td", {}, p.kind), el("td", {}, p.target),
    el("td", {}, p.port ?? p.query ?? ""), el("td", {}, p.expect),
    el("td", {}, p.description), el("td", { class: "reason" }, reason || ""));
}

function select(options, value, onchange) {
  return el("select", { onchange: (e) => onchange(e.target.value) }, options.map((o) => el("option", { value: o, selected: o === value }, o)));
}

function editRow(p, i, reason) {
  const upd = (k) => (v) => { S.plan[i][k] = v; S.planDirty = true; };
  const kindChange = (v) => {
    const q = S.plan[i];
    q.kind = v; S.planDirty = true;
    if (!EXPECT[v].includes(q.expect)) q.expect = EXPECT[v][0];
    if (v === "tcp" || v === "http") { q.port = q.port || 80; q.query = null; } else q.port = null;
    if (v === "dns") { q.query = q.query || "intranet.local"; if (!q.target.startsWith("router_")) q.target = "router_lan"; }
    else q.query = null;
    renderPlan();
  };
  const needsPort = p.kind === "tcp" || p.kind === "http";
  const pq = needsPort ? el("input", { type: "number", min: 1, max: 65535, value: p.port ?? "", oninput: (e) => upd("port")(e.target.value ? Number(e.target.value) : null) })
    : p.kind === "dns" ? el("input", { value: p.query ?? "", oninput: (e) => upd("query")(e.target.value.trim().toLowerCase() || null) })
    : el("span", { class: "muted" }, p.kind === "ssh_banner" ? "22" : "—");
  return el("tr", { class: reason ? "removed" : "" }, el("td", {}),
    el("td", {}, el("input", { class: "mono", value: p.id, oninput: (e) => upd("id")(e.target.value.trim()) })),
    el("td", {}, select(NODES, p.source, upd("source"))),
    el("td", {}, select(KINDS, p.kind, kindChange)),
    el("td", {}, select(p.kind === "dns" ? TARGETS.filter((t) => t.startsWith("router_")) : TARGETS, p.target, upd("target"))),
    el("td", {}, pq),
    el("td", {}, select(EXPECT[p.kind], p.expect, upd("expect"))),
    el("td", { class: "desc" }, el("input", { value: p.description, maxlength: 200, oninput: (e) => upd("description")(e.target.value) })),
    el("td", { class: "reason" }, reason || el("button", { class: "ghost", title: "Delete probe", onclick: () => { S.plan.splice(i, 1); S.planDirty = true; renderPlan(); } }, "✕")));
}

function addProbe() {
  let n = S.plan.length + 1;
  while (S.plan.some((p) => p.id === `probe-${n}`)) n++;
  S.plan.push({ id: `probe-${n}`, source: "attacker", kind: "tcp", target: "router_wan", port: 22, query: null, expect: "deny", description: "" });
  S.planDirty = true;
  renderPlan();
}

async function runOnTwin() {
  $("run-btn").disabled = true;
  setMsg("plan-msg", "Saving plan…");
  try {
    const clean = S.plan.map(({ id, source, kind, target, port, query, expect, description }) =>
      Object.fromEntries(Object.entries({ id, source, kind, target, port, query, expect, description }).filter(([, v]) => v !== null && v !== undefined)));
    const check = await api("PUT", `/api/runs/${S.run.id}/plan`, { probes: clean });
    S.planDirty = false;
    if (check.removed.length) logLine(`Removed before execution: ${check.removed.map((x) => `${x.id} (${x.reason})`).join(", ")}`, "dim");
    await api("POST", `/api/runs/${S.run.id}/execute`);
    setMsg("plan-msg", "");
    $("timeline").replaceChildren();
    refresh();
  } catch (e) {
    setMsg("plan-msg", e.message, true);
  } finally {
    $("run-btn").disabled = false;
  }
}

function renderTimeline() {
  const r = S.run;
  const chips = r.attempts.map((a) => el("span", {
    class: `chip ${a.passed ? "pass" : "fail"} ${S.selected === a.n ? "active" : ""}`,
    onclick: () => { S.selected = a.n; renderTimeline(); renderAttempt(); },
  }, a.label));
  if (["queued", "running"].includes(r.status)) chips.push(el("span", { class: "chip pending" }, r.status === "queued" ? "queued…" : `#${r.attempts.length + 1} …`));
  $("timeline").replaceChildren(...chips);
}

function renderAttempt() {
  const r = S.run;
  const a = r.attempts.find((x) => x.n === S.selected) || r.attempts[r.attempts.length - 1];
  if (!a) { $("evidence").replaceChildren(); $("containment").replaceChildren(el("div", { class: "cc-empty" }, "Runs after the first twin comes up.")); return; }
  $("evidence-title").textContent = `Evidence · attempt #${a.n}${a.stage_failed ? ` · ${a.stage_failed}` : ""}`;

  const ccGroups = {};
  for (const p of a.containment) (ccGroups[p.probe_id.slice(0, 4)] ||= []).push(p);
  const ccNames = { "CC-1": "No egress", "CC-2": "Host isolation", "CC-3": "Secret hygiene" };
  $("containment").replaceChildren(...Object.entries(ccGroups).map(([k, ps]) => {
    const ok = ps.every((p) => p.passed);
    return el("div", { class: "cc-item" }, el("span", { class: `pill ${ok ? "pass" : "fail"}` }, ok ? "PASS" : "FAIL"),
      el("span", { class: "name" }, `${k} ${ccNames[k] || ""}`),
      el("span", { class: "what" }, ps.map((p) => `${p.source} → ${p.target}${p.port ? ":" + p.port : ""}: ${p.actual}`).join(" · ")));
  }));

  const head = el("tr", {}, ["", "probe", "origin", "source → target", "expected", "actual", "detail", ""].map((h) => el("th", {}, h)));
  const body = [];
  if (a.stage_failed && a.nft_output) body.push(el("tr", {}, el("td", { colspan: 8 }, el("pre", { class: "raw mono" }, a.nft_output))));
  if (a.error) body.push(el("tr", {}, el("td", { colspan: 8, class: "muted" }, a.error)));
  for (const p of [...a.plan, ...a.containment]) {
    const raw = el("tr", { class: "hidden" }, el("td", { colspan: 8 }, el("pre", { class: "raw mono" },
      `$ ${p.argv.join(" ")}\nexit ${p.exit_code} · ${p.duration_ms} ms\n--- stdout\n${p.stdout || ""}\n--- stderr\n${p.stderr || ""}`)));
    body.push(el("tr", {},
      el("td", {}, el("span", { class: `pill ${p.passed ? "pass" : "fail"}` }, p.passed ? "PASS" : "FAIL")),
      el("td", { class: "mono" }, p.origin === "invariant" ? `🔒 ${p.probe_id}` : p.probe_id),
      el("td", {}, el("span", { class: "pill origin" }, p.origin)),
      el("td", {}, `${p.source} ${p.kind} → ${p.target}${p.port ? ":" + p.port : ""}${p.query ? " " + p.query : ""}`),
      el("td", {}, p.expect), el("td", {}, p.actual), el("td", { class: "mono" }, (p.detail || "").slice(0, 60)),
      el("td", {}, el("button", { class: "raw-toggle", onclick: () => raw.classList.toggle("hidden") }, "raw"))), raw);
  }
  $("evidence").replaceChildren(el("thead", {}, head), el("tbody", {}, body));
}

function renderResult() {
  const r = S.run, res = r.result;
  show("result-panel", !!res);
  if (!res) return;
  const v = $("verdict");
  v.textContent = res.passed ? "✓ PASSED on the twin" : `✗ FAILED after ${r.attempts.length} attempts. Nothing to approve`;
  v.className = `verdict ${res.passed ? "pass" : "fail"}`;
  $("conflict-banner").textContent = res.conflict_message || "";
  show("conflict-banner", !!res.conflict_message);
  $("summary").textContent = res.summary || "";
  $("diff").replaceChildren(...(res.diff || []).map((d) =>
    el("span", { class: { "+": "add", "-": "del", "@": "hunk", " ": "ctx" }[d.op] }, d.op === "@" ? d.text : `${d.op} ${d.text}`)));
  if (!res.diff?.length) $("diff").textContent = res.passed ? "(no configuration change)" : "(no passing ruleset)";
  $("rationale").textContent = res.rationale || "";
  $("final-ruleset").textContent = res.final_ruleset || "";
  const canApprove = res.passed && !r.approved;
  $("approve-btn").disabled = !canApprove;
  $("approve-btn").textContent = r.approved ? "Approved ✓" : "Approve";
  for (const [id, path] of [["dl-nft", "export.nft"], ["dl-report", "report.md"]]) {
    const a = $(id);
    a.classList.toggle("disabled", !r.approved);
    a.setAttribute("aria-disabled", String(!r.approved));
    if (r.approved) a.href = `/api/runs/${r.id}/${path}`; else a.removeAttribute("href");
  }
  setMsg("approve-msg", res.passed ? (r.approved ? "Downloads unlocked." : "A human must approve before the config can be downloaded.") : "");
}

async function approve() {
  $("approve-btn").disabled = true;
  try { await api("POST", `/api/runs/${S.run.id}/approve`); refresh(); }
  catch (e) { setMsg("approve-msg", e.message, true); $("approve-btn").disabled = false; }
}

init().catch((e) => setMsg("request-msg", `Failed to load: ${e.message}`, true));
