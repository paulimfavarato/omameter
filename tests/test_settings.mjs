// Settings.js tests: parity with manifest.json, the shared edge cases backend/settings.py
// also runs (tests/fixtures/settings_cases.json), and the shell.json reconcile rule.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const root = path.resolve(import.meta.dirname, "..");
const rawSource = fs.readFileSync(path.join(root, "Settings.js"), "utf8").replace(/^\s*\.pragma\s+library\s*$/m, "");
const manifest = JSON.parse(fs.readFileSync(path.join(root, "manifest.json"), "utf8"));
const cases = JSON.parse(fs.readFileSync(path.join(root, "tests", "fixtures", "settings_cases.json"), "utf8"));

function loadSettings() {
  const sandbox = { console, Math, Number, String, Array, Object, Boolean, isFinite, isNaN };
  vm.createContext(sandbox);
  vm.runInContext(rawSource, sandbox, { filename: "Settings.js" });
  return sandbox;
}

const S = loadSettings();
// Values made inside the vm context have other prototypes; compare as plain JSON.
const plain = (v) => JSON.parse(JSON.stringify(v));
const SPEC_FIELDS = ["key", "type", "options", "min", "max", "step", "defaultValue"];

test("embedded schema matches manifest.json barWidget.schema", () => {
  const fromManifest = manifest.barWidget.schema.map((f) => {
    const out = {};
    for (const k of SPEC_FIELDS) if (f[k] !== undefined) out[k] = f[k];
    return out;
  });
  assert.deepEqual(plain(S.SCHEMA), fromManifest);
});

test("defaultSettings matches the manifest defaults block and schema defaults", () => {
  const fromSchema = Object.fromEntries(manifest.barWidget.schema.map((f) => [f.key, f.defaultValue]));
  assert.deepEqual(plain(S.defaultSettings()), fromSchema);
  assert.deepEqual(plain(S.defaultSettings()), manifest.barWidget.defaults);
  assert.deepEqual(plain(S.keys()), Object.keys(fromSchema));
});

function check(got, c) {
  for (const [k, v] of Object.entries(c.expect || {})) assert.strictEqual(got[k], v, `${c.name}: ${k}`);
  for (const k of c.absent || []) assert.ok(!(k in got), `${c.name}: ${k} absent`);
  assert.deepEqual(Object.keys(got).sort(), Object.keys(manifest.barWidget.defaults).sort(), c.name);
}

test("normalize agrees with backend/settings.py on the shared cases", () => {
  for (const c of cases.normalize) check(plain(S.normalize(c.raw)), c);
});

test("applyPatch agrees with backend/settings.py on the shared cases", () => {
  for (const c of cases.applyPatch) check(plain(S.applyPatch(c.current, c.patch)), c);
});

test("normalizePatch returns only the touched keys, both thresholds when one moves", () => {
  const current = S.defaultSettings();
  assert.deepEqual(plain(S.normalizePatch({ displayMode: "used" }, current)), { displayMode: "used" });
  assert.deepEqual(plain(S.normalizePatch({ displayMode: "bogus" }, current)), { displayMode: "remaining" });
  assert.deepEqual(plain(S.normalizePatch({ maxBarItems: 12 }, current)), { maxBarItems: 5 });
  assert.deepEqual(plain(S.normalizePatch({ maxBarItems: 8 }, { barLabelStyle: "compact", maxBarItems: 5 })), { maxBarItems: 8 });
  assert.deepEqual(
    plain(S.normalizePatch({ barLabelStyle: "full" }, { barLabelStyle: "compact", maxBarItems: 7 })),
    { barLabelStyle: "full", maxBarItems: 5 }
  );
  assert.deepEqual(
    plain(S.normalizePatch({ barLabelStyle: "icon" }, { barLabelStyle: "compact", maxBarItems: 7 })),
    { barLabelStyle: "icon" }
  );
  assert.deepEqual(
    plain(S.normalizePatch({ criticalThreshold: 20 }, { warningThreshold: 20, criticalThreshold: 15 })),
    { warningThreshold: 25, criticalThreshold: 20 }
  );
  assert.deepEqual(
    plain(S.normalizePatch({ warningThreshold: 20 }, { warningThreshold: 25, criticalThreshold: 20 })),
    { warningThreshold: 20, criticalThreshold: 15 }
  );
  assert.equal(S.normalizePatch({ nope: 1 }, current), null);
  assert.equal(S.normalizePatch(null, current), null);
});

