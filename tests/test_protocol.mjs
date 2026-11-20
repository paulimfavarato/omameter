// Protocol.js tests: command ids, pending/settled outcomes and the daemon line reducer.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const source = fs.readFileSync(path.resolve(import.meta.dirname, "..", "Protocol.js"), "utf8")
  .replace(/^\s*\.pragma\s+library\s*$/m, "");
const sandbox = { console, Math, JSON, Object, Array, String, Number, Boolean };
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "Protocol.js" });
const P = sandbox;

const plain = (v) => JSON.parse(JSON.stringify(v));

function ingest(state, msg) {
  const before = JSON.stringify(state);
  const r = P.ingest(state, typeof msg === "string" ? msg : JSON.stringify(msg));
  assert.equal(JSON.stringify(state), before, "ingest must not mutate its input");
  return { state: r.state, events: plain(r.events) };
}

test("encode assigns distinct ids, puts the id on the line and marks the command pending", () => {
  let s = P.initialState("t1");
  const a = P.encode(s, "add_hub", { address: "h", managementKey: "k", id: "spoof", cmd: "spoof" });
  assert.equal(JSON.stringify(s), JSON.stringify(P.initialState("t1")), "encode must not mutate its input");
  const b = P.encode(a.state, "get_state");
  assert.notEqual(a.id, b.id);
  assert.match(a.id, /^t1-/);
  assert.deepEqual(JSON.parse(a.line), { cmd: "add_hub", address: "h", managementKey: "k", id: a.id });
  assert.deepEqual(JSON.parse(b.line), { cmd: "get_state", id: b.id });
  assert.deepEqual(plain(P.outcome(b.state, a.id)), { status: "pending", error: "" });
  assert.deepEqual(plain(P.outcome(b.state, b.id)), { status: "pending", error: "" });
  assert.equal(P.outcome(b.state, "nope").status, "unknown");
});

test("result settles a pending command once, with ok or error", () => {
  let s = P.initialState("t");
  const a = P.encode(s, "add_api_key_account", { apiKey: "x" });
  const b = P.encode(a.state, "copy_text", { text: "y" });
  s = b.state;

  let r = ingest(s, { type: "result", id: a.id, ok: true });
  assert.deepEqual(r.events, [{ type: "result", id: a.id, cmd: "add_api_key_account", ok: true, error: "" }]);
  s = r.state;
  assert.deepEqual(plain(P.outcome(s, a.id)), { status: "ok", error: "" });

  r = ingest(s, { type: "result", id: b.id, ok: false, error: "wl-copy not available." });
  assert.deepEqual(r.events, [{ type: "result", id: b.id, cmd: "copy_text", ok: false, error: "wl-copy not available." }]);
  s = r.state;
  assert.deepEqual(plain(P.outcome(s, b.id)), { status: "failed", error: "wl-copy not available." });

  // A second result for the same id, or one for an id never sent, is ignored.
  for (const id of [a.id, "unknown-9"]) {
    r = ingest(s, { type: "result", id, ok: false, error: "late" });
    assert.deepEqual(r.events, []);
    assert.equal(r.state, s);
  }
  assert.equal(P.outcome(s, a.id).status, "ok");
});

test("ok must be literally true", () => {
  const a = P.encode(P.initialState("t"), "get_state");
  const r = ingest(a.state, { type: "result", id: a.id, ok: "yes" });
  assert.equal(r.events[0].ok, false);
});

test("state lines become one state event with safe defaults", () => {
  const s = P.initialState("t");
  let r = ingest(s, { type: "state", dashboard: { hasAccounts: true }, accounts: [{ id: "a" }], hubs: "bad",
    settings: { privacyMode: true }, canUndo: 1, nextRefreshAt: "2026-01-01T00:00:00Z", refreshing: true,
    authSession: { active: true }, providerCatalog: [{ id: "claude" }] });
  assert.equal(r.state, s);
  assert.deepEqual(r.events, [{
    type: "state", dashboard: { hasAccounts: true }, accounts: [{ id: "a" }], hubs: [],
    settings: { privacyMode: true }, layout: null, proxy: null, canUndo: true,
    nextRefreshAt: "2026-01-01T00:00:00Z", refreshing: true, authSession: { active: true },
    providerCatalog: [{ id: "claude" }]
  }]);
  r = ingest(s, { type: "state", providerCatalog: [] });
  assert.equal(r.events[0].providerCatalog, null);
  assert.equal(r.events[0].dashboard, null);
  assert.equal(r.events[0].authSession, null);
});

