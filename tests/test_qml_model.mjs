// Model.js tests. Line texts are checked against tests/fixtures/dashboard.json,
// which the real backend builder writes (python3 tests/fixtures/make_dashboard_fixture.py).
process.env.TZ = "UTC";

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const modelPath = path.resolve(import.meta.dirname, "..", "Model.js");
const rawSource = fs.readFileSync(modelPath, "utf8").replace(/^\s*\.pragma\s+library\s*$/m, "");
const fixture = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "fixtures", "dashboard.json"), "utf8"));

function loadModel() {
  const sandbox = { console, Math, Date, Number, String, Array, Object, Boolean, RegExp, isFinite, isNaN, parseInt, parseFloat };
  vm.createContext(sandbox);
  vm.runInContext(rawSource, sandbox, { filename: "Model.js" });
  return sandbox;
}

const Model = loadModel();
const NOW = new Date(fixture.now).getTime();
const MIN = 60 * 1000;
const dash = fixture.dashboard;
const prio = dash.priority;
const relative = { resetDisplayMode: "relative", timeFormat: "auto", nowMs: NOW };

function row(section, key, provider) {
  const found = prio[section].find((r) => r.key === key && (!provider || r.providerName === provider));
  assert.ok(found, `fixture row ${section}/${key}`);
  return found;
}

function card(kind) {
  return dash.allUsage.find((c) => c.kind === kind);
}

function allLines(d) {
  const out = [];
  for (const c of d.allUsage) for (const a of c.accounts) for (const l of a.lines) {
    out.push(l);
    for (const m of l.memberRows || []) out.push(m);
  }
  for (const s of ["pinned", "runningOut", "useItOrLoseIt", "usedUp"]) out.push(...d.priority[s]);
  out.push(...d.bar.allItems);
  return out;
}

test("autoTextSafe neutralizes HTML/rich-text markup, trackers, and control/bidi chars", () => {
  const payload = '<img src="https://example.invalid/tracker"> & <b>Alert</b>‮\x07';
  const safe = Model.autoTextSafe(payload);
  assert.ok(!safe.includes("<"), "must strip or neutralize '<'");
  assert.ok(!safe.includes(">"), "must strip or neutralize '>'");
  assert.ok(!safe.includes("&"), "must neutralize '&'");
  assert.ok(!safe.includes("‮"), "must strip bidi override");
  assert.ok(!safe.includes("\x07"), "must strip control chars");
});

test("describeLine writes each noteKind's note live from the backend's instants", () => {
  const cases = [
    [row("pinned", "session", "Claude"), "reset", "Resets in 2h"],
    [row("pinned", "weekly", "ChatGPT"), "reset", "Resets in 3d"],
    [row("runningOut", "session"), "reset", "Resets in 4h 30m"],
    [row("runningOut", "burst"), "outIn", "Out in 1h 40m"],
    [row("useItOrLoseIt", "monthly"), "resetRelative", "Resets in 20h"],
    [row("usedUp", "fable"), "resetCompact", "12h"],
    [row("usedUp", "credits"), "amount", "$0 left"],
  ];
  for (const [line, kind, note] of cases) {
    assert.equal(line.noteKind, kind, line.metricId);
    assert.equal(Model.describeLine(line, relative).noteText, note, line.metricId);
  }

  // "reset" follows resetDisplayMode ("auto" is 24h); "resetRelative" always counts down.
  const exact = { resetDisplayMode: "exact", timeFormat: "auto", nowMs: NOW };
  assert.equal(Model.describeLine(row("pinned", "weekly", "ChatGPT"), exact).noteText, "Resets Thu 12:00");
  assert.equal(Model.describeLine(row("runningOut", "session"), exact).noteText, "Resets 16:30");
  assert.equal(Model.describeLine(row("runningOut", "session"), { ...exact, timeFormat: "12h" }).noteText, "Resets 4:30 PM");
  assert.equal(Model.describeLine(row("useItOrLoseIt", "monthly"), exact).noteText, "Resets in 20h");
});

