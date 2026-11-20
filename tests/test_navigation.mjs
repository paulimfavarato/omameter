// Navigation.js tests: popup view, accounts subpages, drafts and auth-flow rules.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const navPath = path.resolve(import.meta.dirname, "..", "Navigation.js");
const rawSource = fs.readFileSync(navPath, "utf8").replace(/^\s*\.pragma\s+library\s*$/m, "");

function loadNavigation() {
  const sandbox = { console, Math, Date, Number, String, Array, Object, Boolean, RegExp, isFinite, isNaN, parseInt, parseFloat };
  vm.createContext(sandbox);
  vm.runInContext(rawSource, sandbox, { filename: "Navigation.js" });
  return sandbox;
}

const Nav = loadNavigation();

const ACTIVE = { active: true, status: "waiting", kind: "claude", method: "oauth" };
const ERROR = { active: false, status: "error", kind: "claude", method: "oauth" };
const COMPLETED = { active: false, status: "completed" };
const CANCELLED = { active: false, status: "cancelled" };
const ACCOUNTS = [
  { id: "acc1", kind: "copilot", label: "Work", accountName: "me@work", organization: "octo" },
  { id: "acc2", kind: "claude", label: "", accountName: "me@home" }
];

function ctx(extra) {
  return Object.assign({ authSession: null, accounts: ACCOUNTS, settings: { defaultTab: "priority" }, opened: true }, extra || {});
}

// Plain JSON so assert.deepEqual ignores the vm realm.
function plain(v) {
  return JSON.parse(JSON.stringify(v));
}

function run(state, action, c) {
  const before = JSON.stringify(state);
  const r = Nav.reduce(state, action, c || ctx());
  assert.equal(JSON.stringify(state), before, `reduce must not mutate its input (${action.type})`);
  return { state: plain(r.state), effects: plain(r.effects) };
}

// Run a sequence of actions; returns the final state and every effect.
function steps(state, actions, c) {
  const effects = [];
  for (const a of actions) {
    const r = run(state, a, c);
    state = r.state;
    effects.push(...r.effects);
  }
  return { state, effects };
}

function cmds(effects, name) {
  return effects.filter((e) => e.cmd === name);
}

function resets(effects) {
  return effects.filter((e) => e.ui === "resetScroll").length;
}

function accounts(sub) {
  const s = plain(Nav.initialState({}));
  s.viewMode = "accounts";
  if (sub) s.subPage = sub;
  return s;
}

const SECRETS = ["apiKey", "hubKey", "replaceHubKey", "manualCode"];

function withSecrets(state) {
  const s = plain(state);
  for (const f of SECRETS) s.drafts[f] = "secret-" + f;
  return s;
}

// ---- initial state & popup open ----

test("initialState takes the default tab from settings", () => {
  assert.equal(Nav.initialState({ defaultTab: "all" }).dashboardTab, "all");
  assert.equal(Nav.initialState({}).dashboardTab, "priority");
  const s = Nav.initialState(null);
  assert.equal(s.viewMode, "dashboard");
  assert.equal(s.subPage, "list");
  assert.deepEqual(plain(s.drafts), { apiKey: "", org: "", domain: "", manualCode: "", hubAddress: "", hubKey: "", rename: "", replaceHubKey: "" });
});

test("popupOpened without a session shows the dashboard on the default tab", () => {
  const s = accounts("account_detail");
  s.dashboardTab = "priority";
  const r = run(s, { type: "popupOpened", session: null }, ctx({ settings: { defaultTab: "all" } }));
  assert.equal(r.state.viewMode, "dashboard");
  assert.equal(r.state.dashboardTab, "all");
  assert.equal(resets(r.effects), 1);
  assert.equal(cmds(r.effects, "cancelAuth").length, 0);
});

test("popupOpened always resets scroll, even when the shown page is unchanged", () => {
  const r = run(plain(Nav.initialState({})), { type: "popupOpened", session: null });
  assert.equal(resets(r.effects), 1);
});

