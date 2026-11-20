import QtQuick
import QtQuick.Controls
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model
import "Settings.js" as Settings
import "Navigation.js" as Navigation
import "ui" as UI

// Top-bar widget and KeyboardPanel popup for Omameter.
// Connects to the shared OmameterService for live daemon state across monitors.
Panel {
  id: root
  moduleName: "paulimfavarato.omameter"
  ipcTarget: ""

  readonly property var service: bar && bar.shell ? bar.shell.serviceFor("paulimfavarato.omameter") : null
  readonly property bool vertical: bar ? bar.vertical : false
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color fg: Commons.Color.popups.text
  readonly property color bg: Commons.Color.popups.background
  readonly property color accentColor: Commons.Color.accent
  readonly property color urgentColor: bar ? bar.urgent : Commons.Color.urgent

  readonly property var dashboard: service && service.dashboard ? service.dashboard : Model.emptyDashboard()
  readonly property var daemonSettings: Settings.normalize(service ? service.settings : null)
  readonly property var palette: service && service.palette
    ? service.palette
    : Model.pickThemePalette({}, fg, bg, accentColor, urgentColor)
  property int barCycleOffset: 0
  readonly property var barData: dashboard && dashboard.bar ? dashboard.bar : Model.emptyDashboard().bar
  readonly property var barAllItems: (barData && barData.allItems && barData.allItems.length > 0)
    ? barData.allItems
    : ((barData && barData.items && barData.items.length > 0) ? barData.items : [])
  readonly property string barItemsSignature: {
    var all = root.barAllItems
    if (!all || !all.length) return ""
    var parts = []
    for (var i = 0; i < all.length; i++) {
      var it = all[i]
      parts.push(it && it.metricId ? String(it.metricId) : String(i))
    }
    return parts.join("|")
  }
  readonly property int barVisibleCount: Math.max(0, Math.min(barAllItems.length, daemonSettings.maxBarItems))
  readonly property var barItems: {
    var all = root.barAllItems
    var vis = root.barVisibleCount
    if (all.length <= vis || vis <= 0 || root.barCycleOffset === 0) {
      return (root.barData && root.barData.items && root.barData.items.length > 0) ? root.barData.items : []
    }
    var n = all.length
    var start = ((root.barCycleOffset % n) + n) % n
    var out = []
    for (var i = 0; i < vis; i++) {
      out.push(all[(start + i) % n])
    }
    return out
  }
  readonly property bool showIconOnly: vertical || !service || !service.ready || !!barData.iconOnly || barItems.length === 0
  readonly property bool privacyMode: daemonSettings.privacyMode
  readonly property string barLabelStyle: String(daemonSettings.barLabelStyle || "full")
  // Options for Model.describeLine, shared by the chip strip, the bar tooltip and Copy Summary.
  readonly property var lineOpts: ({
    resetDisplayMode: daemonSettings.resetDisplayMode,
    timeFormat: daemonSettings.timeFormat,
    nowMs: service ? service.nowMs : Date.now(),
    privacyMode: privacyMode,
    palette: palette,
    strings: strings
  })

  onBarItemsSignatureChanged: {
    if (barCycleOffset !== 0) barCycleOffset = 0
  }

  onBarVisibleCountChanged: {
    if (barAllItems.length <= barVisibleCount && barCycleOffset !== 0) {
      barCycleOffset = 0
    }
  }

  // Popup + accounts navigation state, owned by Navigation.js (reduce + effects).
  property var nav: Navigation.initialState(Settings.defaultSettings())
  readonly property string viewMode: nav.viewMode            // "dashboard" | "accounts" | "settings"
  readonly property string dashboardTab: nav.dashboardTab    // "priority" | "all"
  property bool copiedFeedback: false
  property string copyCommandId: ""   // the Copy Summary command whose outcome shows "Copied"
  property bool toastVisible: false
  property string activeToastText: ""
  property bool activeToastError: false

  // Live i18n catalog (en.json / pt-BR.json) synced with Omarchy Setup -> Language
  Commons.Translations {
    id: strings
    directory: Qt.resolvedUrl("i18n")
  }

  function t(key, fb) { return Model.tr(strings, key, fb) }

  function refresh() {
    if (service) service.refreshAll(true)
  }

  // The one entry point for navigation: Navigation.reduce decides the next state
  // and the effects (service commands, scroll reset, open/close) run here.
  function dispatch(action) {
    var result = Navigation.reduce(nav, action, {
      authSession: service ? service.authSession : null,
      accounts: service ? service.accounts : [],
      settings: daemonSettings,
      opened: opened
    })
    if (result.state !== nav) nav = result.state
    for (var i = 0; i < result.effects.length; i++) runEffect(result.effects[i])
  }

  function runEffect(e) {
    if (e.ui === "resetScroll") { resetScroll(); return }
    if (e.ui === "open") { if (!opened) open(); return }
    if (e.ui === "close") { close(); return }
    if (!service) {
      if (e.awaits) dispatch({ type: "commandSent", form: e.awaits, id: "", error: t("accounts.serviceMissing", "Omameter service is not loaded.") })
      return
    }
    var id = ""
    switch (e.cmd) {
    case "cancelAuth": id = service.cancelAuth(); break
    case "startAuth": id = service.startAuth(e.kind, e.method, e.accountId, e.domain); break
    case "submitAuthCode": id = service.submitAuthCode(e.code); break
    case "addApiKeyAccount": id = service.addApiKeyAccount(e.kind, e.apiKey, e.organization, e.accountId); break
    case "addHub": id = service.addHub(e.address, e.managementKey); break
    case "renameAccount": id = service.renameAccount(e.accountId, e.label); break
    case "replaceHubKey": id = service.replaceHubKey(e.hubId, e.managementKey); break
    case "removeAccount": id = service.removeAccount(e.accountId); break
    case "removeHub": id = service.removeHub(e.hubId); break
    case "detectLocalCredentials": id = service.detectLocalCredentials(); break
    }
    // A waiting form learns its command id; each widget instance settles only the ids it waits for.
    if (e.awaits) dispatch({ type: "commandSent", form: e.awaits, id: String(id || "") })
  }

  function openAccounts(accountId) {
    dispatch({ type: "openAccounts", accountId: accountId || "" })
  }

  function openAddAccount() {
    dispatch({ type: "openAddAccount" })
  }

  function openSettings() {
    dispatch({ type: "openSettings" })
  }

  function setDashboardTab(tab) {
    dispatch({ type: "setDashboardTab", tab: tab })
  }

  function goBackOrClose() {
    dispatch({ type: "back" })
  }

  function resetScroll() {
    if (scrollArea.contentItem) scrollArea.contentItem.contentY = 0
  }

  function copySummary() {
    if (!service) return
    var opts = {}
    for (var k in lineOpts) opts[k] = lineOpts[k]
    opts.nowMs = service.nowMs
    var text = Model.formatSummaryText(dashboard, opts)
    copiedFeedback = false
    copyCommandId = service.copyText(text)
  }

  property bool hoverOpen: false

  function dismissHover() {
    hoverOpenTimer.stop()
    hoverCloseTimer.stop()
    hoverOpen = false
  }

  function canOpenHover() {
    if (root.opened || !root.visible) return false
    if (root.bar && root.bar.activePopout && root.bar.activePopout !== hoverOwner) return false
    return true
  }

  onOpenedChanged: {
    if (opened) {
      dismissHover()
      dispatch({ type: "popupOpened", session: service ? service.authSession : null })
      if (service) service.nowMs = Date.now()
    }
  }

  onSettingsChanged: {
    if (service) service.syncWidgetSettings(settings)
  }

  onServiceChanged: {
    if (service) {
      service.registerInstance(root)
      service.syncWidgetSettings(settings)
    }
  }

  Component.onCompleted: {
    if (service) {
      service.registerInstance(root)
      service.syncWidgetSettings(settings)
    }
  }

  Component.onDestruction: {
    if (service) service.unregisterInstance(root)
  }

  Connections {
    target: root.service
    function onAuthSessionChanged() {
      root.dispatch({ type: "authSessionChanged", session: root.service ? root.service.authSession : null })
    }
    // "Copied" only after the daemon confirms; a failure keeps its error toast.
    function onCommandSettled(id, ok, error) {
      if (id === root.copyCommandId) {
        root.copyCommandId = ""
        if (ok) {
          root.copiedFeedback = true
          copiedTimer.restart()
        }
      }
      root.dispatch({ type: "commandSettled", id: id, ok: ok, error: error })
    }
    function onToastSeqChanged() {
      if (!root.service || !root.service.toastMessage) return
      root.activeToastText = root.service.toastMessage
      root.activeToastError = root.service.toastIsError
      root.toastVisible = true
      toastTimer.restart()
    }
  }

  Timer {
    id: toastTimer
    interval: 4000
    repeat: false
    onTriggered: root.toastVisible = false
  }

  Timer {
    id: copiedTimer
    interval: 1800
    repeat: false
    onTriggered: root.copiedFeedback = false
  }

  QtObject {
    id: hoverOwner
    function close() { root.dismissHover() }
  }

  Timer {
    id: hoverOpenTimer
    interval: 320
    repeat: false
    onTriggered: {
      if (barButton.tooltipHovered && root.canOpenHover()) {
        if (root.service) root.service.nowMs = Date.now()
        root.hoverOpen = true
      }
    }
  }

  Timer {
    id: hoverCloseTimer
    interval: 140
    repeat: false
    onTriggered: {
      if (!barButton.tooltipHovered && !hoverCard.containsMouse) {
        root.hoverOpen = false
      }
    }
  }

  implicitWidth: barButton.implicitWidth
  implicitHeight: barButton.implicitHeight

  // ==================== TOP BAR STRIP ====================
  WidgetButton {
    id: barButton
    anchors.fill: parent
    bar: root.bar
    text: " "
    labelVisible: false
    hasVisualContent: true
    fontSize: Style.font.bodySmall
    active: !!(root.barData && root.barData.alarming)
    horizontalMargin: 7
    fixedWidth: root.vertical ? -1
      : (root.showIconOnly
        ? Style.bar.iconSlot
        : Math.max(Style.bar.iconSlot, chipStrip.implicitWidth + Style.spaceReal(14)))
    tooltipText: ""

    onTooltipHoveredChanged: {
      if (tooltipHovered) {
        hoverCloseTimer.stop()
        if (!root.hoverOpen && root.canOpenHover()) hoverOpenTimer.restart()
      } else {
        hoverOpenTimer.stop()
        if (root.hoverOpen) hoverCloseTimer.restart()
      }
    }

    onPressed: function(buttonCode) {
      root.dismissHover()
      if (buttonCode === Qt.RightButton) {
        root.openSettings()
      } else if (buttonCode === Qt.MiddleButton) {
        root.refresh()
      } else {
        root.toggle()
      }
    }

    onWheelMoved: function(delta) {
      if (root.showIconOnly || root.barAllItems.length <= Math.max(1, root.barVisibleCount) || delta === 0) return
      var step = delta < 0 ? 1 : -1
      var n = root.barAllItems.length
      root.barCycleOffset = ((root.barCycleOffset + step) % n + n) % n
    }

    // Icon-only mode (vertical bar, privacy mode, maxBarItems=0, no urgent items)
    UI.BrandMark {
      visible: root.showIconOnly
      anchors.centerIn: parent
      icon: "omameter.svg"
      iconSize: Style.bar.iconSize + 2
      tintColor: (root.barData && root.barData.alarming && barButton.useActiveColor)
        ? barButton.activeColor
        : barButton.foreground
      badgeBackground: root.bar ? root.bar.background : Commons.Color.background
      fontFamily: barButton.fontFamily
    }

    // Horizontal chips strip: [BrandMark (+ badge)] [glyph|label] [trayValue] ... +N
    Row {
      id: chipStrip
      visible: !root.showIconOnly
      anchors.centerIn: parent
      spacing: root.barLabelStyle === "full" ? Style.space(9) : Style.space(8)

      Repeater {
        model: root.barItems

        delegate: Row {
          required property var modelData
          readonly property var desc: Model.describeLine(modelData, {
            palette: root.palette,
            foreground: barButton.foreground,
            urgent: barButton.activeColor
          })
          readonly property var chipDisplay: Model.barChipDisplay(modelData, root.barLabelStyle)
          readonly property color chipColor: desc.color

          spacing: root.barLabelStyle === "full" ? Style.space(4) : Style.space(3)

          UI.BrandMark {
            anchors.verticalCenter: parent.verticalCenter
            icon: modelData.icon || "omameter.svg"
            iconSize: Style.bar.iconSize
            tintColor: chipColor
            badgeBackground: root.bar ? root.bar.background : Commons.Color.background
            fontFamily: barButton.fontFamily
            badge: modelData.badge ? String(modelData.badge) : ""
            compactBadge: true
          }

          UI.PlainText {
            visible: chipDisplay.glyph !== ""
            anchors.verticalCenter: parent.verticalCenter
            text: chipDisplay.glyph
            color: Qt.rgba(chipColor.r, chipColor.g, chipColor.b, 0.72)
            font.family: barButton.fontFamily
            font.pixelSize: Math.max(10, Style.font.bodySmall - 1)
          }

          UI.PlainText {
            visible: chipDisplay.label !== ""
            anchors.verticalCenter: parent.verticalCenter
            text: chipDisplay.label
            color: Qt.rgba(chipColor.r, chipColor.g, chipColor.b, root.barLabelStyle === "compact" ? 0.72 : 0.82)
            font.family: barButton.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          UI.PlainText {
            visible: !!(modelData.value && String(modelData.value) !== "")
            anchors.verticalCenter: parent.verticalCenter
            text: String(modelData.value || "")
            color: chipColor
            font.family: barButton.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.bold: modelData.severity === "critical" || modelData.severity === "warning"
          }
        }
      }

      // +N overflow indicator
      UI.Pill {
        visible: Number(root.barData.overflow) > 0
        anchors.verticalCenter: parent.verticalCenter
        paddingX: Style.space(6)
        paddingY: Style.space(2)
        color: Qt.rgba(barButton.foreground.r, barButton.foreground.g, barButton.foreground.b, 0.14)
        text: "+" + Number(root.barData.overflow)
        textColor: barButton.foreground
        fontFamily: barButton.fontFamily
        bold: true
      }
    }
  }

  // ==================== HOVER SUMMARY CARD ====================
  UI.BarHoverCard {
    id: hoverCard
    anchorItem: root
    bar: root.bar
    owner: hoverOwner
    open: root.hoverOpen && !root.opened
    foreground: root.fg
    background: root.bg
    accent: root.accentColor
    urgent: root.urgentColor
    palette: root.palette
    fontFamily: root.fontFamily
    data: Model.buildHoverCardData(
      root.dashboard,
      root.service ? root.service.nextRefreshAt : "",
      root.service ? root.service.nowMs : Date.now(),
      root.t("bar.tooltipEmpty", "Omameter — Click to manage accounts"),
      {
        displayMode: root.daemonSettings.displayMode,
        resetDisplayMode: root.daemonSettings.resetDisplayMode,
        timeFormat: root.daemonSettings.timeFormat,
        privacyMode: root.privacyMode,
        palette: root.palette,
        foreground: root.fg,
        urgent: root.urgentColor,
        strings: strings
      }
    )

    onContainsMouseChanged: {
      if (containsMouse) {
        hoverCloseTimer.stop()
      } else if (root.hoverOpen && !barButton.tooltipHovered) {
        hoverCloseTimer.restart()
      }
    }
  }

  // ==================== KEYBOARD PANEL POPUP ====================
  KeyboardPanel {
    id: panel
    anchorItem: root
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher

    readonly property real activeBodyHeight: {
      if (root.viewMode === "accounts") return accountsView.implicitHeight
      if (root.viewMode === "settings") return settingsView.implicitHeight
      return root.dashboardTab === "all" ? allUsageTab.implicitHeight : priorityTab.implicitHeight
    }

    readonly property bool useStableHeight: !!(root.dashboard && root.dashboard.hasAccounts) || root.viewMode !== "dashboard"
    readonly property real minInnerHeight: useStableHeight ? Style.space(520) : Style.space(220)
    readonly property real desiredInnerHeight: Math.max(
      minInnerHeight,
      headerBar.height
        + (bannerBox.visible ? bannerBox.height + Style.space(8) : 0)
        + Style.space(10)
        + Math.max(Style.space(120), activeBodyHeight)
        + Style.space(10)
        + footerDivider.height
        + Style.space(8)
        + footerBar.height
    )

    contentWidth: panel.fittedContentWidth(Style.space(396))
    contentHeight: panel.fittedContentHeight(desiredInnerHeight, Style.space(580))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: allUsageTab.inputFocused || accountsView.inputFocused || settingsView.inputFocused

      onCloseRequested: root.goBackOrClose()
      onTabRequested: function(direction) {
        if (root.viewMode === "dashboard") {
          root.setDashboardTab(root.dashboardTab === "priority" ? "all" : "priority")
        } else {
          root.switchPanel(direction)
        }
      }
      onMoveRequested: function(dx, dy) {
        if (dx !== 0 && root.viewMode === "dashboard") {
          root.setDashboardTab(dx > 0 ? "all" : "priority")
        } else if (dy !== 0 && scrollArea.contentItem) {
          var step = Style.space(48)
          var maxY = Math.max(0, scrollArea.contentItem.contentHeight - scrollArea.height)
          scrollArea.contentItem.contentY = Model.clamp(scrollArea.contentItem.contentY + dy * step, 0, maxY)
        }
      }
      onTextKey: function(text, modifiers) {
        var ctrl = (modifiers & Qt.ControlModifier) !== 0
        var noMods = (modifiers & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)) === 0
        if (ctrl && (text === "r" || text === "R")) {
          root.refresh()
          return
        }
        if (ctrl && (text === "z" || text === "Z")) {
          if (root.service && root.service.canUndo) root.service.undoLayout()
          return
        }
        if (ctrl && text === ",") {
          root.openSettings()
          return
        }
        if (noMods) {
          if (text === "r" || text === "R") {
            root.refresh()
            return
          }
          if (text === "s" || text === "S") {
            root.openSettings()
            return
          }
          if (text === "a" || text === "A") {
            root.openAccounts(null)
            return
          }
          if (root.viewMode === "dashboard") {
            if (text === "1") root.setDashboardTab("priority")
            else if (text === "2") root.setDashboardTab("all")
          }
        }
      }

      // ---- TOP HEADER BAR ----
      Item {
        id: headerBar
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: Math.max(Style.space(30), Math.max(leftNav.implicitHeight, rightActions.implicitHeight))

        // Left: Dashboard Tabs OR Back + Subview Title
        Row {
          id: leftNav
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(6)

          // Dashboard tabs (Priority | All Usage)
          Row {
            visible: root.viewMode === "dashboard"
            spacing: Style.space(4)

            Button {
              text: root.t("tab.priority", "Priority")
              selected: root.dashboardTab === "priority"
              bordered: true
              foreground: root.fg
              accent: root.accentColor
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              horizontalPadding: Style.space(10)
              verticalPadding: Style.space(4)
              onClicked: root.setDashboardTab("priority")
            }

            Button {
              text: root.t("tab.all", "All Usage")
              selected: root.dashboardTab === "all"
              bordered: true
              foreground: root.fg
              accent: root.accentColor
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              horizontalPadding: Style.space(10)
              verticalPadding: Style.space(4)
              onClicked: root.setDashboardTab("all")
            }
          }

          // Subview Back button + Title
          Row {
            visible: root.viewMode !== "dashboard"
            spacing: Style.space(6)

            Button {
              anchors.verticalCenter: parent.verticalCenter
              iconText: "←"
              text: root.t("nav.back", "Back")
              bordered: true
              foreground: root.fg
              accent: root.accentColor
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              iconSize: Style.font.bodySmall
              horizontalPadding: Style.space(7)
              verticalPadding: Style.space(4)
              onClicked: root.goBackOrClose()
            }

            UI.PlainText {
              anchors.verticalCenter: parent.verticalCenter
              text: root.viewMode === "accounts"
                ? root.t("nav.accounts", "Accounts")
                : root.t("nav.settings", "Settings")
              color: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }
          }
        }

        // Right: Quick actions (Accounts, Refresh, Settings)
        Row {
          id: rightActions
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(3)

          PanelActionButton {
            iconText: "󰀄"
            tooltipText: Model.autoTextSafe(root.t("nav.accounts", "Accounts"), false)
            size: Style.space(26)
            fontSize: Style.font.body
            foreground: root.viewMode === "accounts" ? root.accentColor : root.fg
            hoverColor: root.accentColor
            fontFamily: root.fontFamily
            onClicked: root.dispatch({ type: "toggleAccounts" })
          }

          Button {
            iconText: "󰑐"
            tooltipText: Model.autoTextSafe(root.t("nav.refresh", "Refresh") + " (Ctrl+R)", false)
            iconSpinning: !!(root.service && root.service.refreshing)
            iconSize: Style.font.body
            horizontalPadding: Style.space(5)
            verticalPadding: Style.space(4)
            foreground: root.fg
            accent: root.accentColor
            fontFamily: root.fontFamily
            onClicked: root.refresh()
          }

          PanelActionButton {
            iconText: "󰒓"
            tooltipText: Model.autoTextSafe(root.t("nav.settings", "Settings") + " (Ctrl+,)", false)
            size: Style.space(26)
            fontSize: Style.font.body
            foreground: root.viewMode === "settings" ? root.accentColor : root.fg
            hoverColor: root.accentColor
            fontFamily: root.fontFamily
            onClicked: root.dispatch({ type: "toggleSettings" })
          }
        }
      }

      // ---- TOAST / DAEMON ERROR BANNER ----
      BorderSurface {
        id: bannerBox
        anchors.top: headerBar.bottom
        anchors.topMargin: visible ? Style.space(8) : 0
        anchors.left: parent.left
        anchors.right: parent.right
        readonly property string daemonMsg: !root.service
          ? root.t("app.serviceNotLoaded", "Omameter service is not loaded — re-enable the plugin.")
          : (root.service.daemonError || "")
        readonly property bool showingError: daemonMsg !== "" || root.activeToastError
        readonly property string displayText: daemonMsg !== "" ? daemonMsg : (root.toastVisible ? root.activeToastText : "")
        visible: displayText !== ""
        height: visible ? bannerText.implicitHeight + Style.space(12) : 0
        radius: Style.cornerRadius
        color: showingError
          ? Qt.rgba(root.urgentColor.r, root.urgentColor.g, root.urgentColor.b, 0.14)
          : Qt.rgba(root.accentColor.r, root.accentColor.g, root.accentColor.b, 0.12)
        borderSpec: Border.controlSpec("normal", showingError ? root.urgentColor : root.accentColor, showingError ? root.urgentColor : root.accentColor)

        UI.PlainText {
          id: bannerText
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          anchors.margins: Style.space(8)
          text: bannerBox.displayText
          color: bannerBox.showingError ? (root.palette.critical || root.urgentColor) : root.fg
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        MouseArea {
          anchors.fill: parent
          cursorShape: Qt.PointingHandCursor
          onClicked: root.toastVisible = false
        }
      }

      // ---- SCROLLABLE MAIN CONTENT ----
      ScrollView {
        id: scrollArea
        anchors.top: bannerBox.visible ? bannerBox.bottom : headerBar.bottom
        anchors.topMargin: Style.space(10)
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: footerDivider.top
        anchors.bottomMargin: Style.space(10)
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: panel.activeBodyHeight > height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff

        Item {
          width: scrollArea.availableWidth
          implicitHeight: panel.activeBodyHeight

          UI.PriorityTab {
            id: priorityTab
            visible: root.viewMode === "dashboard" && root.dashboardTab === "priority"
            width: parent.width
            service: root.service
            dashboard: root.dashboard
            settings: root.daemonSettings
            palette: root.palette
            strings: strings
            nowMs: root.service ? root.service.nowMs : Date.now()
            foreground: root.fg
            background: root.bg
            accent: root.accentColor
            urgent: root.urgentColor
            fontFamily: root.fontFamily
            onAddAccountRequested: root.openAddAccount()
            onViewAllUsageRequested: root.setDashboardTab("all")
            onFixAccountRequested: function(accId) { root.openAccounts(accId) }
          }

          UI.AllUsageTab {
            id: allUsageTab
            visible: root.viewMode === "dashboard" && root.dashboardTab === "all"
            width: parent.width
            service: root.service
            dashboard: root.dashboard
            settings: root.daemonSettings
            palette: root.palette
            strings: strings
            nowMs: root.service ? root.service.nowMs : Date.now()
            foreground: root.fg
            background: root.bg
            accent: root.accentColor
            urgent: root.urgentColor
            fontFamily: root.fontFamily
            onAddAccountRequested: root.openAddAccount()
            onFixAccountRequested: function(accId) { root.openAccounts(accId) }
          }

          UI.AccountsView {
            id: accountsView
            visible: root.viewMode === "accounts"
            width: parent.width
            service: root.service
            accounts: root.service ? root.service.accounts : []
            hubs: root.service ? root.service.hubs : []
            providerCatalog: root.service ? root.service.providerCatalog : []
            authSession: root.service ? root.service.authSession : null
            nav: root.nav
            onNavigationRequested: function(action) { root.dispatch(action) }
            settings: root.daemonSettings
            palette: root.palette
            strings: strings
            foreground: root.fg
            background: root.bg
            accent: root.accentColor
            urgent: root.urgentColor
            fontFamily: root.fontFamily
          }

          UI.SettingsView {
            id: settingsView
            visible: root.viewMode === "settings"
            width: parent.width
            service: root.service
            dashboard: root.dashboard
            settings: root.daemonSettings
            layout: root.service ? root.service.layout : ({})
            hiddenMetrics: root.dashboard && Array.isArray(root.dashboard.hiddenMetrics) ? root.dashboard.hiddenMetrics : []
            proxy: root.service ? root.service.proxy : ({ enabled: false, url: "" })
            palette: root.palette
            strings: strings
            foreground: root.fg
            background: root.bg
            accent: root.accentColor
            urgent: root.urgentColor
            fontFamily: root.fontFamily
          }
        }
      }

      // ---- FOOTER ----
      PanelSeparator {
        id: footerDivider
        anchors.bottom: footerBar.top
        anchors.bottomMargin: Style.space(8)
        anchors.left: parent.left
        anchors.right: parent.right
        foreground: root.fg
      }

      Item {
        id: footerBar
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        height: Math.max(Style.space(22), Math.max(footerLeft.implicitHeight, footerRight.implicitHeight))

        // Left: Omameter 0.8.0 · Next update in ... (clickable to force refresh).
        // Constrained before footerRight so Undo/Copy Summary never overlap it.
        Item {
          id: footerLeftBox
          anchors.left: parent.left
          anchors.right: footerRight.left
          anchors.rightMargin: Style.space(8)
          anchors.verticalCenter: parent.verticalCenter
          height: footerLeft.implicitHeight
          clip: true

          readonly property bool showVersionPrefix: nextUpdateText.text === ""
            || width >= versionText.implicitWidth + sepText.implicitWidth + nextUpdateText.implicitWidth + Style.space(12)

          Row {
            id: footerLeft
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(6)

            UI.PlainText {
              id: versionText
              visible: footerLeftBox.showVersionPrefix
              anchors.verticalCenter: parent.verticalCenter
              text: root.t("app.title", "Omameter") + " 0.8.0"
              color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.5)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            UI.PlainText {
              id: sepText
              visible: footerLeftBox.showVersionPrefix && nextUpdateText.text !== ""
              anchors.verticalCenter: parent.verticalCenter
              text: "·"
              color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.35)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Item {
              visible: nextUpdateText.text !== ""
              anchors.verticalCenter: parent.verticalCenter
              readonly property real maxAvail: Math.max(
                0,
                footerLeftBox.width - (footerLeftBox.showVersionPrefix ? (versionText.implicitWidth + sepText.implicitWidth + Style.space(12)) : 0)
              )
              width: Math.min(nextUpdateText.implicitWidth, maxAvail)
              height: nextUpdateText.implicitHeight

              UI.PlainText {
                id: nextUpdateText
                width: parent.width
                elide: Text.ElideRight
                text: {
                  if (root.service && root.service.refreshing) return root.t("footer.updating", "Updating…")
                  var nextStr = root.service ? Model.formatNextUpdate(root.service.nextRefreshAt, root.service.nowMs, strings) : ""
                  return nextStr !== "" ? root.t("footer.nextUpdate", "Next update in %1").arg(nextStr) : ""
                }
                color: nextUpdateHover.containsMouse
                  ? root.accentColor
                  : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.62)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              MouseArea {
                id: nextUpdateHover
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.refresh()
              }
            }
          }
        }

        // Right: Privacy Mode indicator + Undo + Copy Summary
        Row {
          id: footerRight
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(4)

          PanelActionButton {
            visible: root.privacyMode
            iconText: "󰈉"
            tooltipText: Model.autoTextSafe(root.t("settings.privacyMode", "Privacy Mode"), false)
            size: Style.space(22)
            fontSize: Style.font.caption
            foreground: root.accentColor
            hoverColor: root.fg
            fontFamily: root.fontFamily
            onClicked: {
              if (root.service) root.service.changeSettings({ privacyMode: false })
            }
          }

          Button {
            visible: !!(root.service && root.service.canUndo)
            text: root.t("nav.undo", "Undo")
            iconText: "󰕌"
            tooltipText: Model.autoTextSafe(root.t("nav.undo", "Undo") + " (Ctrl+Z)", false)
            bordered: true
            foreground: root.fg
            accent: root.accentColor
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            iconSize: Style.font.caption
            horizontalPadding: Style.space(6)
            verticalPadding: Style.space(2)
            onClicked: if (root.service) root.service.undoLayout()
          }

          Button {
            visible: root.viewMode === "dashboard" && !!(root.dashboard && root.dashboard.hasAccounts)
            text: root.copiedFeedback
              ? root.t("footer.copied", "Copied")
              : root.t("footer.copySummary", "Copy Summary")
            iconText: root.copiedFeedback ? "✓" : "󰆏"
            bordered: true
            selected: root.copiedFeedback
            foreground: root.fg
            accent: root.accentColor
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            iconSize: Style.font.caption
            horizontalPadding: Style.space(7)
            verticalPadding: Style.space(2)
            onClicked: root.copySummary()
          }
        }
      }
    }
  }
}