test("Out in counts down with nowMs instead of freezing at emit time", () => {
  const burst = row("runningOut", "burst");
  assert.equal(Model.describeLine(burst, relative).noteText, "Out in 1h 40m");
  assert.equal(Model.describeLine(burst, { ...relative, nowMs: NOW + 40 * MIN }).noteText, "Out in 1h");
  assert.equal(Model.describeLine(burst, { ...relative, nowMs: NOW + 101 * MIN }).noteText, "Out now");

  const panelBurst = card("codex").accounts[0].lines.find((l) => l.key === "burst");
  assert.equal(panelBurst.paceInlineKind, "outIn");
  assert.equal(Model.describeLine(panelBurst, { ...relative, nowMs: NOW + 40 * MIN }).metaText, "Out in 1h");
  const weekly = card("codex").accounts[0].lines.find((l) => l.key === "weekly");
  assert.equal(Model.describeLine(weekly, relative).metaText, weekly.paceTooltip);
  const usage = card("cursor").accounts[0].lines.find((l) => l.key === "usage");
  assert.equal(Model.describeLine(usage, relative).metaText, "Billing cycle ends Jul 31");
});

test("panel, bar tooltip and copy summary say the same reset text for the same line", () => {
  const panelLine = card("codex").accounts[0].lines.find((l) => l.key === "session");
  const later = { ...relative, nowMs: NOW + 30 * MIN };
  const panel = Model.describeLine(panelLine, later).resetText;
  assert.equal(panel, "Resets in 4h");

  const tip = Model.buildBarTooltip(dash, "", later.nowMs, "Omameter", later);
  assert.ok(tip.split("\n").includes("ChatGPT · Session: 25% left · " + panel), tip);

  const summary = Model.formatSummaryText(dash, later);
  assert.ok(summary.split("\n").includes("  - Session: 25% left (" + panel + ")"), summary);

  const exact = { resetDisplayMode: "exact", timeFormat: "24h", nowMs: NOW };
  const panelExact = Model.describeLine(panelLine, exact).resetText;
  assert.equal(panelExact, "Resets 16:30");
  assert.ok(Model.buildBarTooltip(dash, "", NOW, "Omameter", exact).includes("ChatGPT · Session: 25% left · " + panelExact));
  assert.ok(Model.formatSummaryText(dash, exact).includes("  - Session: 25% left (" + panelExact + ")"));
});

test("accountHeader and displayAccountName are the single naming and privacy rule", () => {
  assert.equal(Model.accountHeader("Cursor", "cursor", {}), "Cursor");
  assert.equal(Model.accountHeader("Cursor", "Cursor", { privacyMode: true }), "Cursor");
  assert.equal(Model.accountHeader("Claude", "", { privacyMode: true }), "Claude");
  assert.equal(Model.accountHeader("ChatGPT", "dev@example.com", {}), "ChatGPT · dev@example.com");
  assert.equal(Model.accountHeader("ChatGPT", "dev@example.com", { privacyMode: true }), "ChatGPT · d•••");
  assert.equal(Model.displayAccountName("", { privacyMode: true }), "•••");
  assert.equal(Model.displayAccountName("2 Accounts", { privacyMode: true, isAggregate: true }), "2 Accounts");
  assert.equal(Model.displayAccountName("3 Accountants Inc", { privacyMode: true }), "3•••");

  const codexRow = row("runningOut", "session");
  assert.equal(Model.describeLine(codexRow, relative).header, "ChatGPT · dev@example.com");
  assert.equal(Model.describeLine(codexRow, { ...relative, privacyMode: true }).header, "ChatGPT · d•••");
  const unifiedRow = row("pinned", "session", "Claude");
  assert.equal(unifiedRow.isAggregateName, true);
  assert.equal(Model.describeLine(unifiedRow, { ...relative, privacyMode: true }).header, "Claude · 2 Accounts");
  const member = unifiedRow.memberRows.find((m) => m.accountName === "Personal");
  assert.equal(Model.describeLine(member, { ...relative, privacyMode: true }).accountLabel, "P•••");
});