for (const [name, session] of [["active", ACTIVE], ["error", ERROR]]) {
  test(`browser stole focus: popup reopens on auth_flow with an ${name} session`, () => {
    // Start auth from the picker, the popup closes, then reopens.
    let { state } = steps(accounts("add_picker"), [{ type: "pickMethod", kind: "claude", method: "oauth" }]);
    assert.equal(state.subPage, "auth_flow");
    state = run(state, { type: "showDashboard" }, ctx({ authSession: session })).state; // view moved meanwhile
    state.drafts.manualCode = "typed";
    state.viewMode = "dashboard";
    const r = run(state, { type: "popupOpened", session }, ctx({ authSession: session }));
    assert.equal(r.state.viewMode, "accounts");
    assert.equal(r.state.subPage, "auth_flow");
    assert.equal(cmds(r.effects, "cancelAuth").length, 0);
    assert.equal(resets(r.effects), 1);
  });
}

// ---- auth session changes ----

test("authSessionChanged active or error forces auth_flow", () => {
  for (const session of [ACTIVE, ERROR]) {
    const r = run(accounts("list"), { type: "authSessionChanged", session });
    assert.equal(r.state.subPage, "auth_flow");
    assert.equal(resets(r.effects), 1);
    assert.equal(r.effects.filter((e) => e.cmd).length, 0);
  }
});

test("authSessionChanged completed / cancelled / null returns to the list and clears the manual code", () => {
  for (const session of [COMPLETED, CANCELLED, null]) {
    const s = accounts("auth_flow");
    s.drafts.manualCode = "abc";
    const r = run(s, { type: "authSessionChanged", session }, ctx({ authSession: session }));
    assert.equal(r.state.subPage, "list");
    assert.equal(r.state.drafts.manualCode, "");
    assert.equal(cmds(r.effects, "cancelAuth").length, 0);
    assert.equal(resets(r.effects), 1);
  }
});

test("authSessionChanged with an ended session outside auth_flow changes nothing", () => {
  const s = accounts("account_detail");
  const r = run(s, { type: "authSessionChanged", session: COMPLETED });
  assert.equal(r.state.subPage, "account_detail");
  assert.deepEqual(r.effects, []);
});

// ---- cancelAuth: exactly one ----

for (const [name, session] of [["active", ACTIVE], ["error", ERROR]]) {
  test(`Cancel on auth_flow sends exactly one cancelAuth (${name} session)`, () => {
    const r = run(accounts("auth_flow"), { type: "cancelAuth" }, ctx({ authSession: session }));
    assert.equal(r.state.subPage, "list");
    assert.equal(cmds(r.effects, "cancelAuth").length, 1);
    assert.equal(resets(r.effects), 1);
  });

  test(`Back on auth_flow sends exactly one cancelAuth (${name} session)`, () => {
    const r = run(accounts("auth_flow"), { type: "back" }, ctx({ authSession: session }));
    assert.equal(r.state.subPage, "list");
    assert.equal(r.state.viewMode, "accounts");
    assert.equal(cmds(r.effects, "cancelAuth").length, 1);
  });
}

test("leaving auth_flow by another navigation cancels a live session once", () => {
  const r = run(accounts("auth_flow"), { type: "openAccounts", accountId: "acc1" }, ctx({ authSession: ACTIVE }));
  assert.equal(r.state.subPage, "account_detail");
  assert.equal(cmds(r.effects, "cancelAuth").length, 1);
  assert.equal(r.effects[0].cmd, "cancelAuth");
});

test("leaving auth_flow without a session sends no cancelAuth", () => {
  const r = run(accounts("auth_flow"), { type: "back" }, ctx({ authSession: null }));
  assert.equal(r.state.subPage, "list");
  assert.equal(cmds(r.effects, "cancelAuth").length, 0);
});

test("Sign In Again on auth_flow restarts auth without cancelling", () => {
  const s = accounts("auth_flow");
  s.drafts.manualCode = "old";
  const r = run(s, { type: "beginAuth", kind: "claude", method: "oauth", accountId: "acc2", domain: null }, ctx({ authSession: ERROR }));
  assert.equal(r.state.subPage, "auth_flow");
  assert.equal(r.state.drafts.manualCode, "");
  assert.deepEqual(r.effects, [{ cmd: "startAuth", kind: "claude", method: "oauth", accountId: "acc2", domain: null }]);
});

// ---- openers ----

test("openList clears picker expansion and remove confirmations", () => {
  const s = accounts("account_detail");
  s.expandedPickerKind = "copilot";
  s.confirmRemoveAccount = true;
  s.confirmRemoveHub = true;
  const r = run(s, { type: "openList" });
  assert.equal(r.state.subPage, "list");
  assert.equal(r.state.expandedPickerKind, "");
  assert.equal(r.state.confirmRemoveAccount, false);
  assert.equal(r.state.confirmRemoveHub, false);
});

