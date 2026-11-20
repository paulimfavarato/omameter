.pragma library

// Settings keys, defaults and bounds for the QML side, plus the pure rules for
// writing a change and reconciling edits made in Omarchy Setup -> Plugins.
//
// SCHEMA is a copy of `barWidget.schema` in manifest.json (the single source;
// backend/settings.py reads the manifest directly). tests/test_settings.mjs
// fails when the two drift, so a new key goes into the manifest first.

var SCHEMA = [
  { key: "defaultTab", type: "enum", options: ["priority", "all"], defaultValue: "priority" },
  { key: "maxBarItems", type: "integer", min: 0, max: 8, step: 1, defaultValue: 3 },
  { key: "alwaysShowInBar", type: "boolean", defaultValue: false },
  { key: "barLabelStyle", type: "enum", options: ["full", "compact", "icon"], defaultValue: "full" },
  { key: "barMeterStyle", type: "enum", options: ["none", "bar", "blocks", "braille"], defaultValue: "none" },
  { key: "barLayout", type: "enum", options: ["single", "stacked"], defaultValue: "single" },
  { key: "barProviderColors", type: "boolean", defaultValue: false },
  { key: "displayMode", type: "enum", options: ["remaining", "used"], defaultValue: "remaining" },
  { key: "resetDisplayMode", type: "enum", options: ["relative", "absolute"], defaultValue: "relative" },
  { key: "showPaceMarks", type: "boolean", defaultValue: false },
  { key: "warningThreshold", type: "integer", min: 20, max: 70, step: 5, defaultValue: 30 },
  { key: "criticalThreshold", type: "integer", min: 5, max: 25, step: 5, defaultValue: 10 },
  { key: "burningFastPriority", type: "boolean", defaultValue: true },
  { key: "useItOrLoseItPriority", type: "boolean", defaultValue: true },
  { key: "notifyWarning", type: "boolean", defaultValue: false },
  { key: "notifyCritical", type: "boolean", defaultValue: false },
  { key: "notifyBurningFast", type: "boolean", defaultValue: false },
  { key: "notifyUseItOrLoseIt", type: "boolean", defaultValue: false },
  { key: "notifyReset", type: "boolean", defaultValue: false },
  { key: "privacyMode", type: "boolean", defaultValue: false },
  { key: "timeFormat", type: "enum", options: ["auto", "12h", "24h"], defaultValue: "auto" },
  { key: "refreshIntervalSec", type: "integer", min: 60, max: 3600, step: 60, defaultValue: 300 },
  { key: "localTokenAnalytics", type: "boolean", defaultValue: false }
]

var FIELDS = (function() {
  var out = {}
  for (var i = 0; i < SCHEMA.length; i++) out[SCHEMA[i].key] = SCHEMA[i]
  return out
})()

var WARNING = "warningThreshold"
var CRITICAL = "criticalThreshold"
var MAX_BAR_ITEMS_FULL = 5

function maxBarItemsCap(barLabelStyle, barLayout) {
  var schemaMax = FIELDS.maxBarItems.max
  var style = String(barLabelStyle || "full")
  var layout = String(barLayout || "single")
  if (layout === "stacked" || style === "compact" || style === "icon") return schemaMax
  return Math.min(schemaMax, MAX_BAR_ITEMS_FULL)
}

function hasKey(key) {
  return Object.prototype.hasOwnProperty.call(FIELDS, key)
}

function field(key) {
  return hasKey(key) ? FIELDS[key] : null
}

function keys() {
  return SCHEMA.map(function(f) { return f.key })
}

function defaultSettings() {
  var out = {}
  for (var i = 0; i < SCHEMA.length; i++) out[SCHEMA[i].key] = SCHEMA[i].defaultValue
  return out
}

// Same coercion as backend/settings.py `_coerce`.
function coerce(f, value) {
  if (f.type === "boolean") return typeof value === "boolean" ? value : f.defaultValue
  if (f.type === "enum") return f.options.indexOf(value) >= 0 ? value : f.defaultValue
  var number
  if (typeof value === "number") number = value
  else if (typeof value === "string" && value.trim() !== "") number = Number(value)
  else return f.defaultValue
  if (!isFinite(number)) return f.defaultValue
  return Math.max(f.min, Math.min(f.max, Math.trunc(number)))
}

// ---- Linked Priority thresholds (mirror of PriorityLimits) ----

var STEP = FIELDS[WARNING].step

function snap(value, f) {
  var clamped = Math.max(f.min, Math.min(f.max, Math.trunc(Number(value))))
  return Math.round(clamped / STEP) * STEP
}

function linkLimits(warning, critical) {
  var w = snap(warning, FIELDS[WARNING])
  var c = snap(critical, FIELDS[CRITICAL])
  if (c > w - STEP) c = Math.max(FIELDS[CRITICAL].min, w - STEP)
  if (w < c + STEP) w = Math.min(FIELDS[WARNING].max, c + STEP)
  return { warning: w, critical: c }
}