test("privacy reaches the bar tooltip and the copy summary", () => {
  const priv = fixture.privacyDashboard;
  assert.equal(priv.bar.items.length, 0);
  assert.equal(Model.buildBarTooltip(priv, "", NOW, "Omameter", relative), "Omameter · Privacy Mode");
  assert.equal(Model.buildBarTooltip(dash, "", NOW, "Omameter", { ...relative, privacyMode: true }), "Omameter · Privacy Mode");

  const summary = Model.formatSummaryText(dash, { ...relative, privacyMode: true });
  assert.ok(!summary.includes("dev@example.com"));
  assert.ok(!summary.includes("evil.invalid"));
  assert.ok(summary.includes("ChatGPT · d•••"));
  assert.ok(summary.includes("Claude · 2 Accounts (Multiple)"), summary);
  assert.ok(summary.split("\n").includes("Cursor (Pro)"), "an account named after its provider stays unprefixed");
  assert.ok(summary.split("\n").includes("OpenRouter · <•••"));
});

test("bar tooltip sanitizes provider and account markup and lists rows by section", () => {
  const tip = Model.buildBarTooltip(dash, new Date(NOW + 5 * MIN).toISOString(), NOW, "Omameter", relative);
  const lines = tip.split("\n");
  assert.equal(lines[0], "Omameter");
  assert.ok(lines.includes("Claude · Session: 70% left · Resets in 2h"), tip);
  assert.ok(lines.includes("ChatGPT · Burst: 40% left · Out in 1h 40m"), tip);
  assert.ok(lines.includes("ChatGPT · Fable: 0% left · 12h"), tip);
  assert.equal(lines.length, 1 + 6 + 1, "at most six rows plus header and next update");
  assert.equal(lines[lines.length - 1], "Next update in 5m");
  assert.ok(!tip.includes("<") && !tip.includes(">"));
  assert.equal(Model.buildBarTooltip(null, "", NOW, "<b>Empty</b>"), "‹b›Empty‹/b›");
});

test("no line, tooltip or summary output contains undefined", () => {
  for (const d of [dash, fixture.privacyDashboard]) {
    for (const privacyMode of [false, true]) {
      const opts = { ...relative, privacyMode };
      for (const line of allLines(d)) {
        const described = Model.describeLine(line, opts);
        for (const [k, v] of Object.entries(described)) {
          assert.ok(!String(v).includes("undefined"), `${line.metricId}.${k} = ${v}`);
        }
      }
      assert.ok(!Model.buildBarTooltip(d, "", NOW, "Omameter", opts).includes("undefined"));
      assert.ok(!Model.formatSummaryText(d, opts).includes("undefined"));
    }
  }
  assert.ok(!JSON.stringify(Model.describeLine({}, {})).includes("undefined"));
});

test("isSafeExternalUrl accepts only http/https", () => {
  assert.equal(Model.isSafeExternalUrl("https://claude.ai/settings/usage"), true);
  assert.equal(Model.isSafeExternalUrl("javascript:alert(1)"), false);
  assert.equal(Model.isSafeExternalUrl("file:///etc/passwd"), false);
});

test("formatResetDisplay handles relative countdowns, expiry, and fallbacks", () => {
  const now = new Date(2026, 5, 10, 12, 0, 0).getTime();
  const in90m = new Date(now + 90 * 60 * 1000).toISOString();
  assert.equal(Model.formatResetDisplay(in90m, "relative", "24h", now, "fallback"), "Resets in 1h 30m");
  assert.equal(Model.formatResetDisplay(in90m, "relative", "24h", now + 30 * 60 * 1000, "fallback"), "Resets in 1h");
  assert.equal(Model.formatResetDisplay(in90m, "relative", "24h", now + 95 * 60 * 1000, "fallback"), "Resets now");
  assert.equal(Model.formatResetDisplay("", "relative", "24h", now, "Resets soon"), "Resets soon");
  assert.equal(Model.formatResetDisplay("not-a-date", "relative", "24h", now, "Resets soon"), "Resets soon");
});