test("openAddPicker clears the expansion, domain and local scan", () => {
  const s = accounts("list");
  s.expandedPickerKind = "copilot";
  s.drafts.domain = "ghe.example";
  s.hasScannedLocal = true;
  const r = run(s, { type: "openAddPicker" });
  assert.equal(r.state.subPage, "add_picker");
  assert.equal(r.state.expandedPickerKind, "");
  assert.equal(r.state.drafts.domain, "");
  assert.equal(r.state.hasScannedLocal, false);
});

test("openAccountDetail selects the account and fills the rename draft", () => {
  const s = accounts("list");
  s.confirmRemoveAccount = true;
  let r = run(s, { type: "openAccountDetail", accountId: "acc1" });
  assert.equal(r.state.subPage, "account_detail");
  assert.equal(r.state.selectedAccountId, "acc1");
  assert.equal(r.state.drafts.rename, "Work");
  assert.equal(r.state.confirmRemoveAccount, false);
  r = run(s, { type: "openAccountDetail", accountId: "acc2" });
  assert.equal(r.state.drafts.rename, "me@home");
  r = run(s, { type: "openAccountDetail", accountId: "missing" });
  assert.equal(r.state.drafts.rename, "");
});

test("openHubDetail selects the hub and clears its key draft and confirmation", () => {
  const s = accounts("list");
  s.confirmRemoveHub = true;
  const r = run(s, { type: "openHubDetail", hubId: "hub1" });
  assert.equal(r.state.subPage, "hub_detail");
  assert.equal(r.state.selectedHubId, "hub1");
  assert.equal(r.state.drafts.replaceHubKey, "");
  assert.equal(r.state.confirmRemoveHub, false);
});

test("openApiKeyForm sets the form and prefills the organization of an existing account", () => {
  let r = run(accounts("add_picker"), { type: "openApiKeyForm", kind: "openrouter", method: "api_key", accountId: "" });
  assert.equal(r.state.subPage, "api_key_form");
  assert.equal(r.state.apiKeyKind, "openrouter");
  assert.equal(r.state.apiKeyMethod, "api_key");
  assert.equal(r.state.apiKeyExistingAccountId, "");
  assert.equal(r.state.drafts.org, "");
  r = run(accounts("account_detail"), { type: "openApiKeyForm", kind: "copilot", method: "org_token", accountId: "acc1" });
  assert.equal(r.state.apiKeyExistingAccountId, "acc1");
  assert.equal(r.state.drafts.org, "octo");
  assert.equal(r.state.drafts.apiKey, "");
});

test("openHubForm clears the address and key", () => {
  const s = accounts("list");
  s.drafts.hubAddress = "old:1";
  const r = run(s, { type: "openHubForm" });
  assert.equal(r.state.subPage, "hub_form");
  assert.equal(r.state.drafts.hubAddress, "");
  assert.equal(r.state.drafts.hubKey, "");
});

test("togglePickerKind expands and collapses, clearing the domain", () => {
  const s = accounts("add_picker");
  s.drafts.domain = "x";
  let r = run(s, { type: "togglePickerKind", kind: "copilot" });
  assert.equal(r.state.expandedPickerKind, "copilot");
  assert.equal(r.state.drafts.domain, "");
  r = run(r.state, { type: "togglePickerKind", kind: "copilot" });
  assert.equal(r.state.expandedPickerKind, "");
});

test("pickMethod: key methods open the key form, others start auth with the Copilot domain", () => {
  let r = run(accounts("add_picker"), { type: "pickMethod", kind: "copilot", method: "org_token" });
  assert.equal(r.state.subPage, "api_key_form");
  assert.equal(r.state.apiKeyMethod, "org_token");
  assert.equal(r.effects.filter((e) => e.cmd).length, 0);

  const s = accounts("add_picker");
  s.drafts.domain = "  octo.ghe.com ";
  r = run(s, { type: "pickMethod", kind: "copilot", method: "device_code" });
  assert.equal(r.state.subPage, "auth_flow");
  assert.deepEqual(cmds(r.effects, "startAuth"), [{ cmd: "startAuth", kind: "copilot", method: "device_code", accountId: null, domain: "octo.ghe.com" }]);

  s.drafts.domain = "ignored.example";
  r = run(s, { type: "pickMethod", kind: "chatgpt", method: "oauth" });
  assert.equal(cmds(r.effects, "startAuth")[0].domain, null);
});