test("stepThreshold steps by the schema step and respects bounds", () => {
  assert.equal(S.stepThreshold("warningThreshold", 30, 1), 35);
  assert.equal(S.stepThreshold("warningThreshold", 70, 1), 70);
  assert.equal(S.stepThreshold("warningThreshold", 20, -1), 20);
  assert.equal(S.stepThreshold("criticalThreshold", 5, -1), 5);
  assert.equal(S.stepThreshold("criticalThreshold", 12, 1), 15);
  assert.equal(S.stepThreshold("criticalThreshold", 25, 1), 25);
});

// ---- reconcile: shell.json -> daemon ----

const SHELL = { displayMode: "remaining", privacyMode: false, maxBarItems: 3 };

test("reconcile: the first read only records, it does not push", () => {
  const r = S.reconcile({}, SHELL, true, {});
  assert.equal(r.patch, null);
  assert.deepEqual(plain(r.lastSeen), SHELL);
  assert.deepEqual(plain(r.pending), {});
});

test("reconcile: an external edit pushes only the changed key", () => {
  const first = S.reconcile({}, SHELL, true, {});
  const r = S.reconcile(first.lastSeen, { ...SHELL, displayMode: "used" }, true, first.pending);
  assert.deepEqual(plain(r.patch), { displayMode: "used" });
  assert.equal(r.lastSeen.displayMode, "used");
});

test("reconcile: keys outside the schema and null values are ignored", () => {
  const first = S.reconcile({}, SHELL, true, {});
  const r = S.reconcile(first.lastSeen, { ...SHELL, someShellKey: 4, maxBarItems: null }, true, {});
  assert.equal(r.patch, null);
  assert.ok(!("someShellKey" in r.lastSeen));
});

test("reconcile: an edit while not ready waits and goes out once ready", () => {
  const first = S.reconcile({}, SHELL, true, {});
  const waiting = S.reconcile(first.lastSeen, { ...SHELL, privacyMode: true }, false, first.pending);
  assert.equal(waiting.patch, null);
  assert.deepEqual(plain(waiting.pending), { privacyMode: true });
  // A second edit while still down joins the queue.
  const more = S.reconcile(waiting.lastSeen, { ...SHELL, privacyMode: true, maxBarItems: 1 }, false, waiting.pending);
  assert.deepEqual(plain(more.pending), { privacyMode: true, maxBarItems: 1 });
  // The ready transition flushes with no new widget settings.
  const flushed = S.reconcile(more.lastSeen, null, true, more.pending);
  assert.deepEqual(plain(flushed.patch), { privacyMode: true, maxBarItems: 1 });
  assert.deepEqual(plain(flushed.pending), {});
  // Nothing left to send afterwards.
  assert.equal(S.reconcile(flushed.lastSeen, null, true, flushed.pending).patch, null);
});

test("reconcile: the echo of our own write sends nothing", () => {
  const first = S.reconcile({}, SHELL, true, {});
  const written = S.markWritten(first.lastSeen, { displayMode: "used", notAKey: 1 });
  assert.ok(!("notAKey" in written));
  const echo = S.reconcile(written, { ...SHELL, displayMode: "used" }, true, {});
  assert.equal(echo.patch, null);
});

test("reconcile: several widget instances handed the same edit send it once", () => {
  let state = { lastSeen: {}, pending: {} };
  const sent = [];
  function deliver(widgetSettings, ready) {
    const r = S.reconcile(state.lastSeen, widgetSettings, ready, state.pending);
    state = { lastSeen: r.lastSeen, pending: r.pending };
    if (r.patch) sent.push(plain(r.patch));
  }
  for (let i = 0; i < 3; i++) deliver(SHELL, true); // three monitors start up
  const edited = { ...SHELL, displayMode: "used" };
  for (let i = 0; i < 3; i++) deliver(edited, true); // each re-injected with the edit
  assert.deepEqual(sent, [{ displayMode: "used" }]);
  // Same while the daemon is down: queued once, sent once.
  const edited2 = { ...edited, privacyMode: true };
  for (let i = 0; i < 3; i++) deliver(edited2, false);
  deliver(null, true);
  deliver(null, true);
  assert.deepEqual(sent, [{ displayMode: "used" }, { privacyMode: true }]);
});