test("formatResetDisplay formats exact and absolute local times in 24h and 12h", () => {
  const now = new Date(2026, 5, 10, 10, 0, 0).getTime(); // Wed Jun 10 2026 10:00 local
  const sameDayAfternoon = new Date(2026, 5, 10, 14, 30, 0).toISOString();
  const sameDayMidnight = new Date(2026, 5, 10, 0, 5, 0).toISOString();
  const nextFriday = new Date(2026, 5, 12, 14, 30, 0).toISOString(); // Fri Jun 12 2026 14:30 local

  assert.equal(Model.formatResetDisplay(sameDayAfternoon, "exact", "24h", now, ""), "Resets 14:30");
  assert.equal(Model.formatResetDisplay(sameDayAfternoon, "exact", "auto", now, ""), "Resets 14:30");
  assert.equal(Model.formatResetDisplay(sameDayAfternoon, "absolute", "12h", now, ""), "Resets 2:30 PM");
  assert.equal(Model.formatResetDisplay(sameDayMidnight, "exact", "12h", now, ""), "Resets 12:05 AM");
  assert.equal(Model.formatResetDisplay(nextFriday, "exact", "24h", now, ""), "Resets Fri 14:30");
  assert.equal(Model.formatResetDisplay(nextFriday, "absolute", "12h", now, ""), "Resets Fri 2:30 PM");
});

test("formatCompactResetDisplay formats compact durations, now, and fallbacks", () => {
  const now = new Date(2026, 5, 10, 12, 0, 0).getTime();
  const in2h15m = new Date(now + (2 * 60 + 15) * 60 * 1000).toISOString();
  assert.equal(Model.formatCompactResetDisplay(in2h15m, now, "fb"), "2h 15m");
  assert.equal(Model.formatCompactResetDisplay(in2h15m, now + 3 * 3600 * 1000, "fb"), "now");
  assert.equal(Model.formatCompactResetDisplay("", now, "fb"), "fb");
  assert.equal(Model.formatCompactResetDisplay("invalid", now, "fb"), "fb");
});

test("tr translates through the strings object and falls back without one", () => {
  const calls = [];
  const strings = { t: (key, fb) => { calls.push([key, fb]); return "T:" + key; } };
  assert.equal(Model.tr(strings, "settings.alerts", "Alerts"), "T:settings.alerts");
  assert.deepEqual(calls, [["settings.alerts", "Alerts"]]);
  assert.equal(Model.tr(null, "settings.alerts", "Alerts"), "Alerts");
  assert.equal(Model.tr(undefined, "settings.alerts"), "settings.alerts");
  assert.equal(Model.tr(null, "settings.alerts", ""), "settings.alerts");
});

test("isAuthErrorKind and isTransientErrorKind classify daemon error kinds", () => {
  assert.equal(Model.isAuthErrorKind("auth_expired"), true);
  assert.equal(Model.isAuthErrorKind("api_key_rejected"), true);
  assert.equal(Model.isAuthErrorKind("offline"), false);
  assert.equal(Model.isAuthErrorKind(""), false);

  assert.equal(Model.isTransientErrorKind("offline"), true);
  assert.equal(Model.isTransientErrorKind("unreachable"), true);
  assert.equal(Model.isTransientErrorKind("rate_limited"), true);
  assert.equal(Model.isTransientErrorKind("auth_expired"), false);
  assert.equal(Model.isTransientErrorKind("refused"), false);
});

test("describeLine and buildBarTooltip localize countdowns when strings are provided", () => {
  const dict = {
    "priority.resetsIn": "Reseta em %1",
    "priority.outIn": "Acaba em %1",
    "footer.nextUpdate": "Próxima atualização em %1",
    "settings.privacyMode": "Modo Privacidade",
  };
  const ptStrings = { t: (key, fb) => (key in dict ? dict[key] : fb) };
  const opts = { ...relative, strings: ptStrings };
  const tip = Model.buildBarTooltip(dash, new Date(NOW + 5 * MIN).toISOString(), NOW, "Omameter", opts);
  assert.ok(tip.includes("Reseta em 2h"), tip);
  assert.ok(tip.includes("Acaba em 1h 40m"), tip);
  assert.ok(tip.includes("Próxima atualização em 5m"), tip);
  assert.equal(
    Model.buildBarTooltip(dash, "", NOW, "Omameter", { ...opts, privacyMode: true }),
    "Omameter · Modo Privacidade"
  );
});