test("scanLocal marks the scan and asks the service", () => {
  const r = run(accounts("add_picker"), { type: "scanLocal" });
  assert.equal(r.state.hasScannedLocal, true);
  assert.deepEqual(r.effects, [{ cmd: "detectLocalCredentials" }]);
});

// ---- back ----

test("back from every accounts subpage", () => {
  const expected = {
    add_picker: "list",
    auth_flow: "list",
    hub_form: "list",
    account_detail: "list",
    hub_detail: "list",
    api_key_form: "list"
  };
  for (const [from, to] of Object.entries(expected)) {
    const r = run(accounts(from), { type: "back" });
    assert.equal(r.state.viewMode, "accounts", from);
    assert.equal(r.state.subPage, to, from);
    assert.equal(resets(r.effects), 1, from);
  }
});

test("back from replacing an existing account's key returns to its detail", () => {
  const s = accounts("api_key_form");
  s.apiKeyExistingAccountId = "acc1";
  s.selectedAccountId = "acc1";
  s.drafts.rename = "Work";
  const r = run(s, { type: "back" });
  assert.equal(r.state.subPage, "account_detail");
  assert.equal(r.state.drafts.rename, "Work");
});

test("back from the accounts list and settings returns to the dashboard; back on the dashboard closes", () => {
  let r = run(accounts("list"), { type: "back" });
  assert.equal(r.state.viewMode, "dashboard");
  assert.equal(resets(r.effects), 1);
  const settings = plain(Nav.initialState({}));
  settings.viewMode = "settings";
  r = run(settings, { type: "back" });
  assert.equal(r.state.viewMode, "dashboard");
  r = run(plain(Nav.initialState({})), { type: "back" });
  assert.deepEqual(r.effects, [{ ui: "close" }]);
});

// ---- widget-level views ----

test("openAccounts, openAddAccount and openSettings open the popup", () => {
  const dash = plain(Nav.initialState({}));
  let r = run(dash, { type: "openAccounts", accountId: "" }, ctx({ opened: false }));
  assert.equal(r.state.viewMode, "accounts");
  assert.equal(r.state.subPage, "list");
  assert.deepEqual(r.effects, [{ ui: "open" }, { ui: "resetScroll" }]);
  r = run(dash, { type: "openAccounts", accountId: "acc2" });
  assert.equal(r.state.subPage, "account_detail");
  r = run(dash, { type: "openAddAccount" });
  assert.equal(r.state.subPage, "add_picker");
  assert.ok(r.effects.some((e) => e.ui === "open"));
  r = run(dash, { type: "openSettings" }, ctx({ opened: false }));
  assert.equal(r.state.viewMode, "settings");
  assert.ok(r.effects.some((e) => e.ui === "open"));
});

test("openSettings closes the popup when settings are already shown", () => {
  const s = plain(Nav.initialState({}));
  s.viewMode = "settings";
  let r = run(s, { type: "openSettings" }, ctx({ opened: true }));
  assert.deepEqual(r.effects, [{ ui: "close" }]);
  r = run(s, { type: "openSettings" }, ctx({ opened: false }));
  assert.ok(r.effects.some((e) => e.ui === "open"));
});

test("toggleAccounts and toggleSettings switch with the dashboard", () => {
  let r = run(plain(Nav.initialState({})), { type: "toggleAccounts" });
  assert.equal(r.state.viewMode, "accounts");
  assert.equal(r.state.subPage, "list");
  r = run(r.state, { type: "toggleAccounts" });
  assert.equal(r.state.viewMode, "dashboard");
  r = run(r.state, { type: "toggleSettings" });
  assert.equal(r.state.viewMode, "settings");
  r = run(r.state, { type: "toggleSettings" });
  assert.equal(r.state.viewMode, "dashboard");
});

// ---- resetScroll ----

