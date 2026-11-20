.pragma library

// Pure, stateless data helpers for the Omameter Omarchy plugin.
// Kept free of Qt/QML globals so every helper is deterministic and testable.

// Settings keys, defaults and bounds live in Settings.js.

function emptyDashboard() {
  return {
    hasAccounts: false,
    accountCount: 0,
    allUsage: [],
    hiddenMetrics: [],
    priority: {
      summary: "waitingForUsage",
      showsClearRow: false,
      pinned: [],
      runningOut: [],
      useItOrLoseIt: [],
      notices: [],
      usedUp: []
    },
    bar: {
      iconOnly: true,
      items: [],
      allItems: [],
      overflow: 0,
      alarming: false,
      privacyMode: false
    }
  }
}

function clamp(value, lo, hi) {
  var n = Number(value)
  if (!isFinite(n)) return lo
  if (n < lo) return lo
  if (n > hi) return hi
  return n
}

function cleanText(value, maxLength) {
  var text = value === undefined || value === null ? "" : String(value)
  text = text.replace(/[\t\r]/g, " ")
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g, "")
    .replace(/[\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, "")
  var limit = Number(maxLength) || 1024
  if (text.length <= limit) return text
  var end = limit - 1
  var code = text.charCodeAt(end - 1)
  if (code >= 0xd800 && code <= 0xdbff) end -= 1
  return text.slice(0, end) + "…"
}

// Translate `key` through an i18n strings object (anything with `t(key, fb)`).
// Before the strings object exists, fall back to `fb`, then to the key itself.
function tr(strings, key, fb) {
  return strings ? strings.t(key, fb) : (fb || key)
}

// Sanitize untrusted strings before passing them to any Omarchy component
// that may evaluate Qt Text.AutoText (e.g. tooltips). Strips control chars
// and replaces <, >, & so HTML/rich-text entities can never trigger.
function autoTextSafe(value, keepNewlines) {
  var text = cleanText(value, 1500)
  if (!keepNewlines) {
    text = text.replace(/[\n\u2028\u2029]/g, " ")
  } else {
    text = text.replace(/[\u2028\u2029]/g, "\n")
  }
  return text
    .replace(/&/g, "＆")
    .replace(/</g, "‹")
    .replace(/>/g, "›")
}