test("windowGlyph and barChipDisplay differentiate 5h, session, and weekly across full/compact/icon modes", () => {
  const fiveH = { shortTitle: "Gemini 5h", windowKind: "5h", windowTag: "5h" };
  const session = { shortTitle: "Session", windowKind: "session", windowTag: "5h" };
  const weekly = { shortTitle: "Gemini", windowKind: "weekly", windowTag: "7d" };
  const daily = { shortTitle: "Daily", windowKind: "daily", windowTag: "1d" };
  const monthly = { shortTitle: "Monthly", windowKind: "monthly", windowTag: "30d" };
  const extra = { shortTitle: "Extra", windowKind: "extra", windowTag: "$" };

  // Distinct non-empty glyphs for every window kind
  const glyphs = [fiveH, session, weekly, daily, monthly, extra].map((it) => Model.windowGlyph(it.windowKind));
  assert.ok(glyphs.every((g) => typeof g === "string" && g.length > 0));
  assert.equal(new Set(glyphs).size, glyphs.length);

  // Full mode keeps shortTitle and no glyph
  assert.deepEqual(JSON.parse(JSON.stringify(Model.barChipDisplay(fiveH, "full"))), { label: "Gemini 5h", glyph: "" });
  assert.deepEqual(JSON.parse(JSON.stringify(Model.barChipDisplay(weekly, "full"))), { label: "Gemini", glyph: "" });

  // Compact mode uses windowTag
  assert.deepEqual(JSON.parse(JSON.stringify(Model.barChipDisplay(fiveH, "compact"))), { label: "5h", glyph: "" });
  assert.deepEqual(JSON.parse(JSON.stringify(Model.barChipDisplay(session, "compact"))), { label: "5h", glyph: "" });
  assert.deepEqual(JSON.parse(JSON.stringify(Model.barChipDisplay(weekly, "compact"))), { label: "7d", glyph: "" });

  // Icon mode suppresses text labels and emits distinct glyphs for 5h, session, and weekly
  const icon5h = Model.barChipDisplay(fiveH, "icon");
  const iconSession = Model.barChipDisplay(session, "icon");
  const iconWeekly = Model.barChipDisplay(weekly, "icon");
  assert.equal(icon5h.label, "");
  assert.equal(iconSession.label, "");
  assert.equal(iconWeekly.label, "");
  assert.notEqual(icon5h.glyph, iconSession.glyph);
  assert.notEqual(icon5h.glyph, iconWeekly.glyph);
  assert.notEqual(iconSession.glyph, iconWeekly.glyph);
});

test("metricPickerLabel appends windowTag only when shortTitle omits the window", () => {
  assert.equal(Model.metricPickerLabel({ shortTitle: "Gemini", windowTag: "7d" }), "Gemini 7d");
  assert.equal(Model.metricPickerLabel({ shortTitle: "Gemini 5h", windowTag: "5h" }), "Gemini 5h");
  assert.equal(Model.metricPickerLabel({ shortTitle: "Session", windowTag: "5h" }), "Session");
  assert.equal(Model.metricPickerLabel({ shortTitle: "Weekly", windowTag: "7d" }), "Weekly");
  assert.equal(Model.metricPickerLabel({ shortTitle: "Sonnet", windowTag: "7d" }), "Sonnet 7d");
  assert.equal(Model.metricPickerLabel({ shortTitle: "Extra", windowTag: "$" }), "Extra");
});

test("buildHoverCardData groups metrics by provider with icons, compact values, and reset/burn glyphs", () => {
  const nextAt = new Date(NOW + 3 * MIN).toISOString();
  const data = Model.buildHoverCardData(dash, nextAt, NOW, "Omameter", {
    ...relative,
    displayMode: "used",
  });
  assert.equal(data.state, "metrics");
  assert.equal(data.modeTag, "Used");
  assert.equal(data.nextText, "Next 3m");
  assert.ok(data.groups.length >= 2);
  // Every group carries provider icon + name and grouped rows
  for (const grp of data.groups) {
    assert.ok(grp.icon.endsWith(".svg"));
    assert.ok(grp.providerName.length > 0);
    assert.ok(grp.rows.length >= 1);
  }
  const allRows = data.groups.flatMap((g) => g.rows);
  const burning = allRows.find((r) => r.isBurning);
  assert.ok(burning, "expected a burningFast row");
  assert.equal(burning.timeKind, "out");
  assert.equal(burning.timeGlyph, "󱐋");
  assert.equal(burning.timeText, "1h 40m");

  const resetRow = allRows.find((r) => r.timeKind === "reset");
  assert.ok(resetRow, "expected a reset row");
  assert.equal(resetRow.timeGlyph, "󰑐");
  assert.ok(resetRow.timeText.length > 0 && !resetRow.timeText.startsWith("Resets"));
});