test("auth_update, local_credentials and toast lines", () => {
  const s = P.initialState("t");
  assert.deepEqual(ingest(s, { type: "auth_update", session: { status: "waiting" } }).events,
    [{ type: "authSession", session: { status: "waiting" } }]);
  assert.deepEqual(ingest(s, { type: "auth_update", session: null }).events, [{ type: "authSession", session: null }]);
  assert.deepEqual(ingest(s, { type: "local_credentials", items: [{ id: "c1" }] }).events,
    [{ type: "localCredentials", items: [{ id: "c1" }] }]);
  assert.deepEqual(ingest(s, { type: "local_credentials" }).events, [{ type: "localCredentials", items: [] }]);
  assert.deepEqual(ingest(s, { type: "toast", message: "Added X.", error: false }).events,
    [{ type: "toast", message: "Added X.", error: false }]);
  assert.deepEqual(ingest(s, { type: "toast", error: 1 }).events, [{ type: "toast", message: "", error: true }]);
});

test("invalid, oversized, non-object and unknown lines are ignored", () => {
  const s = P.initialState("t");
  const huge = JSON.stringify({ type: "toast", message: "x".repeat(P.MAX_LINE_CHARS) });
  for (const line of ["", "   ", "{not json", "[1,2]", "42", "null", '"state"', huge,
    JSON.stringify({ type: "mystery" }), JSON.stringify({ message: "no type" })]) {
    const r = P.ingest(s, line);
    assert.equal(r.state, s, line.slice(0, 20));
    assert.deepEqual(plain(r.events), [], line.slice(0, 20));
  }
  assert.deepEqual(plain(P.ingest(s, undefined).events), []);
});

test("daemonDown fails every pending command with the message, and only those", () => {
  let s = P.initialState("t");
  const a = P.encode(s, "detect_local_credentials");
  const b = P.encode(a.state, "add_hub");
  const c = P.encode(b.state, "get_state");
  s = ingest(c.state, { type: "result", id: c.id, ok: true }).state;

  const r = P.daemonDown(s, "Daemon exited with code 1");
  assert.deepEqual(plain(r.events).map((e) => [e.id, e.cmd, e.ok, e.error]), [
    [a.id, "detect_local_credentials", false, "Daemon exited with code 1"],
    [b.id, "add_hub", false, "Daemon exited with code 1"]
  ]);
  assert.equal(P.outcome(r.state, a.id).status, "failed");
  assert.equal(P.outcome(r.state, c.id).status, "ok");
  assert.deepEqual(plain(P.daemonDown(r.state, "again").events), []);
  assert.ok(P.daemonDown(P.encode(r.state, "x").state, "").events[0].error.length > 0, "a default message");
});

test("settle fails a command that never reached the daemon", () => {
  const a = P.encode(P.initialState("t"), "copy_text");
  const r = P.settle(a.state, a.id, false, "The Omameter daemon is not running.");
  assert.deepEqual(plain(r.events), [{ type: "result", id: a.id, cmd: "copy_text", ok: false,
    error: "The Omameter daemon is not running." }]);
  assert.equal(P.outcome(r.state, a.id).status, "failed");
  assert.deepEqual(plain(P.settle(r.state, a.id, true).events), []);
});

test("settled outcomes are bounded", () => {
  let s = P.initialState("t");
  const ids = [];
  for (let i = 0; i < P.MAX_SETTLED + 10; i++) {
    const e = P.encode(s, "get_state");
    ids.push(e.id);
    s = ingest(e.state, { type: "result", id: e.id, ok: true }).state;
  }
  assert.equal(Object.keys(s.settled).length, P.MAX_SETTLED);
  assert.equal(P.outcome(s, ids[0]).status, "unknown");
  assert.equal(P.outcome(s, ids[ids.length - 1]).status, "ok");
});