// Parse key = "#rrggbb" pairs from Omarchy's active theme colors.toml.
function parseColorsToml(raw) {
  var out = {}
  var lines = String(raw || "").split(/\r?\n/)
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].replace(/#[^"']*$/, "").trim()
    if (!line) continue
    var m = line.match(/^([A-Za-z0-9_.-]+)\s*=\s*["']?(#[0-9A-Fa-f]{6})["']?/)
    if (m) out[m[1].toLowerCase()] = m[2]
  }
  return out
}

function parseHexRgb(hex) {
  var s = String(hex || "").trim()
  var m = s.match(/^#([0-9A-Fa-f]{6})$/)
  if (!m) return null
  var h = m[1]
  return {
    r: parseInt(h.slice(0, 2), 16),
    g: parseInt(h.slice(2, 4), 16),
    b: parseInt(h.slice(4, 6), 16)
  }
}

function isLightHex(hex) {
  var rgb = parseHexRgb(hex)
  if (!rgb) return false
  var luma = (0.2126 * rgb.r + 0.7152 * rgb.g + 0.0722 * rgb.b) / 255
  return luma > 0.55
}

// Derive semantic colors cleanly from the active Omarchy theme palette.
function pickThemePalette(themeColors, foreground, background, accent, urgent) {
  var t = themeColors || {}
  var bgHex = t.background || String(background || "#101315")
  var light = isLightHex(bgHex)

  var warning = t.orange || t.amber || t.color3 || t.yellow || (light ? "#c96a00" : "#e59b3c")
  var critical = String(urgent || "") || t.red || t.color1 || (light ? "#c62828" : "#e05252")
  var good = t.green || t.color2 || String(accent || "") || (light ? "#2e7d32" : "#5cb874")

  return {
    normal: String(foreground || "#cacccc"),
    accent: String(accent || foreground || "#cacccc"),
    warning: warning,
    critical: critical,
    good: good,
    isLight: light
  }
}

function severityColor(severity, palette, fallbackFg, fallbackUrgent) {
  var sev = String(severity || "normal")
  var p = palette || {}
  if (sev === "critical") return p.critical || fallbackUrgent || "#e05252"
  if (sev === "warning") return p.warning || "#e59b3c"
  return fallbackFg || p.normal || "#cacccc"
}

// ---------------------------------------------------------------------------
// Account naming: the one privacy rule and the one "provider · account" rule.
// ---------------------------------------------------------------------------

// opts: { privacyMode, isAggregate }. An aggregate name ("2 Accounts" on a
// unified card) identifies nobody, so privacy leaves it alone.
function displayAccountName(name, opts) {
  var o = opts || {}
  var clean = cleanText(name, 120).trim()
  if (!o.privacyMode || o.isAggregate) return clean
  if (!clean) return "•••"
  return clean.charAt(0) + "•••"
}

// "Provider · Account", or just "Provider" when the account name repeats it,
// ignoring case (an account named after its provider reveals nothing, so the
// comparison uses the real name and privacy applies only to what is shown).
// opts as displayAccountName.
function accountHeader(providerName, accountName, opts) {
  var prov = cleanText(providerName, 120).trim()
  var raw = cleanText(accountName, 120).trim()
  if (!raw || raw.toLowerCase() === prov.toLowerCase()) return prov
  return prov + " · " + displayAccountName(accountName, opts)
}

function formatDurationCompact(seconds) {
  var secs = Number(seconds)
  if (!isFinite(secs) || secs <= 0) return "now"
  if (secs < 60) return Math.max(1, Math.round(secs)) + "s"
  var mins = Math.ceil(secs / 60)
  if (mins < 60) return mins + "m"
  var hours = Math.floor(mins / 60)
  var remMins = mins % 60
  if (hours < 24) return remMins > 0 ? (hours + "h " + remMins + "m") : (hours + "h")
  var days = Math.floor(hours / 24)
  var remHours = hours % 24
  return remHours > 0 ? (days + "d " + remHours + "h") : (days + "d")
}

function _pad2(n) {
  return n < 10 ? ("0" + n) : String(n)
}

function _formatClockTime(dt, timeFormat) {
  var h = dt.getHours()
  var m = _pad2(dt.getMinutes())
  var tf = String(timeFormat || "auto")
  if (tf === "12h") {
    var suffix = h >= 12 ? "PM" : "AM"
    var h12 = h % 12
    if (h12 === 0) h12 = 12
    return h12 + ":" + m + " " + suffix
  }
  return _pad2(h) + ":" + m
}

function isAuthErrorKind(kind) {
  var k = String(kind || "")
  return k === "auth_expired" || k === "api_key_rejected"
}

function isTransientErrorKind(kind) {
  var k = String(kind || "")
  return k === "offline" || k === "unreachable" || k === "rate_limited"
}

function _trArg(strings, key, fb, arg1) {
  return tr(strings, key, fb).replace("%1", String(arg1))
}

// Weekday/month abbreviations come from the catalog as comma lists (index 0 = Sunday / January).
function _nameList(strings, key, fb, index) {
  var names = String(tr(strings, key, fb)).split(",")
  var fallback = String(fb).split(",")
  return String(names[index] || fallback[index] || "").trim()
}

// The "when" of an absolute reset: "14:30", "Tue 14:30" within a week, "Nov 21 14:30" beyond.
function formatAbsoluteWhen(target, timeFormat, nowMs, strings) {
  var now = isFinite(Number(nowMs)) ? Number(nowMs) : Date.now()
  var nowDate = new Date(now)
  var deltaSec = (target.getTime() - now) / 1000
  var timeStr = _formatClockTime(target, timeFormat)
  var sameDay = target.getFullYear() === nowDate.getFullYear()
    && target.getMonth() === nowDate.getMonth()
    && target.getDate() === nowDate.getDate()
  if (sameDay) return timeStr
  if (deltaSec > 0 && deltaSec < 7 * 86400) {
    return _nameList(strings, "time.weekdaysShort", "Sun,Mon,Tue,Wed,Thu,Fri,Sat", target.getDay()) + " " + timeStr
  }
  var month = _nameList(strings, "time.monthsShort", "Jan,Feb,Mar,Apr,May,Jun,Jul,Aug,Sep,Oct,Nov,Dec", target.getMonth())
  return tr(strings, "time.monthDay", "%1 %2").replace("%1", month).replace("%2", String(target.getDate())) + " " + timeStr
}

function formatResetDisplay(resetsAtIso, resetDisplayMode, timeFormat, nowMs, fallbackText, strings) {
  var fb = String(fallbackText || "")
  if (!resetsAtIso) return fb
  var target = new Date(String(resetsAtIso))
  var targetMs = target.getTime()
  if (!isFinite(targetMs)) return fb
  var now = isFinite(Number(nowMs)) ? Number(nowMs) : Date.now()
  var deltaSec = (targetMs - now) / 1000
  var mode = String(resetDisplayMode || "relative")

  if (mode === "exact" || mode === "absolute") {
    return _trArg(strings, "time.resetsAt", "Resets %1", formatAbsoluteWhen(target, timeFormat, now, strings))
  }

  if (deltaSec <= 0) return tr(strings, "bar.resetsNow", "Resets now")
  return _trArg(strings, "priority.resetsIn", "Resets in %1", formatDurationCompact(deltaSec))
}

// Seconds from nowMs (or Date.now()) until an ISO timestamp; NaN when absent/unparseable.
function _secondsUntil(iso, nowMs) {
  if (!iso) return NaN
  var targetMs = new Date(String(iso)).getTime()
  if (!isFinite(targetMs)) return NaN
  var now = isFinite(Number(nowMs)) ? Number(nowMs) : Date.now()
  return (targetMs - now) / 1000
}

function formatCompactResetDisplay(resetsAtIso, nowMs, fallbackText, strings) {
  var deltaSec = _secondsUntil(resetsAtIso, nowMs)
  if (isNaN(deltaSec)) return String(fallbackText || "")
  if (deltaSec <= 0) return tr(strings, "hover.now", "now")
  return formatDurationCompact(deltaSec)
}

// Live "Out in 1h 40m" countdown to the instant a line runs dry at its current pace.
function formatOutIn(runoutAtIso, nowMs, strings) {
  var deltaSec = _secondsUntil(runoutAtIso, nowMs)
  if (isNaN(deltaSec)) return ""
  if (deltaSec <= 0) return tr(strings, "priority.outNow", "Out now")
  return _trArg(strings, "priority.outIn", "Out in %1", formatDurationCompact(deltaSec))
}

function formatNextUpdate(isoString, nowMs, strings) {
  var deltaSec = _secondsUntil(isoString, nowMs)
  if (isNaN(deltaSec)) return ""
  if (deltaSec <= 5) return tr(strings, "time.soon", "soon")
  return formatDurationCompact(deltaSec)
}

function formatResetExpiry(isoString, nowMs, strings) {
  var deltaSec = _secondsUntil(isoString, nowMs)
  if (isNaN(deltaSec)) return ""
  if (deltaSec <= 0) return tr(strings, "resets.expired", "expired")
  return formatDurationCompact(deltaSec)
}

function isResetExpiringSoon(isoString, nowMs) {
  var deltaSec = _secondsUntil(isoString, nowMs)
  return deltaSec > 0 && deltaSec <= 7 * 24 * 3600
}

function formatCompactNumber(val) {
  var n = Number(val)
  if (!isFinite(n)) return "0"
  var av = Math.abs(n)
  var sign = n < 0 ? "-" : ""
  function trim1(x) {
    return x.toFixed(1).replace(/\.0$/, "")
  }
  if (av >= 1e9) return sign + trim1(av / 1e9) + "B"
  if (av >= 1e6) return sign + trim1(av / 1e6) + "M"
  if (av >= 1e3) return sign + trim1(av / 1e3) + "K"
  return sign + trim1(av)
}

function currencySymbol(code) {
  var c = String(code || "USD").toUpperCase()
  var map = {
    USD: "$",
    EUR: "€",
    GBP: "£",
    JPY: "¥",
    BRL: "R$",
    CAD: "CA$",
    AUD: "A$",
    CHF: "CHF ",
    INR: "₹"
  }
  return map[c] || (c + " ")
}

function formatCurrency(val, code) {
  var n = Number(val)
  if (!isFinite(n)) return ""
  var sym = currencySymbol(code)
  var av = Math.abs(n)
  var sign = n < 0 ? "-" : ""
  if (av >= 1000) return sign + sym + formatCompactNumber(av)
  var cents = Math.round(av * 100)
  if (cents % 100 === 0) return sign + sym + Math.round(cents / 100)
  return sign + sym + av.toFixed(2)
}

// ---------------------------------------------------------------------------
// describeLine: everything one metric line says, in one place.
// `line` is a dashboard line view, priority row or bar item from the daemon;
// opts: { resetDisplayMode, timeFormat, nowMs, privacyMode, palette, foreground, urgent }.
// Time phrases are formatted here, live from nowMs: the daemon sends instants
// (resetsAt, runoutAt) and kinds (noteKind, paceInlineKind), never durations.
// ---------------------------------------------------------------------------

function _noteText(kind, live) {
  if (kind === "amount") return live.amountText
  if (kind === "resetCompact") return live.compactResetText
  if (kind === "reset") return live.resetText
  if (kind === "resetRelative") return live.relativeResetText
  if (kind === "outIn") return live.outInText
  return ""
}

function describeLine(line, opts) {
  var l = line || {}
  var o = opts || {}
  var nowMs = isFinite(Number(o.nowMs)) ? Number(o.nowMs) : Date.now()
  var timeFormat = o.timeFormat || "auto"
  var resetsAt = l.resetsAt ? String(l.resetsAt) : ""
  var nameOpts = { privacyMode: !!o.privacyMode, isAggregate: !!l.isAggregateName }
  var isBounded = !!l.isBounded
  var severity = String(l.prioritySeverity || l.severity || "normal")
  var hasPaceMark = l.paceMark !== null && l.paceMark !== undefined && l.paceMark !== "" && isFinite(Number(l.paceMark))

  var live = {
    amountText: cleanText(l.amountText, 120),
    resetText: resetsAt ? formatResetDisplay(resetsAt, o.resetDisplayMode || "relative", timeFormat, nowMs, "", o.strings) : "",
    relativeResetText: resetsAt ? formatResetDisplay(resetsAt, "relative", timeFormat, nowMs, "", o.strings) : "",
    compactResetText: resetsAt ? formatCompactResetDisplay(resetsAt, nowMs, "", o.strings) : "",
    outInText: l.runoutAt ? formatOutIn(l.runoutAt, nowMs, o.strings) : ""
  }

  var paceTooltip = cleanText(l.paceTooltip, 120)
  var metaText = ""
  if (isBounded) {
    if (l.paceInlineKind === "outIn") metaText = live.outInText
    else if (l.paceInlineKind === "pace") metaText = paceTooltip
  } else {
    metaText = cleanText(l.subtitle, 160)
  }

  return {
    title: cleanText(l.title, 120),
    shortTitle: cleanText(l.shortTitle || l.title, 60),
    amountText: live.amountText,
    trayValue: cleanText(l.trayValue, 40),
    resetText: live.resetText,
    noteText: _noteText(String(l.noteKind || ""), live),
    metaText: metaText,
    paceTip: autoTextSafe(paceTooltip, false),
    header: accountHeader(l.providerName, l.accountName, nameOpts),
    accountLabel: displayAccountName(l.accountName, nameOpts),
    severity: severity,
    color: severityColor(severity, o.palette, o.foreground, o.urgent),
    isBounded: isBounded,
    fillFraction: isFinite(Number(l.fillFraction)) ? clamp(Number(l.fillFraction), 0, 1) : 0,
    hasPaceMark: hasPaceMark,
    paceFraction: hasPaceMark ? clamp(Number(l.paceMark), 0, 1) : 0
  }
}

// opts: { privacyMode, resetDisplayMode, timeFormat, nowMs, strings } (as describeLine).
function formatSummaryText(dashboard, opts) {
  var o = opts || {}
  if (!dashboard || !dashboard.hasAccounts) return tr(o.strings, "summary.noAccounts", "Omameter: No accounts configured.")
  var lines = [tr(o.strings, "summary.title", "Omameter Summary")]
  var cards = dashboard.allUsage || []
  for (var i = 0; i < cards.length; i++) {
    var card = cards[i]
    var accounts = card.accounts || []
    for (var j = 0; j < accounts.length; j++) {
      var acc = accounts[j]
      var header = accountHeader(card.displayName, acc.accountName, { privacyMode: !!o.privacyMode, isAggregate: !!acc.isUnified })
      var plan = acc.planMultiple ? tr(o.strings, "card.multiplePlans", "Multiple") : cleanText(acc.plan, 60)
      if (plan) header += " (" + plan + ")"
      lines.push("")
      lines.push(header)
      if (acc.error) {
        lines.push("  ! " + cleanText(acc.error, 200))
      } else if (acc.notice) {
        lines.push("  i " + cleanText(acc.notice, 200))
      }
      var metrics = acc.lines || []
      for (var k = 0; k < metrics.length; k++) {
        var d = describeLine(metrics[k], o)
        var row = "  - " + d.title + ": " + d.amountText
        if (d.resetText) row += " (" + d.resetText + ")"
        lines.push(row)
      }
    }
  }
  return lines.join("\n")
}

// Nerd Font Material Design glyphs distinguishing 5h, session, daily, weekly, monthly, and extra limits.
function windowGlyph(windowKind) {
  var k = String(windowKind || "")
  if (k === "5h") return "󰔛"
  if (k === "session") return "󱐋"
  if (k === "weekly") return "󰨳"
  if (k === "daily") return "󰃶"
  if (k === "monthly") return "󰸗"
  if (k === "extra") return "󰇁"
  return ""
}

// Compute what a top-bar chip renders next to the provider icon:
//   "full"    -> { label: shortTitle, glyph: "" }
//   "compact" -> { label: windowTag || shortTitle, glyph: "" }
//   "icon"    -> { label: "", glyph: windowGlyph(windowKind) }
function barChipDisplay(item, barLabelStyle) {
  var it = item || {}
  var style = String(barLabelStyle || "full")
  var shortTitle = cleanText(it.shortTitle || it.title, 60)
  var tag = cleanText(it.windowTag, 16)
  if (style === "icon") {
    return { label: "", glyph: windowGlyph(it.windowKind) }
  }
  if (style === "compact") {
    return { label: tag || shortTitle, glyph: "" }
  }
  return { label: shortTitle, glyph: "" }
}

// Disambiguate metrics whose shortTitle omits the window (e.g. Antigravity "Gemini" weekly -> "Gemini 7d").
function metricPickerLabel(item) {
  var it = item || {}
  var base = cleanText(it.shortTitle || it.title, 28)
  var tag = cleanText(it.windowTag, 12)
  if (!base || !tag) return base
  // The daemon decides from the untranslated title; the word checks below cover items without the flag.
  if (typeof it.titleNamesWindow === "boolean") return it.titleNamesWindow ? base : base + " " + tag
  var lower = base.toLowerCase()
  var tagLower = tag.toLowerCase()
  if (lower.indexOf(tagLower) >= 0) return base
  if (tagLower === "7d" && lower.indexOf("week") >= 0) return base
  if (tagLower === "5h" && lower.indexOf("session") >= 0) return base
  if (tagLower === "1d" && (lower.indexOf("day") >= 0 || lower.indexOf("daily") >= 0 || lower.indexOf("today") >= 0)) return base
  if (tagLower === "30d" && lower.indexOf("month") >= 0) return base
  if (tagLower === "$" && (lower.indexOf("extra") >= 0 || lower.indexOf("credit") >= 0 || lower.indexOf("balance") >= 0 || lower.indexOf("spend") >= 0)) return base
  return base + " " + tag
}

// opts: { privacyMode, resetDisplayMode, timeFormat, strings } (as describeLine; nowMs is the argument).
function buildBarTooltip(dashboard, nextRefreshAt, nowMs, emptyFallback, opts) {
  var o = opts || {}
  var fallback = autoTextSafe(emptyFallback || tr(o.strings, "bar.tooltipEmpty", "Omameter — Click to manage accounts"), false)
  if (!dashboard || !dashboard.hasAccounts) return fallback

  var bar = dashboard.bar || {}
  if (o.privacyMode || bar.privacyMode) {
    return autoTextSafe("Omameter · " + tr(o.strings, "settings.privacyMode", "Privacy Mode"), false)
  }
  var lineOpts = { resetDisplayMode: o.resetDisplayMode, timeFormat: o.timeFormat, nowMs: nowMs, strings: o.strings }

  var lines = ["Omameter"]
  var prio = dashboard.priority || {}
  var items = []
  var sections = [prio.pinned || [], prio.runningOut || [], prio.useItOrLoseIt || [], prio.usedUp || []]
  var seen = {}
  for (var s = 0; s < sections.length; s++) {
    for (var i = 0; i < sections[s].length; i++) {
      var it = sections[s][i]
      if (!it || seen[it.metricId]) continue
      seen[it.metricId] = true
      items.push(it)
      if (items.length >= 6) break
    }
    if (items.length >= 6) break
  }

  if (items.length > 0) {
    for (var j = 0; j < items.length; j++) {
      var row = items[j]
      var d = describeLine(row, lineOpts)
      var label = cleanText(row.providerName, 60) + (row.badge ? " [" + cleanText(row.badge, 8) + "]" : "") + " · " + d.shortTitle
      var val = d.amountText || d.trayValue
      var suffix = d.noteText ? " · " + d.noteText : ""
      lines.push(label + ": " + val + suffix)
    }
  } else {
    var notices = prio.notices || []
    if (notices.length > 0) {
      lines.push(cleanText(notices[0].providerName, 60) + ": " + cleanText(notices[0].message, 200))
    } else if (prio.showsClearRow) {
      lines.push(tr(o.strings, "priority.allClear", "All Clear"))
    } else {
      lines.push(_trArg(o.strings, "bar.connectedCount", "%1 connected", dashboard.accountCount || 0))
    }
  }

  var nextStr = formatNextUpdate(nextRefreshAt, nowMs, o.strings)
  if (nextStr) lines.push(_trArg(o.strings, "footer.nextUpdate", "Next update in %1", nextStr))
  return autoTextSafe(lines.join("\n"), true)
}

function _compactAbsoluteReset(resetsAtIso, timeFormat, nowMs, strings) {
  var target = new Date(String(resetsAtIso || ""))
  if (!isFinite(target.getTime())) return ""
  return formatAbsoluteWhen(target, timeFormat, nowMs, strings)
}

// Structured data for the rich top-bar hover card (ui/BarHoverCard.qml):
// grouped by provider/account, with compact column values and reset/burn glyphs.
// opts: { displayMode, resetDisplayMode, timeFormat, privacyMode, palette, foreground, urgent, strings, maxItems }.
function buildHoverCardData(dashboard, nextRefreshAt, nowMs, emptyFallback, opts) {
  var o = opts || {}
  var now = isFinite(Number(nowMs)) ? Number(nowMs) : Date.now()
  var nextRaw = formatNextUpdate(nextRefreshAt, now, o.strings)
  var nextText = nextRaw ? _trArg(o.strings, "hover.nextIn", "Next %1", nextRaw) : ""
  var fallbackMsg = cleanText(emptyFallback || tr(o.strings, "bar.tooltipEmpty", "Omameter — Click to manage accounts"), 140)

  if (!dashboard || !dashboard.hasAccounts) {
    return {
      state: "empty",
      title: tr(o.strings, "app.title", "Omameter"),
      modeTag: "",
      nextText: "",
      message: fallbackMsg,
      groups: [],
      notices: []
    }
  }

  var bar = dashboard.bar || {}
  if (o.privacyMode || bar.privacyMode) {
    return {
      state: "privacy",
      title: tr(o.strings, "app.title", "Omameter"),
      modeTag: "",
      nextText: nextText,
      message: tr(o.strings, "settings.privacyMode", "Privacy Mode"),
      groups: [],
      notices: []
    }
  }

  var prio = dashboard.priority || {}
  var maxItems = (isFinite(Number(o.maxItems)) && Number(o.maxItems) > 0) ? Math.floor(Number(o.maxItems)) : 10
  var items = []
  var sections = [prio.pinned || [], prio.runningOut || [], prio.useItOrLoseIt || [], prio.usedUp || []]
  var seen = {}
  for (var s = 0; s < sections.length; s++) {
    for (var i = 0; i < sections[s].length; i++) {
      var it = sections[s][i]
      if (!it || seen[it.metricId]) continue
      seen[it.metricId] = true
      items.push(it)
      if (items.length >= maxItems) break
    }
    if (items.length >= maxItems) break
  }

  var lineOpts = {
    resetDisplayMode: o.resetDisplayMode || "relative",
    timeFormat: o.timeFormat || "auto",
    nowMs: now,
    privacyMode: false,
    palette: o.palette,
    foreground: o.foreground,
    urgent: o.urgent,
    strings: o.strings
  }

  var groups = []
  var groupMap = {}
  var nowLabel = tr(o.strings, "hover.now", "now")
  var resetMode = String(o.resetDisplayMode || "relative")
  var timeFormat = String(o.timeFormat || "auto")

  for (var j = 0; j < items.length; j++) {
    var row = items[j]
    var d = describeLine(row, lineOpts)
    var gkey = String(row.cardId || ((row.kind || "") + "|" + (row.badge || "") + "|" + (row.providerName || "")))
    var grp = groupMap[gkey]
    if (!grp) {
      grp = {
        groupKey: gkey,
        kind: String(row.kind || ""),
        icon: String(row.icon || "omameter.svg"),
        providerName: cleanText(row.providerName, 60),
        badge: cleanText(row.badge, 8),
        isHub: !!row.isHub,
        rows: []
      }
      groupMap[gkey] = grp
      groups.push(grp)
    }

    var nk = String(row.noteKind || "")
    var timeKind = ""
    var timeGlyph = ""
    var timeText = ""
    if (nk === "outIn" && row.runoutAt) {
      var outSec = _secondsUntil(row.runoutAt, now)
      if (!isNaN(outSec)) {
        timeKind = "out"
        timeGlyph = "󱐋"
        timeText = outSec <= 0 ? nowLabel : formatDurationCompact(outSec)
      }
    } else if ((nk === "reset" || nk === "resetRelative" || nk === "resetCompact") && row.resetsAt) {
      var resSec = _secondsUntil(row.resetsAt, now)
      if (!isNaN(resSec)) {
        timeKind = "reset"
        timeGlyph = "󰑐"
        if (nk === "reset" && (resetMode === "exact" || resetMode === "absolute")) {
          timeText = _compactAbsoluteReset(row.resetsAt, timeFormat, now, o.strings)
        } else {
          timeText = resSec <= 0 ? nowLabel : formatDurationCompact(resSec)
        }
      }
    } else if (nk === "amount") {
      timeKind = "amount"
      timeText = d.amountText
    }

    var valText = cleanText(row.trayValue, 24) || d.amountText
    grp.rows.push({
      metricId: String(row.metricId || ""),
      label: metricPickerLabel(row),
      value: valText,
      isBounded: d.isBounded,
      fillFraction: d.fillFraction,
      severity: d.severity,
      color: d.color,
      timeKind: timeKind,
      timeGlyph: timeGlyph,
      timeText: timeText,
      isBurning: timeKind === "out"
    })
  }

  var rawNotices = prio.notices || []
  var notices = []
  for (var n = 0; n < rawNotices.length && notices.length < 3; n++) {
    var no = rawNotices[n]
    if (!no) continue
    notices.push({
      icon: String(no.icon || "omameter.svg"),
      providerName: cleanText(no.providerName, 40),
      message: cleanText(no.message, 120)
    })
  }

  var modeTag = ""
  if (groups.length > 0) {
    modeTag = String(o.displayMode || "remaining") === "used"
      ? tr(o.strings, "settings.used", "Used")
      : tr(o.strings, "settings.remainingShort", "Left")
  }

  var state = "metrics"
  var message = ""
  if (groups.length === 0) {
    if (notices.length > 0) {
      state = "notices"
    } else if (prio.showsClearRow) {
      state = "clear"
      message = tr(o.strings, "priority.allClear", "All Clear")
    } else {
      state = "summary"
      var count = Number(dashboard.accountCount || 0)
      message = count > 0 ? (count + " " + tr(o.strings, "nav.accounts", "Accounts")) : tr(o.strings, "priority.waiting", "Checking usage…")
    }
  }

  return {
    state: state,
    title: tr(o.strings, "app.title", "Omameter"),
    modeTag: modeTag,
    nextText: nextText,
    message: message,
    groups: groups,
    notices: notices
  }
}

function authMethodLabel(method, strings) {
  var m = String(method || "")
  if (m === "oauth") return tr(strings, "authMethod.oauth", "Browser Sign-In")
  if (m === "device") return tr(strings, "authMethod.device", "Device Code")
  if (m === "api_key") return tr(strings, "authMethod.apiKey", "API Key")
  if (m === "org_token") return tr(strings, "authMethod.orgToken", "Org Token")
  if (m === "hub") return tr(strings, "authMethod.hub", "CLIProxyAPI Hub")
  return m
}

function isSafeExternalUrl(url) {
  var s = String(url || "").trim()
  return /^https?:\/\/[^\s]+$/i.test(s)
}

function safeProviderCatalog(catalog) {
  if (Array.isArray(catalog) && catalog.length > 0) return catalog
  // Static fallback before daemon emits first state
  return [
    { id: "claude", displayName: "Claude", icon: "claude.svg", authMethods: ["oauth"], keyPage: "", unifiable: true },
    { id: "codex", displayName: "ChatGPT", icon: "codex.svg", authMethods: ["oauth", "device"], keyPage: "", unifiable: true },
    { id: "cursor", displayName: "Cursor", icon: "cursor.svg", authMethods: ["oauth"], keyPage: "", unifiable: false },
    { id: "amp", displayName: "Amp", icon: "amp.svg", authMethods: ["device"], keyPage: "", unifiable: false },
    { id: "antigravity", displayName: "Antigravity", icon: "antigravity.svg", authMethods: ["oauth"], keyPage: "", unifiable: false },
    { id: "copilot", displayName: "Copilot", icon: "copilot.svg", authMethods: ["device", "org_token"], keyPage: "", unifiable: false },
    { id: "devin", displayName: "Devin", icon: "devin.svg", authMethods: ["oauth"], keyPage: "", unifiable: false },
    { id: "grok", displayName: "Grok", icon: "grok.svg", authMethods: ["device"], keyPage: "", unifiable: false },
    { id: "muse", displayName: "Muse Code", icon: "muse.svg", authMethods: ["device"], keyPage: "", unifiable: false },
    { id: "ollama", displayName: "Ollama", icon: "ollama.svg", authMethods: ["api_key"], keyPage: "https://ollama.com/settings/keys", unifiable: false },
    { id: "opencode", displayName: "OpenCode", icon: "opencode.svg", authMethods: ["device", "api_key"], keyPage: "", unifiable: false },
    { id: "openrouter", displayName: "OpenRouter", icon: "openrouter.svg", authMethods: ["api_key"], keyPage: "https://openrouter.ai/settings/management-keys", unifiable: false },
    { id: "zai", displayName: "Z.ai", icon: "zai.svg", authMethods: ["api_key"], keyPage: "https://z.ai/manage-apikey/apikey-list", unifiable: false }
  ]
}