test("buildHoverCardData handles empty, privacy, clear, and notices states cleanly", () => {
  const empty = Model.buildHoverCardData(null, "", NOW, "Click to add", relative);
  assert.equal(empty.state, "empty");
  assert.equal(empty.message, "Click to add");

  const priv = Model.buildHoverCardData(dash, "", NOW, "Empty", { ...relative, privacyMode: true });
  assert.equal(priv.state, "privacy");
  assert.equal(priv.message, "Privacy Mode");
  assert.equal(priv.groups.length, 0);

  const clearDash = {
    hasAccounts: true,
    accountCount: 2,
    bar: { privacyMode: false },
    priority: { pinned: [], runningOut: [], useItOrLoseIt: [], usedUp: [], notices: [], showsClearRow: true },
  };
  const clear = Model.buildHoverCardData(clearDash, "", NOW, "Empty", relative);
  assert.equal(clear.state, "clear");
  assert.equal(clear.message, "All Clear");

  const noticeDash = {
    hasAccounts: true,
    accountCount: 1,
    bar: { privacyMode: false },
    priority: {
      pinned: [],
      runningOut: [],
      useItOrLoseIt: [],
      usedUp: [],
      notices: [{ icon: "claude.svg", providerName: "Claude", message: "Sign in again" }],
      showsClearRow: false,
    },
  };
  const notice = Model.buildHoverCardData(noticeDash, "", NOW, "Empty", relative);
  assert.equal(notice.state, "notices");
  assert.equal(notice.notices.length, 1);
  assert.equal(notice.notices[0].icon, "claude.svg");
});



test("absolute resets, durations and auth labels follow the catalog", () => {
  const dict = {
    "time.resetsAt": "Reseta %1",
    "time.weekdaysShort": "dom,seg,ter,qua,qui,sex,sáb",
    "time.monthsShort": "jan,fev,mar,abr,mai,jun,jul,ago,set,out,nov,dez",
    "time.monthDay": "%2 de %1",
    "hover.now": "agora",
    "authMethod.oauth": "Login pelo Navegador",
  };
  const pt = { t: (key, fb) => (key in dict ? dict[key] : fb) };
  const now = Date.UTC(2026, 6, 13, 12, 0, 0); // Monday
  const tomorrow = new Date(now + 26 * 3600 * 1000).toISOString();
  const later = new Date(now + 20 * 86400 * 1000).toISOString();
  assert.equal(Model.formatResetDisplay(tomorrow, "absolute", "24h", now, "", null), "Resets Tue 14:00");
  assert.equal(Model.formatResetDisplay(tomorrow, "absolute", "24h", now, "", pt), "Reseta ter 14:00");
  assert.equal(Model.formatResetDisplay(later, "absolute", "24h", now, "", pt), "Reseta 2 de ago 12:00");
  assert.equal(Model.formatCompactResetDisplay(new Date(now - 1000).toISOString(), now, "", pt), "agora");
  assert.equal(Model.authMethodLabel("oauth", pt), "Login pelo Navegador");
  assert.equal(Model.authMethodLabel("device"), "Device Code");
});

test("metricPickerLabel trusts the daemon's titleNamesWindow flag over word checks", () => {
  assert.equal(Model.metricPickerLabel({ shortTitle: "Semanal", windowTag: "7d", titleNamesWindow: true }), "Semanal");
  assert.equal(Model.metricPickerLabel({ shortTitle: "Gemini", windowTag: "7d", titleNamesWindow: false }), "Gemini 7d");
});
