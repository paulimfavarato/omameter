import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io

// Standalone Omameter Analytics floating window (Phase 1 Quota History + Phase 2 Local Token Analytics).
// Runs in its own Quickshell instance (`bin/omameter-analytics`) so omarchy-shell uses 0 MB when closed.
ShellRoot {
  id: appRoot

  // ---- Paths ----
  readonly property string homeDir: Quickshell.env("HOME") || ""
  readonly property string stateHome: Quickshell.env("XDG_STATE_HOME") || (homeDir + "/.local/state")
  readonly property string configHome: Quickshell.env("XDG_CONFIG_HOME") || (homeDir + "/.config")
  readonly property string pluginDir: {
    var envDir = String(Quickshell.env("OMAMETER_PLUGIN_DIR") || "").trim().replace(/\/$/, "")
    if (envDir !== "") return envDir
    return Qt.resolvedUrl(".").toString().replace(/^file:\/\//, "").replace(/\/analytics\/?$/, "").replace(/\/$/, "")
  }
  readonly property string cliPath: pluginDir + "/backend/analytics/cli.py"
  // Omarchy stores active theme files under ~/.local/state/omarchy/current/theme/ (legacy: ~/.config/omarchy/current/theme/)
  readonly property string stateThemeDir: stateHome + "/omarchy/current/theme"
  readonly property string legacyThemeDir: configHome + "/omarchy/current/theme"
  readonly property string shellJsonPath: configHome + "/omarchy/shell.json"
  readonly property string userShellTomlPath: configHome + "/omarchy/shell.toml"

  function cleanText(raw, maxLen) {
    var s = String(raw === null || raw === undefined ? "" : raw).replace(/[\u0000-\u001f\u007f-\u009f]/g, " ")
    var lim = Number(maxLen || 220)
    return s.length > lim ? s.slice(0, lim) : s
  }

  function parseColorsToml(raw) {
    var out = {}
    var lines = String(raw || "").split(/\r?\n/)
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i].replace(/#[^"']*$/, "").trim()
      if (!ln) continue
      var m = /^([A-Za-z0-9_.-]+)\s*=\s*["']?([^"']+)["']?/.exec(ln)
      if (m) out[m[1].toLowerCase()] = m[2].trim()
    }
    return out
  }

  function parseShellToml(raw) {
    var out = {}
    var section = ""
    var lines = String(raw || "").split(/\r?\n/)
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i].trim()
      if (!ln || ln.charAt(0) === "#") continue
      var sec = /^\[([A-Za-z0-9_.-]+)\]/.exec(ln)
      if (sec) {
        section = sec[1].toLowerCase()
        continue
      }
      var kv = /^([A-Za-z0-9_.-]+)\s*=\s*(.+)$/.exec(ln)
      if (!kv) continue
      var key = (section ? (section + ".") : "") + kv[1].toLowerCase()
      var val = kv[2].trim()
      if ((val.charAt(0) === "\"" && val.indexOf("\"", 1) > 0)) {
        val = val.slice(1, val.indexOf("\"", 1))
      } else if ((val.charAt(0) === "'" && val.indexOf("'", 1) > 0)) {
        val = val.slice(1, val.indexOf("'", 1))
      } else {
        val = val.replace(/\s+#.*$/, "").trim()
      }
      out[key] = val
    }
    return out
  }

  // ---- Omarchy Native Theme Palette (live from ~/.local/state/omarchy/current/theme/) ----
  property var rawTheme: ({})
  property var themeShellValues: ({})
  property var userShellValues: ({})
  property var shellValues: ({})
  // Follows Hyprland decoration:rounding (0 = sharp Omarchy default, N>0 = max(2, round(N/2)))
  property int cornerRadius: 0

  function _mergeShellValues() {
    var merged = {}
    for (var tk in themeShellValues) merged[tk] = themeShellValues[tk]
    for (var uk in userShellValues) merged[uk] = userShellValues[uk]
    shellValues = merged
  }

  function _col(val, fb) {
    var s = String(val || "").trim()
    return /^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(s) ? s : fb
  }
  function _shellColor(key, fb) {
    var v = shellValues[key]
    if (typeof v !== "string" || v.length === 0) return fb
    var seen = {}
    var cur = v.trim()
    while (cur && shellValues[cur.toLowerCase()] && !seen[cur.toLowerCase()]) {
      seen[cur.toLowerCase()] = true
      cur = String(shellValues[cur.toLowerCase()] || "").trim()
    }
    var tok = cur.split(/\s+/)[0] || ""
    return /^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(tok) ? tok : fb
  }
  function _shellAlpha(key, fb) {
    var v = shellValues[key]
    if (v === undefined || v === null || v === "") return fb
    var n = Number(v)
    return isFinite(n) ? Math.max(0, Math.min(1, n)) : fb
  }
  function _isLight(c) {
    return (0.299 * c.r + 0.587 * c.g + 0.114 * c.b) > 0.55
  }
  function radiusCap(cap) {
    return cornerRadius > 0 ? Math.min(cornerRadius, Number(cap || cornerRadius)) : 0
  }

  readonly property color baseBg: _col(rawTheme.background, "#101315")
  readonly property color baseFg: _col(rawTheme.foreground, "#cacccc")
  readonly property color baseAccent: _col(rawTheme.accent || rawTheme.blue || rawTheme.color4, "#7daea3")
  readonly property color bg: _shellColor("popups.background", baseBg)
  readonly property color fg: _shellColor("popups.text", baseFg)
  readonly property color accent: _shellColor("menu.selected-text", baseAccent)
  readonly property color goodColor: _col(rawTheme.green || rawTheme.color2, _isLight(bg) ? "#2e7d32" : "#a9b665")
  readonly property color warningColor: _col(rawTheme.orange || rawTheme.yellow || rawTheme.color3, _isLight(bg) ? "#c96a00" : "#d8a657")
  readonly property color criticalColor: _col(rawTheme.red || rawTheme.color1, _isLight(bg) ? "#c62828" : "#ea6962")

  // Exact Omarchy [controls] chrome from shell.toml (normal / hover-cursor / selected)
  readonly property color _ctrlNormalBase: _shellColor("controls.normal-color", fg)
  readonly property color _ctrlHoverBase: _shellColor("controls.hover-cursor-color", fg)
  readonly property color _ctrlSelectedBase: _shellColor("controls.selected-color", fg)
  readonly property color _ctrlNormalBorderBase: _shellColor("controls.normal-border", fg)
  readonly property color _ctrlHoverBorderBase: _shellColor("controls.hover-cursor-border", fg)
  readonly property color _ctrlSelectedBorderBase: _shellColor("controls.selected-border", accent)
  readonly property real _ctrlNormalAlpha: _shellAlpha("controls.normal-fill-alpha", 0.04)
  readonly property real _ctrlHoverAlpha: _shellAlpha("controls.hover-cursor-fill-alpha", 0.08)
  readonly property real _ctrlSelectedAlpha: _shellAlpha("controls.selected-fill-alpha", 0.18)
  readonly property real _ctrlNormalBorderAlpha: _shellAlpha("controls.normal-border-alpha", 0.4)
  readonly property real _ctrlHoverBorderAlpha: _shellAlpha("controls.hover-cursor-border-alpha", 0.25)

  readonly property color surfaceColor: Qt.rgba(_ctrlNormalBase.r, _ctrlNormalBase.g, _ctrlNormalBase.b, _ctrlNormalAlpha)
  readonly property color surfaceElevated: Qt.rgba(_ctrlHoverBase.r, _ctrlHoverBase.g, _ctrlHoverBase.b, _ctrlHoverAlpha)
  readonly property color selectedSurface: Qt.rgba(_ctrlSelectedBase.r, _ctrlSelectedBase.g, _ctrlSelectedBase.b, _ctrlSelectedAlpha)
  readonly property color borderSubtle: Qt.rgba(_ctrlNormalBorderBase.r, _ctrlNormalBorderBase.g, _ctrlNormalBorderBase.b, Math.min(0.28, _ctrlNormalBorderAlpha * 0.6))
  readonly property color borderHover: Qt.rgba(_ctrlHoverBorderBase.r, _ctrlHoverBorderBase.g, _ctrlHoverBorderBase.b, Math.max(0.25, _ctrlHoverBorderAlpha))
  readonly property color fgMuted: Qt.rgba(fg.r, fg.g, fg.b, 0.64)
  readonly property color fgDim: Qt.rgba(fg.r, fg.g, fg.b, 0.44)
  // Match Omarchy Commons/Style.qml ("monospace" follows `omarchy font set`)
  readonly property string fontFamily: "monospace"

  readonly property var providerColors: ({
    claude: _col(rawTheme.orange || rawTheme.red || rawTheme.color9 || rawTheme.color1, "#e1875c"),
    codex: _col(rawTheme.green || rawTheme.color2, "#a9b665"),
    antigravity: _col(rawTheme.blue || rawTheme.cyan || rawTheme.accent || rawTheme.color4, "#7daea3"),
    opencode: _col(rawTheme.magenta || rawTheme.color5, "#d3869b")
  })
  readonly property var providerOrder: ["claude", "codex", "antigravity", "opencode"]
  readonly property var providerNames: ({
    claude: "Claude Code",
    codex: "GPT Codex",
    antigravity: "Antigravity",
    opencode: "OpenCode"
  })

  // ---- i18n (follows Omarchy shell language, falls back to LANG / en) ----
  property string langCode: {
    var envLang = String(Quickshell.env("LC_ALL") || Quickshell.env("LANG") || "en").toLowerCase()
    return envLang.indexOf("pt") === 0 ? "pt-BR" : "en"
  }
  property var catalogEn: ({})
  property var catalogPt: ({})

  function t(key, fb) {
    var cat = langCode === "pt-BR" ? catalogPt : catalogEn
    if (cat && typeof cat[key] === "string" && cat[key].length > 0) return cat[key]
    if (catalogEn && typeof catalogEn[key] === "string" && catalogEn[key].length > 0) return catalogEn[key]
    return fb !== undefined ? fb : key
  }

  // ---- View & Filter State ----
  property string activeTab: {
    var t0 = String(Quickshell.env("OMAMETER_ANALYTICS_INITIAL_TAB") || "").trim()
    return (t0 === "quotas" || t0 === "settings") ? t0 : "overview"
  }
  property string period: {
    var p0 = String(Quickshell.env("OMAMETER_ANALYTICS_INITIAL_PERIOD") || "").trim()
    return p0 !== "" ? p0 : "today"
  }
  property string providerFilter: {
    var pr0 = String(Quickshell.env("OMAMETER_ANALYTICS_INITIAL_PROVIDER") || "").trim()
    return (pr0 === "claude" || pr0 === "codex" || pr0 === "antigravity" || pr0 === "opencode") ? pr0 : "all"
  }
  property string modelFilter: "all"
  property string projectFilter: "all"
  property var hourStartFilter: null
  property string metricMode: "cost"          // "cost" | "tokens"
  property string rightPanelMode: "projects"  // "projects" | "routes" | "sessions" | "cloud"
  property string historyAccountId: ""
  property string historyMetricKey: ""
  property bool privacyOverride: false

  // ---- Backend State ----
  property bool workerReady: false
  property bool loading: true
  property bool scanning: false
  property bool enablingLocal: false
  property bool _initialScanDone: false
  property bool _t3ScanPending: false
  property string scanPhase: ""
  property string scanProviderName: ""
  property int scanProcessedFiles: 0
  property int scanTotalFiles: 0
  property int scanNewEvents: 0
  property real scanPercent: 0
  property bool pricingRefreshing: false
  property string statusBanner: ""
  property bool statusIsError: false
  property int _reqSeq: 0
  property var report: null

  // Inline pricing / alias editor state (Sources & Pricing tab)
  property string editModelName: ""
  property string editAliasTarget: ""
  property string editInputPerM: ""
  property string editOutputPerM: ""
  property string editCacheReadPerM: ""
  property string editCacheWritePerM: ""

  readonly property bool localEnabled: !!(report && report.localEnabled)
  readonly property bool privacyActive: privacyOverride || !!(report && report.privacyMode)
  readonly property var summary: (report && report.summary) ? report.summary : ({})
  readonly property var series: (report && Array.isArray(report.series)) ? report.series : []
  readonly property var providers: (report && Array.isArray(report.providers)) ? report.providers : []
  readonly property var models: (report && Array.isArray(report.models)) ? report.models : []
  readonly property var projects: (report && Array.isArray(report.projects)) ? report.projects : []
  readonly property var routes: (report && Array.isArray(report.routes)) ? report.routes : []
  readonly property var sessions: (report && Array.isArray(report.sessions)) ? report.sessions : []
  readonly property var unpricedModels: (report && Array.isArray(report.unpricedModels)) ? report.unpricedModels : []
  readonly property var quotas: (report && report.quotas) ? report.quotas : ({ live: [], cloudSpend: [], historyMetrics: [], selectedHistory: {} })
  readonly property var config: (report && report.config) ? report.config : ({})
  readonly property var pricingInfo: (report && report.pricingInfo) ? report.pricingInfo : ({})
  readonly property var thresholds: (report && report.thresholds) ? report.thresholds : ({ warning: 30, critical: 10 })

  // ---- Formatting Helpers ----
  function fmtTokens(n) {
    var v = Number(n || 0)
    if (v >= 1000000000) return (v / 1000000000).toFixed(2) + "B"
    if (v >= 1000000) return (v / 1000000).toFixed(2) + "M"
    if (v >= 1000) return (v / 1000).toFixed(1) + "k"
    return String(Math.round(v))
  }

  function fmtUsd(v) {
    var n = Number(v || 0)
    if (n >= 1000) return "$" + n.toFixed(0)
    if (n >= 100) return "$" + n.toFixed(1)
    if (n >= 0.01 || n === 0) return "$" + n.toFixed(2)
    return "$" + n.toFixed(4)
  }

  function fmtTimeShort(tsSec) {
    if (!tsSec) return "—"
    var d = new Date(Number(tsSec) * 1000)
    var mm = String(d.getMonth() + 1).padStart(2, "0")
    var dd = String(d.getDate()).padStart(2, "0")
    var hh = String(d.getHours()).padStart(2, "0")
    var mi = String(d.getMinutes()).padStart(2, "0")
    return mm + "-" + dd + " " + hh + ":" + mi
  }

  function maskProject(name) {
    var s = String(name || "")
    if (!privacyActive || !s || s === "(no project)") return s
    return "project-" + Math.abs(_simpleHash(s) % 997)
  }

  function _simpleHash(str) {
    var h = 0
    for (var i = 0; i < str.length; i++) {
      h = ((h << 5) - h + str.charCodeAt(i)) | 0
    }
    return h
  }

  function _showStatus(msg, isErr) {
    statusBanner = appRoot.cleanText(msg, 220)
    statusIsError = !!isErr
    statusClearTimer.restart()
  }

  function _resetScanProgress() {
    scanPhase = ""
    scanProviderName = ""
    scanProcessedFiles = 0
    scanTotalFiles = 0
    scanNewEvents = 0
    scanPercent = 0
  }

  function startScan(force) {
    if (scanning) return
    if (!localEnabled) {
      fetchReport("none")
      return
    }
    _resetScanProgress()
    scanning = true
    scanPhase = "preparing"
    sendCmd({ cmd: "scan", force: !!force })
  }

  // ---- NDJSON Backend Communication ----
  function sendCmd(obj) {
    if (!workerProc.running) return
    _reqSeq += 1
    obj.id = _reqSeq
    try {
      workerProc.write(JSON.stringify(obj) + "\n")
    } catch (e) {
      _showStatus(t("analytics.ipcError", "Could not communicate with analytics worker."), true)
    }
  }

  function fetchReport(scanMode) {
    loading = true
    if (scanMode && scanMode !== "none") {
      _resetScanProgress()
      scanning = true
      scanPhase = "preparing"
    }
    sendCmd({
      cmd: "get_report",
      scan: scanMode || "none",
      period: period,
      provider: providerFilter,
      model: modelFilter,
      project: projectFilter,
      hourStart: hourStartFilter,
      historyAccountId: historyAccountId,
      historyMetricKey: historyMetricKey
    })
  }

  function setPeriod(p) {
    period = p
    hourStartFilter = null
    fetchReport("none")
  }

  function setProvider(p) {
    providerFilter = (providerFilter === p && p !== "all") ? "all" : p
    modelFilter = "all"
    hourStartFilter = null
    fetchReport("none")
  }

  function setModelFilter(m) {
    modelFilter = (modelFilter === m) ? "all" : m
    fetchReport("none")
  }

  function setProjectFilter(p) {
    projectFilter = (projectFilter === p) ? "all" : p
    fetchReport("none")
  }

  function clearExtraFilters() {
    modelFilter = "all"
    projectFilter = "all"
    hourStartFilter = null
    fetchReport("none")
  }

  function enableLocalAnalytics(val) {
    if (enablingLocal) return
    enablingLocal = true
    if (val) {
      _resetScanProgress()
      scanning = true
      scanPhase = "preparing"
    }
    sendCmd({ cmd: "set_local_enabled", enabled: !!val })
  }

  function toggleSource(pid, enabled) {
    var patch = { enabledSources: {} }
    patch.enabledSources[pid] = !!enabled
    sendCmd({ cmd: "update_config", patch: patch })
  }

  function setT3Antigravity(enabled) {
    _t3ScanPending = !!enabled
    sendCmd({ cmd: "update_config", patch: { includeT3Antigravity: !!enabled } })
  }

  function setRetentionDays(days) {
    sendCmd({ cmd: "update_config", patch: { retentionDays: Number(days) } })
  }

  function savePlanPrice(pid, txt) {
    var cleaned = String(txt || "").trim().replace(/[^0-9.]/g, "")
    var val = cleaned === "" ? null : Number(cleaned)
    sendCmd({ cmd: "set_plan_price", provider: pid, monthlyUsd: val })
  }

  function saveCustomPrice() {
    var m = String(editModelName || "").trim()
    if (!m) return
    if (editAliasTarget.trim() !== "") {
      sendCmd({ cmd: "set_model_alias", sourceModel: m, targetModel: editAliasTarget.trim() })
    } else {
      sendCmd({
        cmd: "set_model_price",
        model: m,
        inputPerM: editInputPerM.trim() !== "" ? Number(editInputPerM) : null,
        outputPerM: editOutputPerM.trim() !== "" ? Number(editOutputPerM) : null,
        cacheReadPerM: editCacheReadPerM.trim() !== "" ? Number(editCacheReadPerM) : null,
        cacheWritePerM: editCacheWritePerM.trim() !== "" ? Number(editCacheWritePerM) : null
      })
    }
    editModelName = ""
    editAliasTarget = ""
    editInputPerM = ""
    editOutputPerM = ""
    editCacheReadPerM = ""
    editCacheWritePerM = ""
  }

  function removePriceOverride(m) {
    sendCmd({ cmd: "set_model_price", model: m, inputPerM: null, outputPerM: null, cacheReadPerM: null, cacheWritePerM: null })
  }

  function removeModelAlias(m) {
    sendCmd({ cmd: "set_model_alias", sourceModel: m, targetModel: null })
  }

  function refreshPricingOnline() {
    pricingRefreshing = true
    sendCmd({ cmd: "refresh_pricing" })
  }

  function handleWorkerLine(line) {
    var msg = null
    try { msg = JSON.parse(line) } catch (e) { return }
    if (!msg || typeof msg !== "object") return
    if (msg.type === "ready") {
      workerReady = true
      _initialScanDone = false
      fetchReport("none")
      return
    }
    if (msg.type === "scan_progress") {
      scanning = true
      scanPhase = String(msg.phase || "scanning")
      scanProviderName = String(msg.providerName || msg.provider || "")
      scanProcessedFiles = Number(msg.processedFiles || 0)
      scanTotalFiles = Number(msg.totalFiles || 0)
      scanNewEvents = Number(msg.newEvents || 0)
      scanPercent = Number(msg.percent || 0)
      return
    }
    if (msg.type !== "response") return

    var cmd = String(msg.cmd || "")
    if (!msg.ok) {
      loading = false
      scanning = false
      enablingLocal = false
      pricingRefreshing = false
      _showStatus(String(msg.error || t("analytics.cmdFailed", "Analytics command failed.")), true)
      return
    }

    if (cmd === "get_report") {
      report = msg.report || null
      loading = false
      if (msg.scan !== null && msg.scan !== undefined) scanning = false
      if (report && report.quotas && report.quotas.selectedHistory) {
        var sh = report.quotas.selectedHistory
        if (!historyAccountId && sh.accountId) historyAccountId = String(sh.accountId)
        if (!historyMetricKey && sh.metricKey) historyMetricKey = String(sh.metricKey)
      }
      if (!_initialScanDone && report && report.localEnabled) {
        _initialScanDone = true
        startScan(false)
      }
    } else if (cmd === "scan") {
      scanning = false
      var stats = msg.scan || {}
      if (Number(stats.scannedFiles || 0) > 0) {
        _showStatus(
          t("analytics.scanCompleteToast", "Scan complete: %1 files, %2 new events.")
            .arg(Number(stats.scannedFiles || 0))
            .arg(Number(stats.newEvents || 0)),
          false
        )
      }
      fetchReport("none")
    } else if (cmd === "set_local_enabled") {
      enablingLocal = false
      if (msg.localEnabled) {
        _initialScanDone = true
        _showStatus(t("analytics.localEnabledToast", "Local token analytics enabled."), false)
        fetchReport("none")
        // Kick off incremental scan immediately with live streaming progress
        _resetScanProgress()
        scanning = true
        scanPhase = "preparing"
        sendCmd({ cmd: "scan", force: false })
      } else {
        scanning = false
        _showStatus(t("analytics.localDisabledToast", "Local token analytics paused."), false)
        fetchReport("none")
      }
    } else if (cmd === "update_config" || cmd === "set_model_price" || cmd === "set_model_alias" || cmd === "set_plan_price") {
      _showStatus(t("analytics.savedToast", "Analytics settings updated."), false)
      if (cmd === "update_config" && _t3ScanPending) {
        _t3ScanPending = false
        if (localEnabled && !scanning) {
          startScan(false)
          return
        }
      }
      fetchReport("none")
    } else if (cmd === "refresh_pricing") {
      pricingRefreshing = false
      _showStatus(t("analytics.pricingUpdated", "LiteLLM pricing catalog updated (%1 models).").arg(Number(msg.count || msg.models || 0)), false)
      fetchReport("none")
    } else if (cmd === "clear_token_history") {
      _showStatus(t("analytics.ledgerCleared", "Local token ledger cleared."), false)
      fetchReport("none")
    }
  }

  function reloadOmarchyTheme() {
    colorsFile.reload()
    shellTomlFile.reload()
    userShellTomlFile.reload()
    if (!hyprRoundingProc.running) hyprRoundingProc.running = true
  }

  // ---- File Watchers (Omarchy Theme + Shell Overrides + Language + i18n) ----
  FileView {
    id: colorsFile
    path: appRoot.stateThemeDir + "/colors.toml"
    watchChanges: true
    printErrors: false
    onLoaded: appRoot.rawTheme = appRoot.parseColorsToml(text())
    onFileChanged: reload()
    onLoadFailed: legacyColorsFile.reload()
  }

  FileView {
    id: legacyColorsFile
    path: appRoot.legacyThemeDir + "/colors.toml"
    watchChanges: false
    printErrors: false
    onLoaded: {
      if (Object.keys(appRoot.rawTheme || {}).length === 0) {
        appRoot.rawTheme = appRoot.parseColorsToml(text())
      }
    }
  }

  FileView {
    id: shellTomlFile
    path: appRoot.stateThemeDir + "/shell.toml"
    watchChanges: true
    printErrors: false
    onLoaded: {
      appRoot.themeShellValues = appRoot.parseShellToml(text())
      appRoot._mergeShellValues()
    }
    onFileChanged: reload()
    onLoadFailed: {
      appRoot.themeShellValues = ({})
      appRoot._mergeShellValues()
    }
  }

  FileView {
    id: userShellTomlFile
    path: appRoot.userShellTomlPath
    watchChanges: true
    printErrors: false
    onLoaded: {
      appRoot.userShellValues = appRoot.parseShellToml(text())
      appRoot._mergeShellValues()
    }
    onFileChanged: reload()
    onLoadFailed: {
      appRoot.userShellValues = ({})
      appRoot._mergeShellValues()
    }
  }

  // Match Omarchy Commons/Style.qml cornerRadius (derived from Hyprland decoration:rounding)
  Process {
    id: hyprRoundingProc
    command: ["hyprctl", "getoption", "decoration:rounding", "-j"]
    running: true
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var obj = JSON.parse(String(text || "").trim())
          var r = Number(obj && obj.int !== undefined ? obj.int : 0)
          appRoot.cornerRadius = (isFinite(r) && r > 0) ? Math.max(2, Math.round(r / 2)) : 0
        } catch (e) {
          appRoot.cornerRadius = 0
        }
      }
    }
  }

  // Re-read symlinked current/theme periodically so live `omarchy theme set` updates immediately
  Timer {
    interval: 4000
    repeat: true
    running: win.visible
    onTriggered: {
      colorsFile.reload()
      shellTomlFile.reload()
    }
  }

  FileView {
    id: shellJsonFile
    path: appRoot.shellJsonPath
    watchChanges: true
    printErrors: false
    onLoaded: {
      try {
        var obj = JSON.parse(text())
        var lang = String((obj && obj.language) || "").trim()
        if (lang) appRoot.langCode = lang.toLowerCase().indexOf("pt") === 0 ? "pt-BR" : "en"
      } catch (e) {}
    }
    onFileChanged: reload()
  }

  FileView {
    id: enFile
    path: appRoot.pluginDir + "/i18n/en.json"
    printErrors: false
    onLoaded: { try { appRoot.catalogEn = JSON.parse(text()) } catch (e) {} }
  }

  FileView {
    id: ptFile
    path: appRoot.pluginDir + "/i18n/pt-BR.json"
    printErrors: false
    onLoaded: { try { appRoot.catalogPt = JSON.parse(text()) } catch (e) {} }
  }

  // ---- Persistent Python NDJSON Worker (alive only while window is open) ----
  Process {
    id: workerProc
    command: ["/usr/bin/python3", "-u", appRoot.cliPath, "--serve"]
    running: true
    stdinEnabled: true

    stdout: SplitParser {
      onRead: function(line) { appRoot.handleWorkerLine(line) }
    }
    stderr: SplitParser {
      onRead: function(line) {
        var cleaned = appRoot.cleanText(line, 200).trim()
        if (cleaned) appRoot._showStatus(cleaned, true)
      }
    }
  }

  // Periodic incremental refresh every 60s while window stays open
  Timer {
    interval: 60000
    repeat: true
    running: appRoot.workerReady && win.visible
    onTriggered: {
      if (!appRoot.scanning) appRoot.fetchReport("recent")
    }
  }

  Timer {
    id: statusClearTimer
    interval: 4500
    repeat: false
    onTriggered: appRoot.statusBanner = ""
  }

  // ---- IPC so `bin/omameter-analytics` can focus & re-filter an open window ----
  IpcHandler {
    target: "analytics"

    function focus(tab: string, provider: string, per: string): string {
      if (tab === "overview" || tab === "quotas" || tab === "settings") appRoot.activeTab = tab
      if (provider === "all" || provider === "claude" || provider === "codex" || provider === "antigravity" || provider === "opencode") {
        appRoot.providerFilter = provider
      }
      if (per && per !== "") appRoot.period = per
      win.visible = true
      appRoot.reloadOmarchyTheme()
      if (appRoot.workerReady) appRoot.fetchReport("recent")
      return "ok"
    }
  }

  // ---- Reusable Omarchy Control Button Component ----
  component ChipButton: Rectangle {
    id: chip
    property string text: ""
    property bool selected: false
    property bool muted: false
    property color dotColor: "transparent"
    property bool showDot: false
    property int fontSize: 11
    property int padX: 10
    property int padY: 5
    signal clicked()

    implicitWidth: chipRow.implicitWidth + padX * 2
    implicitHeight: chipRow.implicitHeight + padY * 2
    radius: appRoot.cornerRadius
    color: selected
      ? appRoot.selectedSurface
      : (chipHover.hovered ? appRoot.surfaceElevated : appRoot.surfaceColor)
    Behavior on color { ColorAnimation { duration: 80 } }
    border.width: 1
    border.color: selected
      ? Qt.rgba(appRoot.accent.r, appRoot.accent.g, appRoot.accent.b, 0.72)
      : (chipHover.hovered ? appRoot.borderHover : appRoot.borderSubtle)

    HoverHandler { id: chipHover }

    Row {
      id: chipRow
      anchors.centerIn: parent
      spacing: 6

      Rectangle {
        visible: chip.showDot
        width: 7
        height: 7
        radius: 3.5
        anchors.verticalCenter: parent.verticalCenter
        color: chip.dotColor
      }

      PlainText {
        anchors.verticalCenter: parent.verticalCenter
        text: chip.text
        color: chip.selected ? appRoot.accent : (chip.muted ? appRoot.fgMuted : appRoot.fg)
        font.family: appRoot.fontFamily
        font.pixelSize: chip.fontSize
        font.bold: chip.selected
      }
    }

    MouseArea {
      anchors.fill: parent
      cursorShape: Qt.PointingHandCursor
      onClicked: chip.clicked()
    }
  }

  // ---- Reusable Omarchy Card Container ----
  component CardBox: Rectangle {
    radius: appRoot.cornerRadius
    color: appRoot.surfaceColor
    border.width: 1
    border.color: appRoot.borderSubtle
  }

  // ---- Main Floating Window ----
  FloatingWindow {
    id: win
    title: "Omameter Analytics"
    implicitWidth: 1120
    implicitHeight: 760
    minimumSize: Qt.size(780, 540)
    color: appRoot.bg
    visible: true

    onVisibleChanged: {
      if (!visible) Qt.quit()
    }

    Item {
      id: rootItem
      anchors.fill: parent
      focus: true

      Keys.onPressed: function(ev) {
        var ctrl = (ev.modifiers & Qt.ControlModifier) !== 0
        if (ev.key === Qt.Key_Escape || (ctrl && ev.key === Qt.Key_Q)) {
          Qt.quit()
          ev.accepted = true
        } else if (ctrl && ev.key === Qt.Key_R) {
          appRoot.startScan(false)
          ev.accepted = true
        } else if (!ctrl && !(ev.modifiers & Qt.AltModifier)) {
          if (ev.key === Qt.Key_1) { appRoot.activeTab = "overview"; ev.accepted = true }
          else if (ev.key === Qt.Key_2) { appRoot.activeTab = "quotas"; ev.accepted = true }
          else if (ev.key === Qt.Key_3) { appRoot.activeTab = "settings"; ev.accepted = true }
          else if (ev.key === Qt.Key_C && appRoot.activeTab === "overview") { appRoot.metricMode = "cost"; ev.accepted = true }
          else if (ev.key === Qt.Key_T && appRoot.activeTab === "overview") { appRoot.metricMode = "tokens"; ev.accepted = true }
          else if (ev.key === Qt.Key_P) { appRoot.privacyOverride = !appRoot.privacyOverride; ev.accepted = true }
        }
      }

      ColumnLayout {
        anchors.fill: parent
        anchors.margins: 16
        spacing: 12

        // ==================== TOP HEADER BAR ====================
        RowLayout {
          Layout.fillWidth: true
          spacing: 10

          // Title & status subtitle
          Column {
            spacing: 2

            PlainText {
              text: appRoot.t("analytics.title", "Omameter Analytics")
              color: appRoot.fg
              font.family: appRoot.fontFamily
              font.pixelSize: 16
              font.bold: true
            }

            PlainText {
              text: appRoot.scanning
                ? appRoot.t("analytics.scanning", "Scanning local harness logs...")
                : (appRoot.localEnabled && appRoot.report && appRoot.report.lastScanAt
                  ? appRoot.t("analytics.lastScan", "Last scan: %1").arg(appRoot.fmtTimeShort(appRoot.report.lastScanAt))
                  : appRoot.t("analytics.subtitlePhase1", "Quota history & local token analytics"))
              color: appRoot.fgMuted
              font.family: appRoot.fontFamily
              font.pixelSize: 10
            }
          }

          Item { Layout.fillWidth: true }

          // Main Tabs (1 / 2 / 3)
          Row {
            spacing: 6

            ChipButton {
              text: appRoot.t("analytics.tabOverview", "Overview")
              selected: appRoot.activeTab === "overview"
              onClicked: appRoot.activeTab = "overview"
            }

            ChipButton {
              text: appRoot.t("analytics.tabQuotas", "Quota History")
              selected: appRoot.activeTab === "quotas"
              onClicked: appRoot.activeTab = "quotas"
            }

            ChipButton {
              text: appRoot.t("analytics.tabSources", "Sources & Pricing")
              selected: appRoot.activeTab === "settings"
              onClicked: appRoot.activeTab = "settings"
            }
          }

          Rectangle {
            width: 1
            height: 22
            color: appRoot.borderSubtle
          }

          // Cost / Tokens toggle
          Row {
            visible: appRoot.activeTab === "overview"
            spacing: 4

            ChipButton {
              text: appRoot.t("analytics.metricCost", "$ Cost")
              selected: appRoot.metricMode === "cost"
              padX: 8
              onClicked: appRoot.metricMode = "cost"
            }

            ChipButton {
              text: appRoot.t("analytics.metricTokens", "Tokens")
              selected: appRoot.metricMode === "tokens"
              padX: 8
              onClicked: appRoot.metricMode = "tokens"
            }
          }

          // Privacy quick toggle
          ChipButton {
            text: appRoot.privacyActive
              ? appRoot.t("analytics.privacyOn", "Privacy: ON")
              : appRoot.t("analytics.privacyOff", "Privacy")
            selected: appRoot.privacyActive
            padX: 8
            onClicked: appRoot.privacyOverride = !appRoot.privacyOverride
          }

          // Refresh / Scan
          ChipButton {
            text: appRoot.scanning
              ? appRoot.t("analytics.refreshing", "Scanning...")
              : appRoot.t("analytics.refresh", "Refresh")
            onClicked: appRoot.startScan(false)
          }
        }

        // ==================== LIVE SCAN PROGRESS BAR ====================
        Rectangle {
          visible: appRoot.scanning || appRoot.enablingLocal
          Layout.fillWidth: true
          implicitHeight: scanProgCol.implicitHeight + 16
          radius: appRoot.cornerRadius
          color: Qt.rgba(appRoot.accent.r, appRoot.accent.g, appRoot.accent.b, 0.11)
          border.width: 1
          border.color: Qt.rgba(appRoot.accent.r, appRoot.accent.g, appRoot.accent.b, 0.45)

          Column {
            id: scanProgCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 8
            spacing: 6

            Item {
              width: parent.width
              height: 16

              PlainText {
                anchors.left: parent.left
                anchors.right: scanPctTxt.left
                anchors.rightMargin: 10
                anchors.verticalCenter: parent.verticalCenter
                elide: Text.ElideRight
                text: {
                  if (appRoot.scanPhase === "finalizing") {
                    return appRoot.t("analytics.scanFinalizing", "Finalizing local ledger & building report...")
                  }
                  if (appRoot.scanTotalFiles > 0) {
                    var who = appRoot.scanProviderName !== "" ? appRoot.scanProviderName : "harnesses"
                    return appRoot.t("analytics.scanProgress", "Scanning %1: %2 / %3 files (%4 events)...")
                      .arg(who)
                      .arg(appRoot.scanProcessedFiles)
                      .arg(appRoot.scanTotalFiles)
                      .arg(appRoot.scanNewEvents)
                  }
                  return appRoot.t("analytics.scanPreparing", "Discovering local harness logs (Claude, Codex, Antigravity, OpenCode)...")
                }
                color: appRoot.fg
                font.family: appRoot.fontFamily
                font.pixelSize: 11
                font.bold: true
              }

              PlainText {
                id: scanPctTxt
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: appRoot.scanTotalFiles > 0
                  ? (Math.round(appRoot.scanPercent) + "%")
                  : "…"
                color: appRoot.accent
                font.family: appRoot.fontFamily
                font.pixelSize: 11
                font.bold: true
              }
            }

            // Progress bar track (determinate fill or animated indeterminate pulse)
            Rectangle {
              id: progTrack
              width: parent.width
              height: 6
              radius: appRoot.radiusCap(3)
              clip: true
              color: Qt.rgba(appRoot.fg.r, appRoot.fg.g, appRoot.fg.b, 0.12)

              Rectangle {
                visible: appRoot.scanTotalFiles > 0
                height: parent.height
                radius: appRoot.radiusCap(3)
                color: appRoot.accent
                width: Math.max(8, parent.width * Math.max(0.02, Math.min(1.0, appRoot.scanPercent / 100.0)))
                Behavior on width { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
              }

              Rectangle {
                id: pulseBar
                visible: appRoot.scanTotalFiles === 0 && (appRoot.scanning || appRoot.enablingLocal)
                width: Math.max(80, parent.width * 0.28)
                height: parent.height
                radius: appRoot.radiusCap(3)
                color: appRoot.accent
                x: -width

                SequentialAnimation on x {
                  running: pulseBar.visible
                  loops: Animation.Infinite
                  NumberAnimation { from: -pulseBar.width; to: progTrack.width; duration: 1100; easing.type: Easing.InOutQuad }
                }
              }
            }
          }
        }

        // ==================== STATUS / ERROR BANNER ====================
        Rectangle {
          visible: appRoot.statusBanner !== ""
          Layout.fillWidth: true
          implicitHeight: bannerTxt.implicitHeight + 12
          radius: appRoot.cornerRadius
          color: appRoot.statusIsError
            ? Qt.rgba(appRoot.criticalColor.r, appRoot.criticalColor.g, appRoot.criticalColor.b, 0.16)
            : Qt.rgba(appRoot.accent.r, appRoot.accent.g, appRoot.accent.b, 0.14)
          border.width: 1
          border.color: appRoot.statusIsError
            ? Qt.rgba(appRoot.criticalColor.r, appRoot.criticalColor.g, appRoot.criticalColor.b, 0.55)
            : Qt.rgba(appRoot.accent.r, appRoot.accent.g, appRoot.accent.b, 0.45)

          PlainText {
            id: bannerTxt
            anchors.fill: parent
            anchors.margins: 6
            text: appRoot.statusBanner
            color: appRoot.statusIsError ? appRoot.criticalColor : appRoot.fg
            font.family: appRoot.fontFamily
            font.pixelSize: 11
            wrapMode: Text.WordWrap
          }
        }

        // ==================== MAIN SCROLLABLE BODY ====================
        ScrollView {
          id: bodyScroll
          Layout.fillWidth: true
          Layout.fillHeight: true
          clip: true
          ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

          Item {
            width: bodyScroll.availableWidth
            implicitHeight: {
              if (appRoot.activeTab === "quotas") return quotasTabCol.implicitHeight + 12
              if (appRoot.activeTab === "settings") return settingsTabCol.implicitHeight + 12
              return overviewTabCol.implicitHeight + 12
            }

            // ============================================================
            // TAB 1: OVERVIEW (Phase 2 Local Tokens + Phase 1 Summary)
            // ============================================================
            Column {
              id: overviewTabCol
              visible: appRoot.activeTab === "overview"
              width: parent.width
              spacing: 12

              // ---- Filter Bar: Period + Since Reset + Harnesses ----
              Flow {
                width: parent.width
                spacing: 6

                Repeater {
                  model: [
                    { id: "today", key: "analytics.periodToday", fb: "Today" },
                    { id: "7d",    key: "analytics.period7d",    fb: "7d" },
                    { id: "30d",   key: "analytics.period30d",   fb: "30d" },
                    { id: "90d",   key: "analytics.period90d",   fb: "90d" },
                    { id: "all",   key: "analytics.periodAll",   fb: "All" }
                  ]
                  delegate: ChipButton {
                    required property var modelData
                    text: appRoot.t(modelData.key, modelData.fb)
                    selected: appRoot.period === modelData.id
                    onClicked: appRoot.setPeriod(modelData.id)
                  }
                }

                // Dynamic "Since Reset" pills from live Omameter quota windows
                Repeater {
                  model: (appRoot.quotas && Array.isArray(appRoot.quotas.live))
                    ? appRoot.quotas.live.filter(function(q) { return !!q.canSinceReset }).slice(0, 3)
                    : []
                  delegate: ChipButton {
                    required property var modelData
                    text: appRoot.t("analytics.sinceReset", "Since reset") + ": " + String(modelData.providerName || modelData.provider) + " " + String(modelData.title || "")
                    selected: appRoot.period === String(modelData.resetWindowId || "")
                    showDot: true
                    dotColor: appRoot.providerColors[modelData.provider] || appRoot.accent
                    onClicked: appRoot.setPeriod(String(modelData.resetWindowId || "today"))
                  }
                }

                Rectangle {
                  width: 1
                  height: 24
                  color: appRoot.borderSubtle
                }

                // Harness filter chips
                ChipButton {
                  text: appRoot.t("analytics.allHarnesses", "All harnesses")
                  selected: appRoot.providerFilter === "all"
                  onClicked: appRoot.setProvider("all")
                }

                Repeater {
                  model: appRoot.providers
                  delegate: ChipButton {
                    required property var modelData
                    text: String(modelData.name || modelData.id)
                      + (Number(modelData.totalTokens || 0) > 0
                        ? (" · " + (appRoot.metricMode === "cost"
                          ? appRoot.fmtUsd(modelData.costUsd)
                          : appRoot.fmtTokens(modelData.totalTokens)))
                        : "")
                    selected: appRoot.providerFilter === modelData.id
                    muted: !modelData.installed && Number(modelData.totalTokens || 0) === 0
                    showDot: true
                    dotColor: appRoot.providerColors[modelData.id] || appRoot.accent
                    onClicked: appRoot.setProvider(modelData.id)
                  }
                }
              }

              // Active sub-filters bar (model / project / clicked hour)
              Row {
                visible: appRoot.modelFilter !== "all" || appRoot.projectFilter !== "all" || appRoot.hourStartFilter !== null
                spacing: 8

                PlainText {
                  anchors.verticalCenter: parent.verticalCenter
                  text: appRoot.t("analytics.activeFilters", "Filtered by:")
                  color: appRoot.fgMuted
                  font.family: appRoot.fontFamily
                  font.pixelSize: 11
                }

                ChipButton {
                  visible: appRoot.modelFilter !== "all"
                  text: "Model: " + appRoot.modelFilter + " ×"
                  selected: true
                  onClicked: appRoot.setModelFilter("all")
                }

                ChipButton {
                  visible: appRoot.projectFilter !== "all"
                  text: "Project: " + appRoot.maskProject(appRoot.projectFilter) + " ×"
                  selected: true
                  onClicked: appRoot.setProjectFilter("all")
                }

                ChipButton {
                  visible: appRoot.hourStartFilter !== null
                  text: appRoot.t("analytics.hourBucket", "Hour: %1").arg(appRoot.fmtTimeShort(appRoot.hourStartFilter)) + " ×"
                  selected: true
                  onClicked: { appRoot.hourStartFilter = null; appRoot.fetchReport("none") }
                }

                ChipButton {
                  text: appRoot.t("analytics.clearFilters", "Clear")
                  onClicked: appRoot.clearExtraFilters()
                }
              }

              // ---- Opt-In Privacy Banner (shown when localTokenAnalytics is OFF) ----
              CardBox {
                visible: appRoot.report !== null && !appRoot.localEnabled
                width: parent.width
                implicitHeight: optInCol.implicitHeight + 28
                border.color: Qt.rgba(appRoot.accent.r, appRoot.accent.g, appRoot.accent.b, 0.55)
                color: Qt.rgba(appRoot.accent.r, appRoot.accent.g, appRoot.accent.b, 0.08)

                Column {
                  id: optInCol
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.top: parent.top
                  anchors.margins: 14
                  spacing: 10

                  PlainText {
                    width: parent.width
                    text: appRoot.t("analytics.optInTitle", "Local Token & Cost Analytics is off by default")
                    color: appRoot.fg
                    font.family: appRoot.fontFamily
                    font.pixelSize: 13
                    font.bold: true
                    wrapMode: Text.WordWrap
                  }

                  PlainText {
                    width: parent.width
                    text: appRoot.t("analytics.optInDesc", "Enable to incrementally scan local session logs from installed harnesses (Claude Code, GPT Codex, Antigravity, OpenCode). Everything stays 100% local in SQLite — zero data leaves your machine.")
                    color: appRoot.fgMuted
                    font.family: appRoot.fontFamily
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                  }

                  Flow {
                    width: parent.width
                    spacing: 8

                    Repeater {
                      model: appRoot.providers
                      delegate: Rectangle {
                        required property var modelData
                        implicitWidth: detRow.implicitWidth + 16
                        implicitHeight: detRow.implicitHeight + 10
                        radius: appRoot.cornerRadius
                        color: appRoot.surfaceColor
                        border.width: 1
                        border.color: appRoot.borderSubtle

                        Row {
                          id: detRow
                          anchors.centerIn: parent
                          spacing: 6

                          Rectangle {
                            width: 7
                            height: 7
                            radius: 3.5
                            anchors.verticalCenter: parent.verticalCenter
                            color: modelData.installed ? (appRoot.providerColors[modelData.id] || appRoot.goodColor) : appRoot.fgDim
                          }

                          PlainText {
                            anchors.verticalCenter: parent.verticalCenter
                            text: String(modelData.name || modelData.id) + ": "
                              + (modelData.installed
                                ? appRoot.t("analytics.filesDetected", "%1 files").arg(Number(modelData.fileCount || 0))
                                : appRoot.t("analytics.notDetected", "not found"))
                            color: modelData.installed ? appRoot.fg : appRoot.fgDim
                            font.family: appRoot.fontFamily
                            font.pixelSize: 11
                          }
                        }
                      }
                    }
                  }

                  Row {
                    spacing: 10

                    ChipButton {
                      text: appRoot.enablingLocal
                        ? appRoot.t("analytics.enablingBtn", "Enabling...")
                        : appRoot.t("analytics.enableLocalBtn", "Enable Local Token Analytics")
                      selected: true
                      fontSize: 12
                      padX: 14
                      padY: 7
                      onClicked: appRoot.enableLocalAnalytics(true)
                    }

                    ChipButton {
                      text: appRoot.t("analytics.viewQuotaHistoryBtn", "View Quota History (Phase 1)")
                      fontSize: 11
                      padX: 12
                      padY: 7
                      onClicked: appRoot.activeTab = "quotas"
                    }
                  }
                }
              }

              // ---- 4 KPI CARDS ----
              Row {
                width: parent.width
                spacing: 10
                readonly property real cardW: Math.floor((width - 30) / 4)

                // KPI 1: Estimated API Cost + Plan ROI
                CardBox {
                  width: parent.cardW
                  implicitHeight: 90

                  Column {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 4

                    PlainText {
                      text: appRoot.t("analytics.kpiEstCost", "API-Equiv. Spend")
                      color: appRoot.fgMuted
                      font.family: appRoot.fontFamily
                      font.pixelSize: 10
                    }

                    PlainText {
                      text: appRoot.localEnabled ? appRoot.fmtUsd(appRoot.summary.estimatedCostUsd) : "—"
                      color: appRoot.accent
                      font.family: appRoot.fontFamily
                      font.pixelSize: 21
                      font.bold: true
                    }

                    PlainText {
                      width: parent.width
                      elide: Text.ElideRight
                      text: {
                        var roi = Number(appRoot.summary.planRoiMultiplier || 0)
                        var plan = Number(appRoot.summary.planMonthlyTotalUsd || 0)
                        if (roi > 0 && plan > 0) {
                          return appRoot.t("analytics.kpiRoi", "%1x plan value ($%2/mo)").arg(roi.toFixed(1)).arg(plan.toFixed(0))
                        }
                        var cs = Number(appRoot.summary.cloudSpendUsd || 0)
                        if (cs > 0) return appRoot.t("analytics.kpiCloudSpend", "Cloud billed: %1").arg(appRoot.fmtUsd(cs))
                        return appRoot.t("analytics.kpiSetPlanHint", "Set plan prices in Sources tab")
                      }
                      color: Number(appRoot.summary.planRoiMultiplier || 0) >= 1.0 ? appRoot.goodColor : appRoot.fgDim
                      font.family: appRoot.fontFamily
                      font.pixelSize: 10
                    }
                  }
                }

                // KPI 2: Total Tokens
                CardBox {
                  width: parent.cardW
                  implicitHeight: 90

                  Column {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 4

                    PlainText {
                      text: appRoot.t("analytics.kpiTotalTokens", "Total Tokens")
                      color: appRoot.fgMuted
                      font.family: appRoot.fontFamily
                      font.pixelSize: 10
                    }

                    PlainText {
                      text: appRoot.localEnabled ? appRoot.fmtTokens(appRoot.summary.totalTokens) : "—"
                      color: appRoot.fg
                      font.family: appRoot.fontFamily
                      font.pixelSize: 21
                      font.bold: true
                    }

                    PlainText {
                      width: parent.width
                      elide: Text.ElideRight
                      text: appRoot.t("analytics.kpiInOut", "In %1 · Out %2").arg(appRoot.fmtTokens(appRoot.summary.inputTokens)).arg(appRoot.fmtTokens(appRoot.summary.outputTokens))
                      color: appRoot.fgDim
                      font.family: appRoot.fontFamily
                      font.pixelSize: 10
                    }
                  }
                }

                // KPI 3: Cache Efficiency
                CardBox {
                  width: parent.cardW
                  implicitHeight: 90

                  Column {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 4

                    PlainText {
                      text: appRoot.t("analytics.kpiCacheHit", "Cache Hit Rate")
                      color: appRoot.fgMuted
                      font.family: appRoot.fontFamily
                      font.pixelSize: 10
                    }

                    PlainText {
                      text: appRoot.localEnabled ? (Number(appRoot.summary.cacheHitRate || 0).toFixed(1) + "%") : "—"
                      color: appRoot.goodColor
                      font.family: appRoot.fontFamily
                      font.pixelSize: 21
                      font.bold: true
                    }

                    PlainText {
                      width: parent.width
                      elide: Text.ElideRight
                      text: appRoot.t("analytics.kpiCacheSaved", "Saved ~%1 (%2 read)").arg(appRoot.fmtUsd(appRoot.summary.cacheSavingsUsd)).arg(appRoot.fmtTokens(appRoot.summary.cacheReadTokens))
                      color: appRoot.fgDim
                      font.family: appRoot.fontFamily
                      font.pixelSize: 10
                    }
                  }
                }

                // KPI 4: Sessions & Pricing Coverage
                CardBox {
                  width: parent.cardW
                  implicitHeight: 90

                  Column {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 4

                    PlainText {
                      text: appRoot.t("analytics.kpiSessions", "Sessions & Requests")
                      color: appRoot.fgMuted
                      font.family: appRoot.fontFamily
                      font.pixelSize: 10
                    }

                    PlainText {
                      text: appRoot.localEnabled
                        ? (String(Number(appRoot.summary.sessions || 0)) + " / " + String(Number(appRoot.summary.requests || 0)))
                        : "—"
                      color: appRoot.fg
                      font.family: appRoot.fontFamily
                      font.pixelSize: 21
                      font.bold: true
                    }

                    PlainText {
                      width: parent.width
                      elide: Text.ElideRight
                      text: appRoot.unpricedModels.length > 0
                        ? appRoot.t("analytics.kpiUnpricedWarn", "%1 unpriced model(s) — click Sources").arg(appRoot.unpricedModels.length)
                        : appRoot.t("analytics.kpiModelsActive", "%1 active model(s)").arg(appRoot.models.length)
                      color: appRoot.unpricedModels.length > 0 ? appRoot.warningColor : appRoot.fgDim
                      font.family: appRoot.fontFamily
                      font.pixelSize: 10
                    }
                  }

                  MouseArea {
                    anchors.fill: parent
                    enabled: appRoot.unpricedModels.length > 0
                    cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                    onClicked: appRoot.activeTab = "settings"
                  }
                }
              }

              // ---- MAIN TREND BAR CHART ----
              CardBox {
                width: parent.width
                implicitHeight: 248

                Column {
                  anchors.fill: parent
                  anchors.margins: 12
                  spacing: 8

                  Item {
                    width: parent.width
                    height: 20

                    PlainText {
                      anchors.left: parent.left
                      anchors.verticalCenter: parent.verticalCenter
                      text: (appRoot.metricMode === "cost"
                        ? appRoot.t("analytics.chartCostTrend", "API-Equivalent Cost Trend")
                        : appRoot.t("analytics.chartTokenTrend", "Token Usage Trend"))
                        + (appRoot.report && appRoot.report.period ? (" · " + String(appRoot.report.period.label || "")) : "")
                      color: appRoot.fg
                      font.family: appRoot.fontFamily
                      font.pixelSize: 12
                      font.bold: true
                    }

                    Row {
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: 12

                      Repeater {
                        model: appRoot.providerOrder
                        delegate: Row {
                          required property string modelData
                          spacing: 5

                          Rectangle {
                            width: 8
                            height: 8
                            radius: 4
                            anchors.verticalCenter: parent.verticalCenter
                            color: appRoot.providerColors[modelData] || appRoot.accent
                          }

                          PlainText {
                            anchors.verticalCenter: parent.verticalCenter
                            text: String(appRoot.providerNames[modelData] || modelData)
                            color: appRoot.fgMuted
                            font.family: appRoot.fontFamily
                            font.pixelSize: 10
                          }
                        }
                      }
                    }
                  }

                  BarTrendChart {
                    width: parent.width
                    height: 196
                    series: appRoot.series
                    metricMode: appRoot.metricMode
                    granularity: (appRoot.report && appRoot.report.period) ? String(appRoot.report.period.granularity || "hourly") : "hourly"
                    selectedHourStart: appRoot.hourStartFilter
                    foreground: appRoot.fg
                    background: appRoot.bg
                    accent: appRoot.accent
                    providerColors: appRoot.providerColors
                    providerOrder: appRoot.providerOrder
                    providerNames: appRoot.providerNames
                    fontFamily: appRoot.fontFamily
                    cornerRadius: appRoot.cornerRadius
                    emptyText: appRoot.localEnabled
                      ? appRoot.t("analytics.chartEmpty", "No token events in this window")
                      : appRoot.t("analytics.chartDisabled", "Enable Local Token Analytics above to chart token & cost trends")
                    onBucketClicked: function(b) {
                      if (!b) return
                      if (appRoot.report && appRoot.report.period && appRoot.report.period.granularity === "hourly") {
                        var bts = Number(b.ts || 0)
                        appRoot.hourStartFilter = (appRoot.hourStartFilter === bts) ? null : bts
                        appRoot.fetchReport("none")
                      }
                    }
                  }
                }
              }

              // ---- TWO-COLUMN BREAKDOWN (Models | Projects/Routes/Sessions/Cloud) ----
              Row {
                width: parent.width
                spacing: 12
                readonly property real halfW: Math.floor((width - 12) / 2)

                // LEFT: Models breakdown
                CardBox {
                  width: parent.halfW
                  implicitHeight: Math.max(210, modelsCol.implicitHeight + 24)

                  Column {
                    id: modelsCol
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 12
                    spacing: 8

                    PlainText {
                      text: appRoot.t("analytics.modelsTitle", "Models Breakdown")
                      color: appRoot.fg
                      font.family: appRoot.fontFamily
                      font.pixelSize: 12
                      font.bold: true
                    }

                    PlainText {
                      visible: appRoot.models.length === 0
                      text: appRoot.t("analytics.noModels", "No models recorded in this window.")
                      color: appRoot.fgDim
                      font.family: appRoot.fontFamily
                      font.pixelSize: 11
                    }

                    Repeater {
                      model: appRoot.models.slice(0, 12)
                      delegate: Rectangle {
                        id: mRowBox
                        required property var modelData
                        readonly property bool isSel: appRoot.modelFilter === modelData.model
                        readonly property real shareRatio: {
                          var tot = appRoot.metricMode === "cost"
                            ? Math.max(0.0001, Number(appRoot.summary.estimatedCostUsd || 0))
                            : Math.max(1, Number(appRoot.summary.totalTokens || 0))
                          var cur = appRoot.metricMode === "cost"
                            ? Number(modelData.costUsd || 0)
                            : Number(modelData.totalTokens || 0)
                          return Math.max(0.02, Math.min(1.0, cur / tot))
                        }

                        width: parent.width
                        implicitHeight: 38
                        radius: appRoot.cornerRadius
                        color: isSel
                          ? Qt.rgba(appRoot.accent.r, appRoot.accent.g, appRoot.accent.b, 0.16)
                          : (mHover.hovered ? appRoot.surfaceElevated : "transparent")

                        HoverHandler { id: mHover }

                        Column {
                          anchors.fill: parent
                          anchors.leftMargin: 6
                          anchors.rightMargin: 6
                          anchors.topMargin: 4
                          spacing: 4

                          Item {
                            width: parent.width
                            height: 16

                            Row {
                              anchors.left: parent.left
                              anchors.right: mValTxt.left
                              anchors.rightMargin: 8
                              anchors.verticalCenter: parent.verticalCenter
                              spacing: 6

                              Rectangle {
                                width: 7
                                height: 7
                                radius: 3.5
                                anchors.verticalCenter: parent.verticalCenter
                                color: {
                                  var ps = modelData.providers || []
                                  return ps.length > 0 ? (appRoot.providerColors[ps[0]] || appRoot.accent) : appRoot.accent
                                }
                              }

                              PlainText {
                                anchors.verticalCenter: parent.verticalCenter
                                width: Math.max(60, parent.width - 20)
                                elide: Text.ElideRight
                                text: String(modelData.model || "") + (modelData.priced ? "" : " ⚠")
                                color: modelData.priced ? appRoot.fg : appRoot.warningColor
                                font.family: appRoot.fontFamily
                                font.pixelSize: 11
                                font.bold: mRowBox.isSel
                              }
                            }

                            PlainText {
                              id: mValTxt
                              anchors.right: parent.right
                              anchors.verticalCenter: parent.verticalCenter
                              text: (modelData.priced ? appRoot.fmtUsd(modelData.costUsd) : appRoot.t("analytics.unpricedTag", "unpriced"))
                                + "  ·  " + appRoot.fmtTokens(modelData.totalTokens)
                                + "  ·  " + Number(modelData.cacheHitRate || 0).toFixed(0) + "% cache"
                              color: appRoot.fgMuted
                              font.family: appRoot.fontFamily
                              font.pixelSize: 10
                            }
                          }

                          // Share bar
                          Rectangle {
                            width: parent.width
                            height: 4
                            radius: appRoot.radiusCap(2)
                            color: Qt.rgba(appRoot.fg.r, appRoot.fg.g, appRoot.fg.b, 0.08)

                            Rectangle {
                              width: Math.max(4, parent.width * mRowBox.shareRatio)
                              height: parent.height
                              radius: appRoot.radiusCap(2)
                              color: modelData.priced
                                ? ((modelData.providers && modelData.providers.length > 0)
                                  ? (appRoot.providerColors[modelData.providers[0]] || appRoot.accent)
                                  : appRoot.accent)
                                : appRoot.warningColor
                            }
                          }
                        }

                        MouseArea {
                          anchors.fill: parent
                          cursorShape: Qt.PointingHandCursor
                          onClicked: appRoot.setModelFilter(String(modelData.model || ""))
                        }
                      }
                    }
                  }
                }

                // RIGHT: Projects / Routes / Sessions / Cloud Spend switcher
                CardBox {
                  width: parent.halfW
                  implicitHeight: Math.max(210, rightCol.implicitHeight + 24)

                  Column {
                    id: rightCol
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 12
                    spacing: 8

                    Row {
                      spacing: 4

                      ChipButton {
                        text: appRoot.t("analytics.rightProjects", "Projects")
                        selected: appRoot.rightPanelMode === "projects"
                        fontSize: 10
                        padX: 8
                        padY: 4
                        onClicked: appRoot.rightPanelMode = "projects"
                      }
                      ChipButton {
                        text: appRoot.t("analytics.rightRoutes", "Routes")
                        selected: appRoot.rightPanelMode === "routes"
                        fontSize: 10
                        padX: 8
                        padY: 4
                        onClicked: appRoot.rightPanelMode = "routes"
                      }
                      ChipButton {
                        text: appRoot.t("analytics.rightSessions", "Sessions")
                        selected: appRoot.rightPanelMode === "sessions"
                        fontSize: 10
                        padX: 8
                        padY: 4
                        onClicked: appRoot.rightPanelMode = "sessions"
                      }
                      ChipButton {
                        text: appRoot.t("analytics.rightCloud", "Cloud Spend")
                        selected: appRoot.rightPanelMode === "cloud"
                        fontSize: 10
                        padX: 8
                        padY: 4
                        onClicked: appRoot.rightPanelMode = "cloud"
                      }
                    }

                    // Projects list
                    Column {
                      visible: appRoot.rightPanelMode === "projects"
                      width: parent.width
                      spacing: 4

                      PlainText {
                        visible: appRoot.projects.length === 0
                        text: appRoot.t("analytics.noProjects", "No projects recorded in this window.")
                        color: appRoot.fgDim
                        font.family: appRoot.fontFamily
                        font.pixelSize: 11
                      }

                      Repeater {
                        model: appRoot.projects.slice(0, 12)
                        delegate: Rectangle {
                          required property var modelData
                          readonly property bool isSel: appRoot.projectFilter === String(modelData.project || "")
                          width: parent.width
                          implicitHeight: 30
                          radius: appRoot.cornerRadius
                          color: isSel ? Qt.rgba(appRoot.accent.r, appRoot.accent.g, appRoot.accent.b, 0.16) : (pHov.hovered ? appRoot.surfaceElevated : "transparent")
                          HoverHandler { id: pHov }

                          Item {
                            anchors.fill: parent
                            anchors.leftMargin: 6
                            anchors.rightMargin: 6

                            PlainText {
                              anchors.left: parent.left
                              anchors.right: pVal.left
                              anchors.rightMargin: 8
                              anchors.verticalCenter: parent.verticalCenter
                              elide: Text.ElideRight
                              text: appRoot.maskProject(modelData.name || modelData.project)
                              color: appRoot.fg
                              font.family: appRoot.fontFamily
                              font.pixelSize: 11
                              font.bold: isSel
                            }

                            PlainText {
                              id: pVal
                              anchors.right: parent.right
                              anchors.verticalCenter: parent.verticalCenter
                              text: appRoot.fmtUsd(modelData.costUsd) + " · " + appRoot.fmtTokens(modelData.totalTokens) + " · " + Number(modelData.sessions || 0) + "s"
                              color: appRoot.fgMuted
                              font.family: appRoot.fontFamily
                              font.pixelSize: 10
                            }
                          }

                          MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: if (modelData.project) appRoot.setProjectFilter(String(modelData.project))
                          }
                        }
                      }
                    }

                    // Routes list
                    Column {
                      visible: appRoot.rightPanelMode === "routes"
                      width: parent.width
                      spacing: 4

                      PlainText {
                        visible: appRoot.routes.length === 0
                        text: appRoot.t("analytics.noRoutes", "No routes recorded in this window.")
                        color: appRoot.fgDim
                        font.family: appRoot.fontFamily
                        font.pixelSize: 11
                      }

                      Repeater {
                        model: appRoot.routes.slice(0, 12)
                        delegate: Item {
                          required property var modelData
                          width: parent.width
                          height: 26

                          PlainText {
                            anchors.left: parent.left
                            anchors.right: rVal.left
                            anchors.rightMargin: 8
                            anchors.verticalCenter: parent.verticalCenter
                            elide: Text.ElideRight
                            text: String(modelData.route || "")
                            color: appRoot.fg
                            font.family: appRoot.fontFamily
                            font.pixelSize: 11
                          }

                          PlainText {
                            id: rVal
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            text: appRoot.fmtUsd(modelData.costUsd) + " · " + appRoot.fmtTokens(modelData.totalTokens)
                            color: appRoot.fgMuted
                            font.family: appRoot.fontFamily
                            font.pixelSize: 10
                          }
                        }
                      }
                    }

                    // Recent Sessions list
                    Column {
                      visible: appRoot.rightPanelMode === "sessions"
                      width: parent.width
                      spacing: 4

                      PlainText {
                        visible: appRoot.sessions.length === 0
                        text: appRoot.t("analytics.noSessions", "No sessions recorded in this window.")
                        color: appRoot.fgDim
                        font.family: appRoot.fontFamily
                        font.pixelSize: 11
                      }

                      Repeater {
                        model: appRoot.sessions.slice(0, 12)
                        delegate: Item {
                          required property var modelData
                          width: parent.width
                          height: 28

                          Row {
                            anchors.left: parent.left
                            anchors.right: sVal.left
                            anchors.rightMargin: 8
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: 6

                            Rectangle {
                              width: 7
                              height: 7
                              radius: 3.5
                              anchors.verticalCenter: parent.verticalCenter
                              color: appRoot.providerColors[modelData.provider] || appRoot.accent
                            }

                            PlainText {
                              anchors.verticalCenter: parent.verticalCenter
                              width: Math.max(60, parent.width - 16)
                              elide: Text.ElideRight
                              text: appRoot.maskProject(modelData.projectName) + " · " + String(modelData.primaryModel || "")
                              color: appRoot.fg
                              font.family: appRoot.fontFamily
                              font.pixelSize: 11
                            }
                          }

                          PlainText {
                            id: sVal
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            text: appRoot.fmtUsd(modelData.costUsd) + " · " + appRoot.fmtTimeShort(modelData.ts)
                            color: appRoot.fgMuted
                            font.family: appRoot.fontFamily
                            font.pixelSize: 10
                          }
                        }
                      }
                    }

                    // Phase 1 Cloud-Billed Spend list (from Omameter snapshots)
                    Column {
                      visible: appRoot.rightPanelMode === "cloud"
                      width: parent.width
                      spacing: 4

                      PlainText {
                        visible: !appRoot.quotas.cloudSpend || appRoot.quotas.cloudSpend.length === 0
                        text: appRoot.t("analytics.noCloudSpend", "No cloud-billed USD lines reported by connected Omameter accounts.")
                        color: appRoot.fgDim
                        font.family: appRoot.fontFamily
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                        width: parent.width
                      }

                      Repeater {
                        model: appRoot.quotas.cloudSpend || []
                        delegate: Item {
                          required property var modelData
                          width: parent.width
                          height: 28

                          PlainText {
                            anchors.left: parent.left
                            anchors.right: cVal.left
                            anchors.rightMargin: 8
                            anchors.verticalCenter: parent.verticalCenter
                            elide: Text.ElideRight
                            text: String(modelData.providerName || modelData.provider) + " · " + String(modelData.title || "")
                            color: appRoot.fg
                            font.family: appRoot.fontFamily
                            font.pixelSize: 11
                          }

                          PlainText {
                            id: cVal
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            text: appRoot.fmtUsd(modelData.used)
                              + (modelData.limit !== null && modelData.limit !== undefined ? (" / " + appRoot.fmtUsd(modelData.limit)) : "")
                            color: appRoot.accent
                            font.family: appRoot.fontFamily
                            font.pixelSize: 11
                            font.bold: true
                          }
                        }
                      }
                    }
                  }
                }
              }
            }

            // ============================================================
            // TAB 2: QUOTA HISTORY (Phase 1 — Always Active)
            // ============================================================
            Column {
              id: quotasTabCol
              visible: appRoot.activeTab === "quotas"
              width: parent.width
              spacing: 12

              // Period pills + History Metric picker
              Flow {
                width: parent.width
                spacing: 6

                Repeater {
                  model: [
                    { id: "today", key: "analytics.periodToday", fb: "Today" },
                    { id: "7d",    key: "analytics.period7d",    fb: "7d" },
                    { id: "30d",   key: "analytics.period30d",   fb: "30d" },
                    { id: "90d",   key: "analytics.period90d",   fb: "90d" }
                  ]
                  delegate: ChipButton {
                    required property var modelData
                    text: appRoot.t(modelData.key, modelData.fb)
                    selected: appRoot.period === modelData.id
                    onClicked: appRoot.setPeriod(modelData.id)
                  }
                }

                Rectangle {
                  width: 1
                  height: 24
                  color: appRoot.borderSubtle
                }

                Repeater {
                  model: appRoot.quotas.historyMetrics || []
                  delegate: ChipButton {
                    required property var modelData
                    text: String(modelData.provider || "") + " · " + String(modelData.metric_title || modelData.metric_key || "")
                    selected: appRoot.historyAccountId === String(modelData.account_id || "")
                      && appRoot.historyMetricKey === String(modelData.metric_key || "")
                    showDot: true
                    dotColor: appRoot.providerColors[modelData.provider] || appRoot.accent
                    onClicked: {
                      appRoot.historyAccountId = String(modelData.account_id || "")
                      appRoot.historyMetricKey = String(modelData.metric_key || "")
                      appRoot.fetchReport("none")
                    }
                  }
                }
              }

              // Quota Burn-Rate Line Chart Card
              CardBox {
                width: parent.width
                implicitHeight: 270

                Column {
                  anchors.fill: parent
                  anchors.margins: 12
                  spacing: 8

                  Item {
                    width: parent.width
                    height: 22

                    PlainText {
                      anchors.left: parent.left
                      anchors.verticalCenter: parent.verticalCenter
                      text: {
                        var sh = appRoot.quotas.selectedHistory || {}
                        if (sh.metricTitle) {
                          return String(sh.provider || "").toUpperCase() + " — " + String(sh.metricTitle)
                            + (sh.accountName && !appRoot.privacyActive ? (" (" + String(sh.accountName) + ")") : "")
                        }
                        return appRoot.t("analytics.quotaCurveTitle", "Quota Burn-Rate History")
                      }
                      color: appRoot.fg
                      font.family: appRoot.fontFamily
                      font.pixelSize: 12
                      font.bold: true
                    }

                    Row {
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: 14

                      PlainText {
                        readonly property var sh: appRoot.quotas.selectedHistory || {}
                        text: appRoot.t("analytics.quotaStats", "Peak %1% · Avg %2% · Resets: %3 · Samples: %4")
                          .arg(Number(sh.peakPct || 0).toFixed(1))
                          .arg(Number(sh.avgPct || 0).toFixed(1))
                          .arg(Number(sh.resetCount || 0))
                          .arg((sh.points && sh.points.length) ? sh.points.length : 0)
                        color: appRoot.fgMuted
                        font.family: appRoot.fontFamily
                        font.pixelSize: 10
                      }
                    }
                  }

                  QuotaLineChart {
                    width: parent.width
                    height: 216
                    points: (appRoot.quotas.selectedHistory && Array.isArray(appRoot.quotas.selectedHistory.points))
                      ? appRoot.quotas.selectedHistory.points
                      : []
                    warningUsedPct: Math.max(0, Math.min(100, 100 - Number(appRoot.thresholds.warning || 30)))
                    criticalUsedPct: Math.max(0, Math.min(100, 100 - Number(appRoot.thresholds.critical || 10)))
                    foreground: appRoot.fg
                    background: appRoot.bg
                    accent: appRoot.accent
                    warningColor: appRoot.warningColor
                    criticalColor: appRoot.criticalColor
                    fontFamily: appRoot.fontFamily
                    cornerRadius: appRoot.cornerRadius
                    emptyText: appRoot.t("analytics.quotaEmpty", "No quota history recorded yet — samples are saved automatically on every Omameter refresh.")
                  }
                }
              }

              // Live Quotas Grid across all connected Omameter providers
              CardBox {
                width: parent.width
                implicitHeight: liveQCol.implicitHeight + 24

                Column {
                  id: liveQCol
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.top: parent.top
                  anchors.margins: 12
                  spacing: 8

                  PlainText {
                    text: appRoot.t("analytics.liveQuotasTitle", "Live Provider Quotas")
                    color: appRoot.fg
                    font.family: appRoot.fontFamily
                    font.pixelSize: 12
                    font.bold: true
                  }

                  PlainText {
                    visible: !appRoot.quotas.live || appRoot.quotas.live.length === 0
                    text: appRoot.t("analytics.noLiveQuotas", "No live provider quotas cached yet.")
                    color: appRoot.fgDim
                    font.family: appRoot.fontFamily
                    font.pixelSize: 11
                  }

                  Repeater {
                    model: appRoot.quotas.live || []
                    delegate: Rectangle {
                      required property var modelData
                      width: parent.width
                      implicitHeight: 36
                      radius: appRoot.cornerRadius
                      color: lqHov.hovered ? appRoot.surfaceElevated : "transparent"
                      HoverHandler { id: lqHov }

                      Item {
                        anchors.fill: parent
                        anchors.leftMargin: 8
                        anchors.rightMargin: 8

                        Row {
                          anchors.left: parent.left
                          anchors.verticalCenter: parent.verticalCenter
                          spacing: 8

                          Rectangle {
                            width: 8
                            height: 8
                            radius: 4
                            anchors.verticalCenter: parent.verticalCenter
                            color: {
                              var rem = Number(modelData.remainingPct || 0)
                              if (rem <= Number(appRoot.thresholds.critical || 10)) return appRoot.criticalColor
                              if (rem <= Number(appRoot.thresholds.warning || 30)) return appRoot.warningColor
                              return appRoot.goodColor
                            }
                          }

                          PlainText {
                            anchors.verticalCenter: parent.verticalCenter
                            text: String(modelData.providerName || modelData.provider) + " · " + String(modelData.title || "")
                            color: appRoot.fg
                            font.family: appRoot.fontFamily
                            font.pixelSize: 11
                            font.bold: true
                          }

                          PlainText {
                            visible: !appRoot.privacyActive && !!modelData.accountName
                            anchors.verticalCenter: parent.verticalCenter
                            text: String(modelData.accountName || "")
                            color: appRoot.fgDim
                            font.family: appRoot.fontFamily
                            font.pixelSize: 10
                          }
                        }

                        Row {
                          anchors.right: parent.right
                          anchors.verticalCenter: parent.verticalCenter
                          spacing: 10

                          PlainText {
                            anchors.verticalCenter: parent.verticalCenter
                            text: Number(modelData.usedPct || 0).toFixed(0) + "% used (" + Number(modelData.remainingPct || 0).toFixed(0) + "% left)"
                            color: appRoot.fgMuted
                            font.family: appRoot.fontFamily
                            font.pixelSize: 11
                          }

                          ChipButton {
                            visible: !!modelData.canSinceReset
                            anchors.verticalCenter: parent.verticalCenter
                            text: appRoot.t("analytics.openSinceReset", "Tokens since reset →")
                            fontSize: 10
                            padX: 8
                            padY: 3
                            onClicked: {
                              appRoot.providerFilter = String(modelData.provider || "all")
                              appRoot.period = String(modelData.resetWindowId || "today")
                              appRoot.activeTab = "overview"
                              appRoot.fetchReport("none")
                            }
                          }
                        }
                      }

                      MouseArea {
                        anchors.fill: parent
                        z: -1
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                          appRoot.historyAccountId = String(modelData.accountId || "")
                          appRoot.historyMetricKey = String(modelData.metricKey || "")
                          appRoot.fetchReport("none")
                        }
                      }
                    }
                  }
                }
              }
            }

            // ============================================================
            // TAB 3: SOURCES, RETENTION & PRICING
            // ============================================================
            Column {
              id: settingsTabCol
              visible: appRoot.activeTab === "settings"
              width: parent.width
              spacing: 12

              // ---- Local Scanner & Retention ----
              CardBox {
                width: parent.width
                implicitHeight: scannerCol.implicitHeight + 24

                Column {
                  id: scannerCol
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.top: parent.top
                  anchors.margins: 12
                  spacing: 10

                  Item {
                    width: parent.width
                    height: 28

                    Column {
                      anchors.left: parent.left
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: 2

                      PlainText {
                        text: appRoot.t("analytics.scannerTitle", "Local Harness Token Scanner (Phase 2)")
                        color: appRoot.fg
                        font.family: appRoot.fontFamily
                        font.pixelSize: 12
                        font.bold: true
                      }

                      PlainText {
                        text: appRoot.t("analytics.scannerHint", "Reads session logs directly from installed local harnesses. Never depends on external services.")
                        color: appRoot.fgMuted
                        font.family: appRoot.fontFamily
                        font.pixelSize: 10
                      }
                    }

                    Row {
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: 8

                      ChipButton {
                        text: appRoot.localEnabled
                          ? appRoot.t("analytics.statusEnabled", "Enabled")
                          : appRoot.t("analytics.statusDisabled", "Disabled")
                        selected: appRoot.localEnabled
                        onClicked: appRoot.enableLocalAnalytics(!appRoot.localEnabled)
                      }

                      ChipButton {
                        visible: appRoot.localEnabled
                        text: appRoot.scanning
                          ? appRoot.t("analytics.refreshing", "Scanning...")
                          : appRoot.t("analytics.forceRescan", "Force Rescan")
                        onClicked: appRoot.startScan(true)
                      }

                      ChipButton {
                        text: appRoot.t("analytics.clearLedger", "Clear Token History")
                        onClicked: appRoot.sendCmd({ cmd: "clear_token_history" })
                      }
                    }
                  }

                  // Retention selector
                  Row {
                    spacing: 8

                    PlainText {
                      anchors.verticalCenter: parent.verticalCenter
                      text: appRoot.t("analytics.retentionLabel", "Retention:")
                      color: appRoot.fgMuted
                      font.family: appRoot.fontFamily
                      font.pixelSize: 11
                    }

                    Repeater {
                      model: [
                        { days: 30,  label: "30d" },
                        { days: 90,  label: "90d" },
                        { days: 180, label: "180d" },
                        { days: 365, label: "365d" },
                        { days: 0,   label: "Unlimited" }
                      ]
                      delegate: ChipButton {
                        required property var modelData
                        text: modelData.label
                        selected: Number(appRoot.config.retentionDays !== undefined ? appRoot.config.retentionDays : 180) === modelData.days
                        fontSize: 10
                        padX: 8
                        padY: 4
                        onClicked: appRoot.setRetentionDays(modelData.days)
                      }
                    }
                  }
                }
              }

              // ---- 4 Priority Harnesses & Monthly Plan Prices ----
              CardBox {
                width: parent.width
                implicitHeight: sourcesCol.implicitHeight + 24

                Column {
                  id: sourcesCol
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.top: parent.top
                  anchors.margins: 12
                  spacing: 8

                  PlainText {
                    text: appRoot.t("analytics.harnessesTitle", "Local Harnesses & Monthly Plan Cost (for ROI)")
                    color: appRoot.fg
                    font.family: appRoot.fontFamily
                    font.pixelSize: 12
                    font.bold: true
                  }

                  Repeater {
                    model: appRoot.providers
                    delegate: Rectangle {
                      id: srcRow
                      required property var modelData
                      width: parent.width
                      implicitHeight: 44
                      radius: appRoot.cornerRadius
                      color: appRoot.surfaceColor
                      border.width: 1
                      border.color: appRoot.borderSubtle

                      Item {
                        anchors.fill: parent
                        anchors.leftMargin: 10
                        anchors.rightMargin: 10

                        Row {
                          anchors.left: parent.left
                          anchors.verticalCenter: parent.verticalCenter
                          width: Math.max(0, parent.width - srcActions.width - 12)
                          spacing: 8

                          Rectangle {
                            width: 8
                            height: 8
                            radius: 4
                            anchors.verticalCenter: parent.verticalCenter
                            color: appRoot.providerColors[modelData.id] || appRoot.accent
                          }

                          Column {
                            anchors.verticalCenter: parent.verticalCenter
                            width: Math.max(0, parent.width - 16)
                            spacing: 1

                            PlainText {
                              text: String(modelData.name || modelData.id)
                              color: appRoot.fg
                              font.family: appRoot.fontFamily
                              font.pixelSize: 11
                              font.bold: true
                            }

                            PlainText {
                              width: parent.width
                              elide: Text.ElideRight
                              text: {
                                var base = modelData.installed
                                  ? appRoot.t("analytics.harnessInstalled", "Detected · %1 files · %2 events").arg(Number(modelData.fileCount || 0)).arg(Number(modelData.requests || 0))
                                  : appRoot.t("analytics.harnessNotFound", "Not installed on this machine")
                                if (modelData.t3Available && !modelData.t3Enabled)
                                  return base + " · " + appRoot.t("analytics.t3AntigravityFound", "T3 Code sessions found")
                                return base
                              }
                              color: appRoot.fgMuted
                              font.family: appRoot.fontFamily
                              font.pixelSize: 10
                            }
                          }
                        }

                        Row {
                          id: srcActions
                          anchors.right: parent.right
                          anchors.verticalCenter: parent.verticalCenter
                          spacing: 8

                          PlainText {
                            anchors.verticalCenter: parent.verticalCenter
                            text: appRoot.t("analytics.planUsdLabel", "Plan $/mo:")
                            color: appRoot.fgMuted
                            font.family: appRoot.fontFamily
                            font.pixelSize: 10
                          }

                          TextField {
                            id: planInput
                            anchors.verticalCenter: parent.verticalCenter
                            width: 68
                            height: 26
                            text: Number(modelData.planMonthlyUsd || 0) > 0 ? String(modelData.planMonthlyUsd) : ""
                            placeholderText: "0"
                            color: appRoot.fg
                            placeholderTextColor: appRoot.fgDim
                            font.family: appRoot.fontFamily
                            font.pixelSize: 11
                            background: Rectangle {
                              radius: appRoot.cornerRadius
                              color: appRoot.bg
                              border.width: 1
                              border.color: planInput.activeFocus ? appRoot.accent : appRoot.borderSubtle
                            }
                            onAccepted: appRoot.savePlanPrice(modelData.id, text)
                          }

                          ChipButton {
                            anchors.verticalCenter: parent.verticalCenter
                            text: appRoot.t("analytics.saveBtn", "Save")
                            fontSize: 10
                            padX: 8
                            padY: 4
                            onClicked: appRoot.savePlanPrice(modelData.id, planInput.text)
                          }

                          ChipButton {
                            anchors.verticalCenter: parent.verticalCenter
                            visible: !!(modelData.t3Available || modelData.t3Enabled)
                            text: modelData.t3Enabled
                              ? appRoot.t("analytics.t3AntigravityOn", "T3 Code: ON")
                              : appRoot.t("analytics.t3AntigravityOff", "T3 Code: OFF")
                            selected: !!modelData.t3Enabled
                            fontSize: 10
                            padX: 8
                            padY: 4
                            onClicked: appRoot.setT3Antigravity(!modelData.t3Enabled)
                          }

                          ChipButton {
                            anchors.verticalCenter: parent.verticalCenter
                            text: modelData.enabled ? appRoot.t("analytics.on", "ON") : appRoot.t("analytics.off", "OFF")
                            selected: !!modelData.enabled
                            fontSize: 10
                            padX: 8
                            padY: 4
                            onClicked: appRoot.toggleSource(modelData.id, !modelData.enabled)
                          }
                        }
                      }
                    }
                  }
                }
              }

              // ---- Pricing Catalog, LiteLLM Sync & Custom Model Prices / Aliases ----
              CardBox {
                width: parent.width
                implicitHeight: pricingCol.implicitHeight + 24

                Column {
                  id: pricingCol
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.top: parent.top
                  anchors.margins: 12
                  spacing: 10

                  Item {
                    width: parent.width
                    height: 28

                    Column {
                      anchors.left: parent.left
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: 2

                      PlainText {
                        text: appRoot.t("analytics.pricingTitle", "Model Pricing Catalog & Custom Overrides")
                        color: appRoot.fg
                        font.family: appRoot.fontFamily
                        font.pixelSize: 12
                        font.bold: true
                      }

                      PlainText {
                        text: appRoot.t("analytics.pricingMeta", "Bundled: %1 models · LiteLLM cache: %2 models")
                          .arg(Number(appRoot.pricingInfo.bundledCount || appRoot.pricingInfo.bundledModels || 0))
                          .arg(Number(appRoot.pricingInfo.litellmCount || appRoot.pricingInfo.litellmModels || 0))
                        color: appRoot.fgMuted
                        font.family: appRoot.fontFamily
                        font.pixelSize: 10
                      }
                    }

                    ChipButton {
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      text: appRoot.pricingRefreshing
                        ? appRoot.t("analytics.updatingPricing", "Updating...")
                        : appRoot.t("analytics.updateLitellm", "Update from LiteLLM")
                      selected: true
                      onClicked: appRoot.refreshPricingOnline()
                    }
                  }

                  // Unpriced models quick-select chips
                  Column {
                    visible: appRoot.unpricedModels.length > 0
                    width: parent.width
                    spacing: 6

                    PlainText {
                      text: appRoot.t("analytics.unpricedHeader", "Unpriced models detected (click to map or price):")
                      color: appRoot.warningColor
                      font.family: appRoot.fontFamily
                      font.pixelSize: 11
                      font.bold: true
                    }

                    Flow {
                      width: parent.width
                      spacing: 6

                      Repeater {
                        model: appRoot.unpricedModels
                        delegate: ChipButton {
                          required property var modelData
                          text: String(modelData.model || "") + " (" + appRoot.fmtTokens(modelData.tokens) + ")"
                          selected: appRoot.editModelName === String(modelData.model || "")
                          onClicked: {
                            appRoot.editModelName = String(modelData.model || "")
                            appRoot.editAliasTarget = ""
                          }
                        }
                      }
                    }
                  }

                  // Inline Override / "Map to" editor
                  Flow {
                    width: parent.width
                    spacing: 8

                    TextField {
                      id: edModel
                      width: 200
                      height: 28
                      text: appRoot.editModelName
                      placeholderText: appRoot.t("analytics.modelNamePh", "Model ID (e.g. gpt-6.2)")
                      color: appRoot.fg
                      placeholderTextColor: appRoot.fgDim
                      font.family: appRoot.fontFamily
                      font.pixelSize: 11
                      background: Rectangle {
                        radius: appRoot.cornerRadius
                        color: appRoot.bg
                        border.width: 1
                        border.color: edModel.activeFocus ? appRoot.accent : appRoot.borderSubtle
                      }
                      onTextEdited: appRoot.editModelName = text
                    }

                    TextField {
                      id: edAlias
                      width: 190
                      height: 28
                      text: appRoot.editAliasTarget
                      placeholderText: appRoot.t("analytics.mapToPh", "Map to model (optional)")
                      color: appRoot.fg
                      placeholderTextColor: appRoot.fgDim
                      font.family: appRoot.fontFamily
                      font.pixelSize: 11
                      background: Rectangle {
                        radius: appRoot.cornerRadius
                        color: appRoot.bg
                        border.width: 1
                        border.color: edAlias.activeFocus ? appRoot.accent : appRoot.borderSubtle
                      }
                      onTextEdited: appRoot.editAliasTarget = text
                    }

                    TextField {
                      id: edIn
                      width: 95
                      height: 28
                      text: appRoot.editInputPerM
                      placeholderText: "In $/1M"
                      color: appRoot.fg
                      placeholderTextColor: appRoot.fgDim
                      font.family: appRoot.fontFamily
                      font.pixelSize: 11
                      background: Rectangle {
                        radius: appRoot.cornerRadius
                        color: appRoot.bg
                        border.width: 1
                        border.color: edIn.activeFocus ? appRoot.accent : appRoot.borderSubtle
                      }
                      onTextEdited: appRoot.editInputPerM = text
                    }

                    TextField {
                      id: edOut
                      width: 95
                      height: 28
                      text: appRoot.editOutputPerM
                      placeholderText: "Out $/1M"
                      color: appRoot.fg
                      placeholderTextColor: appRoot.fgDim
                      font.family: appRoot.fontFamily
                      font.pixelSize: 11
                      background: Rectangle {
                        radius: appRoot.cornerRadius
                        color: appRoot.bg
                        border.width: 1
                        border.color: edOut.activeFocus ? appRoot.accent : appRoot.borderSubtle
                      }
                      onTextEdited: appRoot.editOutputPerM = text
                    }

                    ChipButton {
                      text: appRoot.t("analytics.applyPriceBtn", "Apply")
                      selected: appRoot.editModelName.trim() !== ""
                      onClicked: appRoot.saveCustomPrice()
                    }
                  }

                  // Active overrides & aliases list
                  Flow {
                    width: parent.width
                    spacing: 6

                    Repeater {
                      model: Object.keys((appRoot.config && appRoot.config.modelAliases) || {})
                      delegate: ChipButton {
                        required property string modelData
                        text: modelData + " → " + String(appRoot.config.modelAliases[modelData] || "") + " ×"
                        fontSize: 10
                        onClicked: appRoot.removeModelAlias(modelData)
                      }
                    }

                    Repeater {
                      model: Object.keys((appRoot.config && appRoot.config.priceOverrides) || {})
                      delegate: ChipButton {
                        required property string modelData
                        readonly property var ov: appRoot.config.priceOverrides[modelData] || ({})
                        text: modelData + " ($" + Number(ov.inputPerM || 0) + "/$" + Number(ov.outputPerM || 0) + ") ×"
                        fontSize: 10
                        onClicked: appRoot.removePriceOverride(modelData)
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