test("resetScroll is emitted exactly when the shown view, tab or subpage changes", () => {
  const dash = plain(Nav.initialState({}));
  assert.equal(resets(run(dash, { type: "setDashboardTab", tab: "all" }).effects), 1);
  assert.equal(resets(run(dash, { type: "setDashboardTab", tab: "priority" }).effects), 0);
  assert.equal(resets(run(dash, { type: "showDashboard" }).effects), 0);
  assert.equal(resets(run(dash, { type: "toggleSettings" }).effects), 1);
  const list = accounts("list");
  assert.equal(resets(run(list, { type: "openList" }).effects), 0);
  assert.equal(resets(run(list, { type: "openHubForm" }).effects), 1);
  assert.equal(resets(run(list, { type: "setDraft", field: "rename", value: "x" }).effects), 0);
  assert.equal(resets(run(accounts("add_picker"), { type: "togglePickerKind", kind: "copilot" }).effects), 0);
  const detail = accounts("account_detail");
  assert.equal(resets(run(detail, { type: "askRemoveAccount", confirm: true }).effects), 0);
  // Hidden subpage change while on the dashboard: nothing shown changes.
  assert.equal(resets(run(dash, { type: "authSessionChanged", session: ACTIVE }).effects), 0);
});

test("unknown actions return the same state and no effects", () => {
  const s = Nav.initialState({});
  const r = Nav.reduce(s, { type: "nope" }, ctx());
  assert.equal(r.state, s);
  assert.equal(r.effects.length, 0);
});

// ---- canSubmit ----

test("canSubmit is the single per-form rule", () => {
  const s = accounts("api_key_form");
  s.apiKeyKind = "openrouter";
  s.apiKeyMethod = "api_key";
  assert.equal(Nav.canSubmit(s, "apiKey"), false);
  s.drafts.apiKey = "   ";
  assert.equal(Nav.canSubmit(s, "apiKey"), false);
  s.drafts.apiKey = "sk-1";
  assert.equal(Nav.canSubmit(s, "apiKey"), true);

  s.apiKeyKind = "copilot";
  s.apiKeyMethod = "org_token";
  assert.equal(Nav.isOrgTokenForm(s), true);
  assert.equal(Nav.canSubmit(s, "apiKey"), false, "org token needs an organization");
  s.drafts.org = " octo ";
  assert.equal(Nav.canSubmit(s, "apiKey"), true);

  assert.equal(Nav.canSubmit(s, "manualCode"), false);
  s.drafts.manualCode = "code";
  assert.equal(Nav.canSubmit(s, "manualCode"), true);

  assert.equal(Nav.canSubmit(s, "hub"), false);
  s.drafts.hubAddress = "hub:8317";
  assert.equal(Nav.canSubmit(s, "hub"), false);
  s.drafts.hubKey = "mk";
  assert.equal(Nav.canSubmit(s, "hub"), true);

  s.drafts.rename = "New";
  assert.equal(Nav.canSubmit(s, "rename"), false, "rename needs a selected account");
  s.selectedAccountId = "acc1";
  assert.equal(Nav.canSubmit(s, "rename"), true);

  s.drafts.replaceHubKey = "k";
  assert.equal(Nav.canSubmit(s, "replaceHubKey"), false, "replace needs a selected hub");
  s.selectedHubId = "hub1";
  assert.equal(Nav.canSubmit(s, "replaceHubKey"), true);

  assert.equal(Nav.canSubmit(s, "unknown"), false);
});

test("Enter on the API key field cannot skip the organization of an org token", () => {
  const s = accounts("api_key_form");
  s.apiKeyKind = "copilot";
  s.apiKeyMethod = "org_token";
  s.drafts.apiKey = "ghp_x";
  const r = run(s, { type: "submitApiKey" });
  assert.equal(cmds(r.effects, "addApiKeyAccount").length, 0);
  assert.equal(r.state.subPage, "api_key_form");
  assert.equal(r.state.drafts.apiKey, "ghp_x");
});

// ---- submits ----

// A waiting form: submit, the widget reports the command id, then the daemon's outcome.
function submitSent(state, action, c, id = "c1") {
  const sent = run(state, action, c);
  const next = run(sent.state, { type: "commandSent", form: sent.state.awaiting.form, id }, c);
  return { sent, state: next.state };
}

function apiKeyForm(existing) {
  const s = accounts("api_key_form");
  s.apiKeyKind = "copilot";
  s.apiKeyMethod = "org_token";
  s.apiKeyExistingAccountId = existing || "";
  s.drafts.apiKey = " ghp_x ";
  s.drafts.org = " octo ";
  return s;
}

function hubForm() {
  const s = accounts("hub_form");
  s.drafts.hubAddress = " hub:8317 ";
  s.drafts.hubKey = " mk ";
  return s;
}