function withWarning(limits, value) {
  var w = snap(value, FIELDS[WARNING])
  var c = limits.critical
  if (c > w - STEP) c = Math.max(FIELDS[CRITICAL].min, w - STEP)
  return linkLimits(w, c)
}

function withCritical(limits, value) {
  var c = snap(value, FIELDS[CRITICAL])
  var w = limits.warning
  if (w < c + STEP) w = Math.min(FIELDS[WARNING].max, c + STEP)
  return linkLimits(w, c)
}

// Every schema key, typed and bounded; unknown keys dropped (backend normalize_settings).
function normalize(raw) {
  var src = raw && typeof raw === "object" ? raw : {}
  var out = {}
  for (var i = 0; i < SCHEMA.length; i++) {
    var f = SCHEMA[i]
    out[f.key] = Object.prototype.hasOwnProperty.call(src, f.key) ? coerce(f, src[f.key]) : f.defaultValue
  }
  out.maxBarItems = Math.min(out.maxBarItems, maxBarItemsCap(out.barLabelStyle, out.barLayout))
  var lim = linkLimits(out[WARNING], out[CRITICAL])
  out[WARNING] = lim.warning
  out[CRITICAL] = lim.critical
  return out
}

// Settings after a patch; changing one threshold pushes the other (backend apply_patch).
function applyPatch(current, patch) {
  var base = normalize(current)
  var upd = {}
  for (var k in (patch || {})) if (hasKey(k)) upd[k] = patch[k]
  var hasW = upd.hasOwnProperty(WARNING)
  var hasC = upd.hasOwnProperty(CRITICAL)
  if (hasW !== hasC) {
    var cur = linkLimits(base[WARNING], base[CRITICAL])
    var key = hasW ? WARNING : CRITICAL
    var value = coerce(FIELDS[key], upd[key])
    var nxt = hasW ? withWarning(cur, value) : withCritical(cur, value)
    upd[WARNING] = nxt.warning
    upd[CRITICAL] = nxt.critical
  }
  var merged = {}
  for (var b in base) merged[b] = base[b]
  for (var u in upd) merged[u] = upd[u]
  return normalize(merged)
}

// The keys a patch touches, with the values they end up with; touching one
// threshold reports both, since the other may be pushed, and switching into
// single-row "full" style also reports maxBarItems when it gets clamped down to 5.
function normalizePatch(patch, current) {
  var base = normalize(current)
  var next = applyPatch(base, patch)
  var out = {}
  var any = false
  for (var k in (patch || {})) {
    if (!hasKey(k)) continue
    out[k] = next[k]
    any = true
  }
  if (out.hasOwnProperty(WARNING) || out.hasOwnProperty(CRITICAL)) {
    out[WARNING] = next[WARNING]
    out[CRITICAL] = next[CRITICAL]
  }
  if ((out.hasOwnProperty("barLabelStyle") || out.hasOwnProperty("barLayout")) && next.maxBarItems !== base.maxBarItems) {
    out.maxBarItems = next.maxBarItems
  }
  return any ? out : null
}

// One stepper click on a threshold (or any stepped integer) setting, direction -1 or +1.
function stepThreshold(key, current, direction) {
  var f = field(key)
  if (!f || f.type !== "integer") return current
  var step = f.step || 1
  var next = Math.round((Number(current) + step * (direction < 0 ? -1 : 1)) / step) * step
  return Math.max(f.min, Math.min(f.max, next))
}

function isEmpty(obj) {
  for (var k in (obj || {})) return false
  return true
}

function copyOf(obj) {
  var out = {}
  for (var k in (obj || {})) out[k] = obj[k]
  return out
}

// Reverse path: what to send to the daemon given the settings the widget was
// handed (shell.json, via Omarchy Setup -> Plugins).
//   lastSeen: values last seen in (or written to) shell.json, per key
//   widgetSettings: what the widget got now (null = nothing new, just flush)
//   ready: daemon is up
//   pending: changes seen while the daemon was not ready
// The first value seen for a key is only recorded (settings.json is
// authoritative at startup). A change seen while !ready waits in `pending` and
// goes out with the next ready call. Values equal to lastSeen (echoes of our
// own writes, or several widget instances handed the same settings) send nothing.
function reconcile(lastSeen, widgetSettings, ready, pending) {
  var seen = copyOf(lastSeen)
  var queued = copyOf(pending)
  if (widgetSettings && typeof widgetSettings === "object") {
    for (var i = 0; i < SCHEMA.length; i++) {
      var key = SCHEMA[i].key
      var value = widgetSettings[key]
      if (value === undefined || value === null) continue
      if (seen.hasOwnProperty(key) && seen[key] !== value) queued[key] = value
      seen[key] = value
    }
  }
  if (!ready || isEmpty(queued)) return { lastSeen: seen, patch: null, pending: queued }
  return { lastSeen: seen, patch: queued, pending: {} }
}

// Record values we are about to write to shell.json, so their echo sends nothing.
function markWritten(lastSeen, patch) {
  var seen = copyOf(lastSeen)
  for (var k in (patch || {})) if (hasKey(k)) seen[k] = patch[k]
  return seen
}
