import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import "Model.js" as Model
import "Settings.js" as Settings
import "Protocol.js" as Protocol

// Long-lived Omarchy shell service for Omameter.
// Owns the single Python NDJSON daemon (`backend/daemon.py`), shares reactive
// dashboard/account/settings state across all monitors, watches the active
// Omarchy theme palette, and exposes ShellIpc target `paulimfavarato.omameter`.
Item {
  id: root

  // Daemon state properties
  property bool ready: false
  property var dashboard: Model.emptyDashboard()
  property var accounts: []
  property var hubs: []
  property var settings: Settings.defaultSettings()
  property var layout: ({})
  property var proxy: ({ enabled: false, url: "" })
  property bool canUndo: false
  property string nextRefreshAt: ""
  property bool refreshing: false
  property var authSession: null
  property string toastMessage: ""
  property bool toastIsError: false
  property int toastSeq: 0
  property var providerCatalog: []
  property var detectedCredentials: []
  property string daemonError: ""

  // NDJSON connection state (Protocol.js): command ids, pending and settled outcomes.
  // Every command function returns its id; commandSettled fires once per id.
  property var protocol: Protocol.initialState(Date.now().toString(36))
  signal commandSettled(string id, bool ok, string error)
  property string _detectId: ""
  // Scanning = the last detect_local_credentials is still pending (a failure frees the button).
  readonly property bool detectingLocal: _detectId !== "" && Protocol.outcome(protocol, _detectId).status === "pending"

  // i18n catalog for the service's own messages; its language is also pushed to
  // the daemon (set_language) so toasts, errors and notifications follow it.
  Commons.Translations {
    id: strings
    directory: Qt.resolvedUrl("i18n")
  }
  function t(key, fb) { return Model.tr(strings, key, fb) }
  readonly property string language: strings.language
  onLanguageChanged: if (ready) sendLanguage()
  function sendLanguage() { return sendCmd({ cmd: "set_language", language: root.language }) }

  // Live clock for countdown strings ("Next update in ...", reset expiries)
  property real nowMs: Date.now()

  // Theme colors parsed from active Omarchy theme colors.toml
  property var themeColors: ({})
  readonly property var palette: Model.pickThemePalette(
    themeColors,
    Commons.Color.popups.text,
    Commons.Color.popups.background,
    Commons.Color.accent,
    Commons.Color.urgent
  )

  // Internal lifecycle & widget registry
  property var instances: []
  property int restartCount: 0
  property bool destroying: false
  // Settings reconciliation with shell.json (see Settings.reconcile)
  property var _widgetSeen: ({})
  property var _widgetPending: ({})
  property bool _writingShellSettings: false

  readonly property string daemonPath: Qt.resolvedUrl("backend/daemon.py").toString().replace(/^file:\/\//, "")

  function registerInstance(item) {
    if (!item) return
    var next = instances.filter(function(x) { return x && x !== item })
    next.push(item)
    instances = next
  }

  function unregisterInstance(item) {
    instances = instances.filter(function(x) { return x && x !== item })
  }

  // dashboard.bar for the `status` IPC, with each item's resetText written live
  // (the daemon sends only resetsAt) so scripts keep reading the same field.
  function statusBar() {
    var bar = dashboard ? dashboard.bar : null
    if (!bar) return null
    var opts = {
      resetDisplayMode: settings && settings.resetDisplayMode ? String(settings.resetDisplayMode) : "relative",
      timeFormat: settings && settings.timeFormat ? String(settings.timeFormat) : "auto",
      nowMs: Date.now()
    }
    function withResetText(list) {
      return (list || []).map(function(it) {
        var copy = {}
        for (var k in it) copy[k] = it[k]
        copy.resetText = Model.describeLine(it, opts).resetText
        return copy
      })
    }
    var out = {}
    for (var key in bar) out[key] = bar[key]
    out.items = withResetText(bar.items)
    out.allItems = withResetText(bar.allItems)
    return out
  }

  // Send one NDJSON command to the daemon and return its id. The outcome arrives
  // as commandSettled(id, ok, error) (also readable with commandOutcome(id)).
  // With the daemon down the command fails right away (and the daemon is started).
  function sendCmd(obj) {
    if (!obj || typeof obj !== "object") return ""
    var enc = Protocol.encode(protocol, obj.cmd, obj)
    protocol = enc.state
    if (!daemonProc.running) {
      if (!destroying) daemonProc.running = true
      settleLater(enc.id, t("daemon.starting", "The Omameter daemon is not running; starting it. Try again in a moment."))
      return enc.id
    }
    try {
      daemonProc.write(enc.line + "\n")
    } catch (e) {
      console.warn("paulimfavarato.omameter: write failed:", e)
      settleLater(enc.id, t("daemon.sendFailed", "Could not send the command to the Omameter daemon."))
    }
    return enc.id
  }

  // Fail a command that never reached the daemon. Deferred so callers get the
  // id before commandSettled fires, the same order as a daemon answer.
  function settleLater(id, message) {
    Qt.callLater(function() { root.applyEvents(Protocol.settle(root.protocol, id, false, message)) })
  }

  function commandOutcome(id) {
    return Protocol.outcome(protocol, id)
  }

  // ---- Daemon RPC wrappers (each returns the command id) ----
  function getState() { return sendCmd({ cmd: "get_state" }) }
  function refreshAll(force) { return sendCmd({ cmd: "refresh_all", force: force !== false }) }
  function refreshAccount(accountId, force) { return sendCmd({ cmd: "refresh_account", accountId: String(accountId || ""), force: force !== false }) }
  function sendSettingsPatch(patch) { return (patch && typeof patch === "object") ? sendCmd({ cmd: "update_settings", patch: patch }) : "" }

  // The one way to change a setting: normalize, send to the daemon
  // (settings.json) and write the same values to shell.json (Omarchy Setup ->
  // Plugins). Values written to shell.json are marked as seen first, so the
  // echo that comes back through the widget's onSettingsChanged sends nothing.
  function changeSettings(patch) {
    var next = Settings.normalizePatch(patch, settings)
    if (!next) return
    sendSettingsPatch(next)
    persistShellSettings(next)
  }
  function toggleDisplayMode() {
    changeSettings({ displayMode: (settings && settings.displayMode === "used") ? "remaining" : "used" })
  }
  function toggleResetMode() {
    changeSettings({ resetDisplayMode: (settings && settings.resetDisplayMode === "absolute") ? "relative" : "absolute" })
  }

  function shellPluginRegistry() {
    for (var i = 0; i < instances.length; i++) {
      var item = instances[i]
      var registry = item && item.bar && item.bar.shell ? item.bar.shell.pluginRegistry : null
      if (registry && typeof registry.setBarWidget === "function") return registry
    }
    return null
  }

  // shell.json is one entry for this widget, so one write serves every monitor.
  // Each setBarWidget re-injects settings synchronously; the flag keeps those
  // half-written states (one key new, the next still old) from being read as edits.
  function persistShellSettings(patch) {
    var registry = shellPluginRegistry()
    if (!registry) return
    var written = {}
    _writingShellSettings = true
    try {
      for (var k in patch) {
        try {
          if (registry.setBarWidget("paulimfavarato.omameter", k, patch[k]) === "") written[k] = patch[k]
        } catch (e) {
          // Non-fatal: daemon settings.json remains authoritative
        }
      }
    } finally {
      _writingShellSettings = false
    }
    _widgetSeen = Settings.markWritten(_widgetSeen, written)
  }
  function updateProxy(enabled, url) { return sendCmd({ cmd: "update_proxy", enabled: !!enabled, url: String(url || "") }) }
  function setMetricPinned(metricIds, pinned) { return sendCmd({ cmd: "set_metric_pinned", metricIds: metricIds || [], pinned: !!pinned }) }
  function setMetricHidden(metricIds, hidden) { return sendCmd({ cmd: "set_metric_hidden", metricIds: metricIds || [], hidden: !!hidden }) }
  function setProviderUnified(kind, unified) { return sendCmd({ cmd: "set_provider_unified", kind: String(kind || ""), unified: !!unified }) }
  function moveProvider(kind, direction) { return sendCmd({ cmd: "move_provider", kind: String(kind || ""), direction: Number(direction) || 0 }) }
  function moveMetric(accountId, key, direction) { return sendCmd({ cmd: "move_metric", accountId: String(accountId || ""), key: String(key || ""), direction: Number(direction) || 0 }) }
  function undoLayout() { return sendCmd({ cmd: "undo_layout" }) }
  function renameAccount(accountId, label) { return sendCmd({ cmd: "rename_account", accountId: String(accountId || ""), label: String(label || "") }) }
  function removeAccount(accountId) { return sendCmd({ cmd: "remove_account", accountId: String(accountId || "") }) }
  function startAuth(kind, method, accountId, domain) {
    var payload = { cmd: "start_auth", kind: String(kind || ""), method: String(method || "oauth") }
    if (accountId) payload.accountId = String(accountId)
    if (domain) payload.domain = String(domain)
    return sendCmd(payload)
  }
  function submitAuthCode(code) { return sendCmd({ cmd: "submit_auth_code", code: String(code || "") }) }
  function cancelAuth() { return sendCmd({ cmd: "cancel_auth" }) }
  function addApiKeyAccount(kind, apiKey, organization, accountId) {
    return sendCmd({
      cmd: "add_api_key_account",
      kind: String(kind || ""),
      apiKey: String(apiKey || ""),
      organization: String(organization || ""),
      accountId: String(accountId || "")
    })
  }
  function addHub(address, managementKey) { return sendCmd({ cmd: "add_hub", address: String(address || ""), managementKey: String(managementKey || "") }) }
  function replaceHubKey(hubId, managementKey) { return sendCmd({ cmd: "replace_hub_key", hubId: String(hubId || ""), managementKey: String(managementKey || "") }) }
  function removeHub(hubId) { return sendCmd({ cmd: "remove_hub", hubId: String(hubId || "") }) }
  function syncHubs(force) { return sendCmd({ cmd: "sync_hubs", force: force !== false }) }
  function claimReset(accountId, resetItem) { return sendCmd({ cmd: "claim_reset", accountId: String(accountId || ""), resetItem: resetItem || ({}) }) }
  function detectLocalCredentials() {
    if (detectingLocal) return _detectId
    _detectId = sendCmd({ cmd: "detect_local_credentials" })
    return _detectId
  }
  function importLocalCredential(candidateId) {
    return sendCmd({ cmd: "import_local_credential", candidateId: String(candidateId || "") })
  }
  function copyText(text) { return sendCmd({ cmd: "copy_text", text: String(text || "") }) }

  function openUrl(url) {
    if (Model.isSafeExternalUrl(url)) {
      Qt.openUrlExternally(String(url).trim())
    }
  }

  // Sync changes made in Omarchy Setup -> Plugins (shell.json) into the daemon.
  // Every widget instance calls this with the same settings; the shared
  // _widgetSeen makes only the first call send. widgetSettings null = flush pending.
  function syncWidgetSettings(widgetSettings) {
    if (_writingShellSettings) return
    var r = Settings.reconcile(_widgetSeen, widgetSettings, ready, _widgetPending)
    _widgetSeen = r.lastSeen
    _widgetPending = r.pending
    if (r.patch) sendSettingsPatch(r.patch)
  }

  onReadyChanged: {
    if (!ready) return
    sendLanguage()
    syncWidgetSettings(null)
  }

  function ingestLine(line) {
    applyEvents(Protocol.ingest(protocol, line))
  }

  // Apply what Protocol.js decided: new protocol state, then each event to the
  // properties the widgets bind to.
  function applyEvents(r) {
    protocol = r.state
    for (var i = 0; i < r.events.length; i++) {
      var ev = r.events[i]
      if (ev.type === "state") {
        root.dashboard = ev.dashboard || Model.emptyDashboard()
        root.accounts = ev.accounts
        root.hubs = ev.hubs
        root.settings = ev.settings || Settings.defaultSettings()
        root.layout = ev.layout || ({})
        root.proxy = ev.proxy || ({ enabled: false, url: "" })
        root.canUndo = ev.canUndo
        root.nextRefreshAt = ev.nextRefreshAt
        root.refreshing = ev.refreshing
        root.authSession = ev.authSession
        if (ev.providerCatalog) root.providerCatalog = ev.providerCatalog
        root.daemonError = ""
        root.ready = true
        root.nowMs = Date.now()
      } else if (ev.type === "authSession") {
        root.authSession = ev.session
      } else if (ev.type === "localCredentials") {
        root.detectedCredentials = ev.items
      } else if (ev.type === "toast") {
        root.toastMessage = Model.cleanText(ev.message, 260)
        root.toastIsError = ev.error
        root.toastSeq += 1
      } else if (ev.type === "result") {
        root.commandSettled(ev.id, ev.ok, Model.cleanText(ev.error, 260))
      }
    }
  }

  // Route IPC open/close/toggle through the shell so the focused monitor opens
  function invokePanelAction(mode) {
    var live = instances.filter(function(item) { return !!item })
    if (live.length === 0) return "no-widget"
    var shell = live[0].bar ? live[0].bar.shell : null
    if (shell) {
      if (mode === "hide" && typeof shell.hide === "function") {
        shell.hide("paulimfavarato.omameter")
        return "ok"
      }
      if (mode === "toggle" && typeof shell.toggle === "function") {
        shell.toggle("paulimfavarato.omameter", "{}")
        return "ok"
      }
      if (mode === "show" && typeof shell.summon === "function") {
        shell.summon("paulimfavarato.omameter", "{}")
        return "ok"
      }
    }
    // Direct fallback
    var target = live[0]
    if (mode === "hide" && typeof target.close === "function") target.close()
    else if (mode === "toggle" && typeof target.toggle === "function") target.toggle()
    else if (mode === "show" && typeof target.open === "function") target.open()
    return "ok"
  }

  Process {
    id: daemonProc
    command: ["/usr/bin/python3", "-u", root.daemonPath]
    running: true
    stdinEnabled: true

    stdout: SplitParser {
      onRead: function(line) { root.ingestLine(line) }
    }

    stderr: SplitParser {
      onRead: function(line) {
        var cleaned = Model.cleanText(line, 240).trim()
        if (cleaned !== "") {
          root.daemonError = cleaned
        }
      }
    }

    onStarted: {
      root.daemonError = ""
      root.getState()
    }

    onExited: function(code, status) {
      root.ready = false
      root.refreshing = false
      if (root.daemonError === "" && code !== 0 && !root.destroying) {
        root.daemonError = t("daemon.exited", "Daemon exited with code %1").arg(code)
      }
      root.applyEvents(Protocol.daemonDown(root.protocol,
        root.daemonError !== "" ? root.daemonError : t("daemon.stopped", "The Omameter daemon stopped before answering.")))
      if (root.destroying) return
      root.restartCount += 1
      restartTimer.interval = Math.min(30000, 1000 * Math.pow(2, Math.min(5, root.restartCount - 1)))
      restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    repeat: false
    onTriggered: {
      if (!root.destroying && !daemonProc.running) {
        daemonProc.running = true
      }
    }
  }

  // Reset restart backoff after 60s of healthy operation
  Timer {
    interval: 60000
    repeat: true
    running: root.ready
    onTriggered: root.restartCount = 0
  }

  // Keep relative countdowns ("Next update in ...") smoothly refreshed
  Timer {
    interval: 15000
    repeat: true
    running: true
    onTriggered: root.nowMs = Date.now()
  }

  // Watch active Omarchy theme colors.toml for warm warning/good hues
  FileView {
    id: colorsFile
    path: Commons.Color.currentThemePath + "/colors.toml"
    watchChanges: true
    printErrors: false
    onLoaded: root.themeColors = Model.parseColorsToml(text())
    onFileChanged: reload()
    onLoadFailed: root.themeColors = ({})
  }

  Connections {
    target: Commons.Color
    function onAccentChanged() { colorsFile.reload() }
    function onBackgroundChanged() { colorsFile.reload() }
    function onForegroundChanged() { colorsFile.reload() }
  }

  Component.onDestruction: {
    root.destroying = true
    restartTimer.stop()
    if (daemonProc.running) {
      daemonProc.running = false
    }
  }

  // Global shortcut / CLI IPC target: `omarchy-shell paulimfavarato.omameter toggle`
  ShellIpc {
    target: "paulimfavarato.omameter"

    function toggle(): string { return root.invokePanelAction("toggle") }
    function open(): string { return root.invokePanelAction("show") }
    function show(): string { return root.invokePanelAction("show") }
    function close(): string { return root.invokePanelAction("hide") }
    function hide(): string { return root.invokePanelAction("hide") }
    function refresh(): string { root.refreshAll(true); return "ok" }
    function status(): string {
      return JSON.stringify({
        ready: root.ready,
        refreshing: root.refreshing,
        accountCount: root.dashboard ? (root.dashboard.accountCount || 0) : 0,
        bar: root.statusBar(),
        error: root.daemonError
      })
    }
  }
}