function hubDetail() {
  const s = accounts("hub_detail");
  s.selectedHubId = "hub1";
  s.drafts.replaceHubKey = " new ";
  return s;
}

function codeForm() {
  const s = accounts("auth_flow");
  s.drafts.manualCode = " abc ";
  return s;
}

const WAITING_FORMS = [
  { form: "apiKey", page: "api_key_form", submit: "submitApiKey", make: () => apiKeyForm(), secret: "apiKey", c: () => ctx() },
  { form: "hub", page: "hub_form", submit: "submitHub", make: hubForm, secret: "hubKey", c: () => ctx() },
  { form: "replaceHubKey", page: "hub_detail", submit: "submitReplaceHubKey", make: hubDetail, secret: "replaceHubKey", c: () => ctx() },
  { form: "manualCode", page: "auth_flow", submit: "submitManualCode", make: codeForm, secret: "manualCode", c: () => ctx({ authSession: ACTIVE }) }
];

test("submits send the trimmed values and mark the form as waiting for that command", () => {
  let r = run(apiKeyForm("acc1"), { type: "submitApiKey" });
  assert.deepEqual(r.effects, [{ cmd: "addApiKeyAccount", kind: "copilot", apiKey: "ghp_x", organization: "octo", accountId: "acc1", awaits: "apiKey" }]);
  assert.deepEqual(r.state.awaiting, { form: "apiKey", id: "" });

  const plainKey = accounts("api_key_form");
  plainKey.apiKeyKind = "openrouter";
  plainKey.drafts.apiKey = "sk";
  plainKey.drafts.org = "stale";
  assert.equal(cmds(run(plainKey, { type: "submitApiKey" }).effects, "addApiKeyAccount")[0].organization, "");

  r = run(hubForm(), { type: "submitHub" });
  assert.deepEqual(r.effects, [{ cmd: "addHub", address: "hub:8317", managementKey: "mk", awaits: "hub" }]);
  r = run(hubDetail(), { type: "submitReplaceHubKey" });
  assert.deepEqual(r.effects, [{ cmd: "replaceHubKey", hubId: "hub1", managementKey: "new", awaits: "replaceHubKey" }]);
  r = run(codeForm(), { type: "submitManualCode" }, ctx({ authSession: ACTIVE }));
  assert.deepEqual(r.effects, [{ cmd: "submitAuthCode", code: "abc", awaits: "manualCode" }]);

  assert.equal(cmds(run(accounts("hub_form"), { type: "submitHub" }).effects, "addHub").length, 0);
  assert.deepEqual(run(accounts("auth_flow"), { type: "submitManualCode" }).effects, []);
});

for (const w of WAITING_FORMS) {
  test(`${w.form}: while waiting the form stays open with its secret and cannot resubmit`, () => {
    const { sent, state } = submitSent(w.make(), { type: w.submit }, w.c());
    assert.equal(sent.state.subPage, w.page);
    assert.notEqual(sent.state.drafts[w.secret], "", "secret kept while waiting");
    assert.deepEqual(state.awaiting, { form: w.form, id: "c1" });
    assert.equal(Nav.canSubmit(state, w.form), false);
    assert.equal(Nav.isAwaiting(state, w.form), true);
    const again = run(state, { type: w.submit }, w.c());
    assert.deepEqual(again.effects, [], "Enter while waiting sends nothing");
  });

  test(`${w.form}: failure keeps the form, its drafts and the daemon's error`, () => {
    const { state } = submitSent(w.make(), { type: w.submit }, w.c());
    const r = run(state, { type: "commandSettled", id: "c1", ok: false, error: "The hub rejected the key." }, w.c());
    assert.equal(r.state.subPage, w.page);
    assert.equal(r.state.drafts[w.secret], w.make().drafts[w.secret], "secret kept to fix it");
    assert.equal(r.state.awaiting, null);
    assert.equal(Nav.formError(r.state, w.form), "The hub rejected the key.");
    assert.equal(Nav.canSubmit(r.state, w.form), true);
    assert.deepEqual(r.effects, []);
    // Resubmitting clears the old error while it waits again.
    const again = run(r.state, { type: w.submit }, w.c());
    assert.equal(Nav.formError(again.state, w.form), "");
  });

  test(`${w.form}: a result for another id is ignored`, () => {
    const { state } = submitSent(w.make(), { type: w.submit }, w.c());
    const r = Nav.reduce(state, { type: "commandSettled", id: "other", ok: true }, w.c());
    assert.equal(r.state, state);
    assert.deepEqual(plain(r.effects), []);
  });

  test(`${w.form}: leaving while waiting clears the secret and a late result changes nothing`, () => {
    const { state } = submitSent(w.make(), { type: w.submit }, w.c());
    const left = run(state, { type: "showDashboard" }, w.c());
    assert.equal(left.state.drafts[w.secret], "");
    assert.equal(left.state.awaiting, null);
    for (const ok of [true, false]) {
      const late = Nav.reduce(left.state, { type: "commandSettled", id: "c1", ok, error: "x" }, w.c());
      assert.equal(late.state, left.state);
      assert.equal(Nav.formError(late.state, w.form), "");
    }
  });
}

test("success: the API key form goes to the list (or back to the account) and clears the key", () => {
  let { state } = submitSent(apiKeyForm(), { type: "submitApiKey" });
  let r = run(state, { type: "commandSettled", id: "c1", ok: true });
  assert.equal(r.state.subPage, "list");
  assert.equal(r.state.drafts.apiKey, "");
  assert.equal(r.state.awaiting, null);
  assert.equal(resets(r.effects), 1);

  ({ state } = submitSent(apiKeyForm("acc1"), { type: "submitApiKey" }));
  r = run(state, { type: "commandSettled", id: "c1", ok: true });
  assert.equal(r.state.subPage, "account_detail");
  assert.equal(r.state.drafts.apiKey, "");
});

test("success: the hub form goes to the list and clears the management key", () => {
  const { state } = submitSent(hubForm(), { type: "submitHub" });
  const r = run(state, { type: "commandSettled", id: "c1", ok: true });
  assert.equal(r.state.subPage, "list");
  assert.equal(r.state.drafts.hubKey, "");
});

test("success: hub key and manual code stay on their page and clear the secret", () => {
  let { state } = submitSent(hubDetail(), { type: "submitReplaceHubKey" });
  let r = run(state, { type: "commandSettled", id: "c1", ok: true });
  assert.equal(r.state.subPage, "hub_detail");
  assert.equal(r.state.drafts.replaceHubKey, "");

  ({ state } = submitSent(codeForm(), { type: "submitManualCode" }, ctx({ authSession: ACTIVE })));
  r = run(state, { type: "commandSettled", id: "c1", ok: true }, ctx({ authSession: ACTIVE }));
  assert.equal(r.state.subPage, "auth_flow");
  assert.equal(r.state.drafts.manualCode, "");
});

test("commandSent: only the waiting form takes an id; no id means it could not be sent", () => {
  const sent = run(hubForm(), { type: "submitHub" });
  assert.equal(Nav.reduce(sent.state, { type: "commandSent", form: "apiKey", id: "c9" }, ctx()).state, sent.state);
  const r = run(sent.state, { type: "commandSent", form: "hub", id: "", error: "Omameter service is not loaded." });
  assert.equal(r.state.awaiting, null);
  assert.equal(Nav.formError(r.state, "hub"), "Omameter service is not loaded.");
  assert.equal(r.state.drafts.hubKey, " mk ");
  const twice = submitSent(hubForm(), { type: "submitHub" }).state;
  assert.equal(Nav.reduce(twice, { type: "commandSent", form: "hub", id: "c2" }, ctx()).state, twice);
});

test("a form error disappears when its page is left", () => {
  const { state } = submitSent(hubForm(), { type: "submitHub" });
  const failed = run(state, { type: "commandSettled", id: "c1", ok: false, error: "bad" }).state;
  const away = run(failed, { type: "openList" }).state;
  assert.equal(away.formError, null);
  const back = run(away, { type: "openHubForm" }).state;
  assert.equal(Nav.formError(back, "hub"), "");
});

test("submitRename renames the selected account", () => {
  const s = accounts("account_detail");
  s.selectedAccountId = "acc1";
  s.drafts.rename = " Job ";
  const r = run(s, { type: "submitRename" });
  assert.deepEqual(r.effects, [{ cmd: "renameAccount", accountId: "acc1", label: "Job" }]);
  assert.equal(r.state.subPage, "account_detail");
});

test("remove confirmations: ask, cancel, confirm", () => {
  const s = accounts("account_detail");
  s.selectedAccountId = "acc1";
  let r = run(s, { type: "askRemoveAccount", confirm: true });
  assert.equal(r.state.confirmRemoveAccount, true);
  assert.deepEqual(r.effects, []);
  r = run(r.state, { type: "askRemoveAccount", confirm: false });
  assert.equal(r.state.confirmRemoveAccount, false);
  r = run({ ...s, confirmRemoveAccount: true }, { type: "confirmRemoveAccount" });
  assert.deepEqual(cmds(r.effects, "removeAccount"), [{ cmd: "removeAccount", accountId: "acc1" }]);
  assert.equal(r.state.subPage, "list");
  assert.equal(r.state.confirmRemoveAccount, false);

  const h = accounts("hub_detail");
  h.selectedHubId = "hub1";
  r = run(h, { type: "askRemoveHub", confirm: true });
  assert.equal(r.state.confirmRemoveHub, true);
  r = run(r.state, { type: "confirmRemoveHub" });
  assert.deepEqual(cmds(r.effects, "removeHub"), [{ cmd: "removeHub", hubId: "hub1" }]);
  assert.equal(r.state.subPage, "list");
});

// ---- secrets ----

test("setDraft only writes known fields", () => {
  let r = run(accounts("hub_form"), { type: "setDraft", field: "hubKey", value: "k" });
  assert.equal(r.state.drafts.hubKey, "k");
  r = run(accounts("hub_form"), { type: "setDraft", field: "bogus", value: "k" });
  assert.equal(r.state.drafts.bogus, undefined);
});

test("secrets survive typing on their own form", () => {
  const owners = { apiKey: "api_key_form", hubKey: "hub_form", replaceHubKey: "hub_detail", manualCode: "auth_flow" };
  for (const [field, page] of Object.entries(owners)) {
    const r = run(accounts(page), { type: "setDraft", field, value: "s3cret" });
    assert.equal(r.state.drafts[field], "s3cret", field);
  }
});

test("secrets are cleared on cancel, back and any navigation away from their form", () => {
  const exits = [
    { type: "back" },
    { type: "cancelAuth" },
    { type: "openList" },
    { type: "openAddPicker" },
    { type: "openAccountDetail", accountId: "acc1" },
    { type: "openHubDetail", hubId: "hub2" },
    { type: "openHubForm" },
    { type: "openApiKeyForm", kind: "openrouter", method: "api_key", accountId: "" },
    { type: "showDashboard" },
    { type: "toggleAccounts" },
    { type: "toggleSettings" },
    { type: "openSettings" },
    { type: "setDashboardTab", tab: "all" },
    { type: "popupOpened", session: null },
    { type: "authSessionChanged", session: ACTIVE }
  ];
  for (const page of ["api_key_form", "hub_form", "hub_detail", "auth_flow"]) {
    for (const action of exits) {
      const before = withSecrets(accounts(page));
      const r = run(before, action, ctx({ authSession: page === "auth_flow" ? ACTIVE : null }));
      const shown = r.state.viewMode === "accounts" ? r.state.subPage : "";
      const owners = { apiKey: "api_key_form", hubKey: "hub_form", replaceHubKey: "hub_detail", manualCode: "auth_flow" };
      for (const f of SECRETS) {
        if (owners[f] === shown && owners[f] === page && !["openHubForm", "openApiKeyForm", "openHubDetail"].includes(action.type)) {
          continue; // still on the form that owns it (e.g. auth_flow kept on authSessionChanged)
        }
        assert.equal(r.state.drafts[f], "", `${page} + ${action.type}: ${f}`);
      }
    }
  }
});

test("secrets are cleared after every successful submit", () => {
  for (const w of WAITING_FORMS) {
    const { state } = submitSent(withSecrets(w.make()), { type: w.submit }, w.c());
    const r = run(state, { type: "commandSettled", id: "c1", ok: true }, w.c());
    for (const f of SECRETS) assert.equal(r.state.drafts[f], "", `${w.form} success: ${f}`);
  }
});

test("a secret never survives the dashboard, even with its subpage still set underneath", () => {
  const s = withSecrets(accounts("api_key_form"));
  s.viewMode = "dashboard";
  const r = run(s, { type: "setDraft", field: "org", value: "o" });
  for (const f of SECRETS) assert.equal(r.state.drafts[f], "", f);
});
