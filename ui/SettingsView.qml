import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model
import "../Settings.js" as Settings

// 4-tab Settings screen (General, Display, Alerts, Advanced) controlling
// every Omameter setting, threshold, notification toggle, proxy, and CLI hint.
Column {
  id: root

  property var service: null
  property var dashboard: Model.emptyDashboard()
  property var settings: Settings.defaultSettings()
  property var layout: ({})
  property var hiddenMetrics: []
  property var proxy: ({ enabled: false, url: "" })
  property var palette: ({})
  property var strings: null
  property color foreground: Commons.Color.popups.text
  property color background: Commons.Color.popups.background
  property color accent: Commons.Color.accent
  property color urgent: Commons.Color.urgent
  property string fontFamily: Style.font.family

  property string activeTab: "general"
  property string proxyUrlDraft: ""
  property bool proxyEnabledDraft: false
  property bool proxyDirty: false
  readonly property bool inputFocused: proxyField.activeFocus

  function t(key, fb) { return Model.tr(strings, key, fb) }
  function tn(key, count, fb) { return strings ? strings.tn(key, count, fb) : (fb || key) }

  function patchSetting(patch) {
    if (service) service.changeSettings(patch)
  }

  function _copyStringList(listLike, fallbackId) {
    var out = []
    if (listLike && listLike.length > 0) {
      for (var i = 0; i < listLike.length; i++) {
        var s = String(listLike[i] || "")
        if (s) out.push(s)
      }
    }
    if (out.length === 0 && fallbackId) out.push(String(fallbackId))
    return out
  }

  readonly property var barSelectableMetrics: {
    var out = []
    var cards = (dashboard && dashboard.allUsage && dashboard.allUsage.length > 0) ? dashboard.allUsage : []
    var pinList = (layout && layout.priorityPinIDs && layout.priorityPinIDs.length > 0) ? layout.priorityPinIDs : []
    var pinRank = {}
    for (var p = 0; p < pinList.length; p++) {
      var pid = String(pinList[p] || "")
      if (pid && pinRank[pid] === undefined) pinRank[pid] = p
    }
    var pinnedRefs = []
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i] || {}
      var accs = (card.accounts && card.accounts.length > 0) ? card.accounts : []
      for (var j = 0; j < accs.length; j++) {
        var acc = accs[j] || {}
        var lines = (acc.lines && acc.lines.length > 0) ? acc.lines : []
        for (var k = 0; k < lines.length; k++) {
          var ln = lines[k]
          if (!ln || !ln.metricId) continue
          var isPinned = !!ln.isPinned
          var isQuota = !!ln.isBounded || String(ln.windowKind || "") === "extra"
          if (!isQuota && !isPinned) continue
          var cids = root._copyStringList(ln.copyIds, ln.metricId)
          var rank = -1
          if (isPinned) {
            for (var c = 0; c < cids.length; c++) {
              if (pinRank[cids[c]] !== undefined) {
                rank = (rank < 0) ? pinRank[cids[c]] : Math.min(rank, pinRank[cids[c]])
              }
            }
            if (rank < 0) rank = pinList.length + out.length
          }
          var entry = {
            metricId: String(ln.metricId),
            copyIds: cids,
            icon: String(ln.icon || card.icon || "omameter.svg"),
            providerName: String(ln.providerName || card.displayName || card.kind || ""),
            accountName: String(ln.accountName || acc.accountName || ""),
            title: String(ln.title || ""),
            shortTitle: String(ln.shortTitle || ln.title || ""),
            windowKind: String(ln.windowKind || ""),
            windowTag: String(ln.windowTag || ""),
            titleNamesWindow: ln.titleNamesWindow === true,
            badge: String(ln.badge || ""),
            isHub: !!ln.isHub,
            isUnified: !!ln.isUnified,
            isPinned: isPinned,
            pinRank: rank,
            pinSlot: 0
          }
          out.push(entry)
          if (isPinned) pinnedRefs.push(entry)
        }
      }
    }
    pinnedRefs.sort(function(a, b) { return a.pinRank - b.pinRank })
    for (var sIdx = 0; sIdx < pinnedRefs.length; sIdx++) {
      pinnedRefs[sIdx].pinSlot = sIdx + 1
    }
    return out
  }

  readonly property int pinnedMetricsCount: {
    var n = 0
    for (var i = 0; i < barSelectableMetrics.length; i++) {
      if (barSelectableMetrics[i].isPinned) n += 1
    }
    return n
  }

  function clearAllPinned() {
    if (!service) return
    var ids = []
    for (var i = 0; i < barSelectableMetrics.length; i++) {
      var m = barSelectableMetrics[i]
      if (!m.isPinned) continue
      var cids = root._copyStringList(m.copyIds, m.metricId)
      for (var j = 0; j < cids.length; j++) ids.push(cids[j])
    }
    if (ids.length > 0) service.setMetricPinned(ids, false)
  }

  readonly property var unifiableProviders: Model.safeProviderCatalog(service ? service.providerCatalog : null)
    .filter(function(p) { return p && p.unifiable === true && !!p.id })

  function isKindUnified(kind) {
    var list = (layout && layout.unifiedProviderKinds) ? layout.unifiedProviderKinds : []
    return list.indexOf(kind) >= 0
  }

  function syncProxyFromService() {
    if (proxyDirty) return
    proxyEnabledDraft = !!(proxy && proxy.enabled)
    proxyUrlDraft = proxy && proxy.url ? String(proxy.url) : ""
  }

  onProxyChanged: syncProxyFromService()
  Component.onCompleted: syncProxyFromService()

  readonly property var s: Settings.normalize(settings)
  readonly property int warningThreshold: s.warningThreshold
  readonly property int criticalThreshold: s.criticalThreshold
  readonly property int maxBarItems: s.maxBarItems
  readonly property int maxBarItemsLimit: Settings.maxBarItemsCap(s.barLabelStyle)
  readonly property var maxBarItemsOptions: maxBarItemsLimit >= 8
    ? [0, 1, 2, 3, 4, 5, 6, 7, 8]
    : [0, 1, 2, 3, 4, 5]
  readonly property int refreshIntervalSec: s.refreshIntervalSec

  spacing: Style.space(10)

  // 4-tab bar
  Row {
    width: parent.width
    spacing: Style.space(4)

    Repeater {
      model: [
        { id: "general",  key: "settings.general",  fb: "General" },
        { id: "display",  key: "settings.display",  fb: "Display" },
        { id: "alerts",   key: "settings.alerts",   fb: "Alerts" },
        { id: "advanced", key: "settings.advanced", fb: "Advanced" }
      ]

      delegate: Button {
        required property var modelData
        width: Math.floor((parent.width - Style.space(12)) / 4)
        text: root.t(modelData.key, modelData.fb)
        selected: root.activeTab === modelData.id
        bordered: true
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        horizontalPadding: Style.space(4)
        verticalPadding: Style.space(5)
        onClicked: root.activeTab = modelData.id
      }
    }
  }

  // ==================== GENERAL TAB ====================
  Column {
    visible: root.activeTab === "general"
    width: parent.width
    spacing: Style.space(10)

    // Default Tab
    SettingChoiceRow {
      width: parent.width
      title: root.t("settings.defaultTab", "Default Tab")
      options: [
        { value: "priority", label: root.t("tab.priority", "Priority") },
        { value: "all",      label: root.t("tab.all", "All Usage") }
      ]
      value: root.s.defaultTab
      foreground: root.foreground
      background: root.background
      accent: root.accent
      fontFamily: root.fontFamily
      onChosen: function(v) { root.patchSetting({ defaultTab: v }) }
    }

    PanelSeparator { foreground: root.foreground }

    // Max Items in Bar (0..5 in Full style, 0..8 in Compact / Icons styles)
    Column {
      width: parent.width
      spacing: Style.space(5)

      Item {
        width: parent.width
        height: maxLbl.implicitHeight

        PanelSectionHeader {
          id: maxLbl
          anchors.left: parent.left
          text: root.t("settings.maxBarItems", "Max Items in Bar")
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        PlainText {
          anchors.right: parent.right
          anchors.verticalCenter: maxLbl.verticalCenter
          text: root.maxBarItems === 0
            ? root.t("settings.iconOnly", "Icon Only")
            : root.t("settings.upTo", "Up to %1").arg(root.maxBarItems)
          color: root.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }
      }

      Row {
        width: parent.width
        spacing: Style.space(4)

        Repeater {
          model: root.maxBarItemsOptions
          delegate: Button {
            required property int modelData
            readonly property int optCount: root.maxBarItemsOptions.length
            width: Math.floor((parent.width - Style.space(4) * Math.max(0, optCount - 1)) / Math.max(1, optCount))
            text: String(modelData)
            tooltipText: Model.autoTextSafe(modelData === 0 ? root.t("settings.iconOnly", "Icon Only") : root.t("settings.upTo", "Up to %1").arg(modelData), false)
            selected: root.maxBarItems === modelData
            bordered: true
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            horizontalPadding: optCount > 6 ? Style.space(2) : Style.space(4)
            verticalPadding: Style.space(4)
            onClicked: root.patchSetting({ maxBarItems: modelData })
          }
        }
      }
    }

    // Bar Label Style (Full / Compact / Icons)
    SettingChoiceRow {
      width: parent.width
      title: root.t("settings.barLabelStyle", "Top Bar Style")
      options: [
        { value: "full",    label: root.t("settings.barLabelFull", "Full") },
        { value: "compact", label: root.t("settings.barLabelCompact", "Compact") },
        { value: "icon",    label: root.t("settings.barLabelIcon", "Icons") }
      ]
      value: root.s.barLabelStyle
      foreground: root.foreground
      background: root.background
      accent: root.accent
      fontFamily: root.fontFamily
      onChosen: function(v) { root.patchSetting({ barLabelStyle: v }) }
    }

    // Always Show in Bar
    SettingToggleRow {
      width: parent.width
      label: root.t("settings.alwaysShowInBar", "Always Show in Top Bar")
      description: root.t("settings.alwaysShowInBarHint", "When on, auto-fills extra slots with burning-fast, expiring, and spent limits. When off, only your selected Top Bar Items appear.")
      checked: !!root.s.alwaysShowInBar
      foreground: root.foreground
      accent: root.accent
      fontFamily: root.fontFamily
      onToggled: root.patchSetting({ alwaysShowInBar: !root.s.alwaysShowInBar })
    }

    // Top Bar Items: direct pin picker so users can choose which limits appear in the top bar
    Column {
      visible: root.barSelectableMetrics.length > 0
      width: parent.width
      spacing: Style.space(5)

      Item {
        width: parent.width
        height: Math.max(barItemsLbl.implicitHeight, barItemsMeta.implicitHeight)

        PanelSectionHeader {
          id: barItemsLbl
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          text: root.t("settings.barMetrics", "Top Bar Items")
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        Row {
          id: barItemsMeta
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(6)

          PlainText {
            anchors.verticalCenter: parent.verticalCenter
            text: root.pinnedMetricsCount > 0
              ? (root.maxBarItems === 0 || root.pinnedMetricsCount > root.maxBarItems
                ? root.t("settings.barMetricsPinnedOverflow", "%1 pinned · showing %2").arg(root.pinnedMetricsCount).arg(root.maxBarItems)
                : root.tn("settings.barMetricsPinnedCount", root.pinnedMetricsCount, "%1 pinned").arg(root.pinnedMetricsCount))
              : (root.s.alwaysShowInBar
                ? root.t("settings.barMetricsAuto", "Auto (urgent first)")
                : root.t("settings.barMetricsAlertsOnly", "None (alerts only)"))
            color: root.pinnedMetricsCount > 0
              ? root.accent
              : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.58)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: root.pinnedMetricsCount > 0
          }

          Button {
            visible: root.pinnedMetricsCount > 0
            anchors.verticalCenter: parent.verticalCenter
            text: root.t("settings.barMetricsClear", "Clear")
            bordered: true
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            horizontalPadding: Style.space(6)
            verticalPadding: Style.space(2)
            onClicked: root.clearAllPinned()
          }
        }
      }

      PlainText {
        width: parent.width
        text: root.s.alwaysShowInBar
          ? root.t("settings.barMetricsHint", "Pick metrics to pin first in selection order. Urgent limits fill remaining slots.")
          : root.t("settings.barMetricsHintStrict", "Pick the exact metrics to show in the top bar (in selection order). Leave empty for icon-only until a limit runs low.")
        color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.58)
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      Flow {
        width: parent.width
        spacing: Style.space(5)

        Repeater {
          model: root.barSelectableMetrics

          delegate: BorderSurface {
            id: barChip
            required property var modelData
            readonly property bool isPinned: !!modelData.isPinned
            readonly property int pinSlot: Number(modelData.pinSlot || 0)
            readonly property bool isOverflow: isPinned && (root.maxBarItems === 0 || pinSlot > root.maxBarItems)
            readonly property color chipFg: isPinned
              ? (isOverflow
                ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.72)
                : root.accent)
              : root.foreground
            readonly property string chipLabel: {
              var base = Model.metricPickerLabel(modelData)
              return modelData.badge ? (base + " · " + Model.cleanText(modelData.badge, 12)) : base
            }
            readonly property string chipTooltip: {
              var hdr = Model.accountHeader(modelData.providerName, modelData.accountName, {
                privacyMode: !!root.s.privacyMode,
                isAggregate: !!modelData.isUnified
              })
              var base = hdr + " — " + Model.cleanText(modelData.title, 60)
              if (isOverflow) {
                base += "\n" + root.t("settings.barMetricsOverflowTip", "In overflow (slot #%1) — raise Max Items in Bar or scroll the bar").arg(pinSlot)
              }
              return Model.autoTextSafe(base, isOverflow)
            }

            width: chipRow.implicitWidth + Style.space(14)
            height: chipRow.implicitHeight + Style.space(8)
            radius: Style.cornerRadius
            color: isPinned
              ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, isOverflow ? (chipHover.hovered ? 0.16 : 0.09) : (chipHover.hovered ? 0.24 : 0.16))
              : (chipHover.hovered
                ? Style.hoverFillFor(root.foreground, root.accent)
                : Style.normalFillFor(root.foreground, root.accent))
            borderSpec: Border.controlSpec(
              (isPinned || chipHover.hovered) ? "hover-cursor" : "normal",
              isPinned ? (isOverflow ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.6) : root.accent) : root.foreground,
              root.accent
            )

            Behavior on color { ColorAnimation { duration: Style.duration(80) } }

            HoverHandler { id: chipHover }

            Row {
              id: chipRow
              anchors.centerIn: parent
              spacing: Style.space(5)

              BrandMark {
                anchors.verticalCenter: parent.verticalCenter
                icon: barChip.modelData.icon
                iconSize: Style.space(12)
                tintColor: barChip.chipFg
                badgeBackground: root.background
                fontFamily: root.fontFamily
                isHub: !!barChip.modelData.isHub
              }

              PlainText {
                anchors.verticalCenter: parent.verticalCenter
                text: barChip.chipLabel
                color: barChip.chipFg
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: barChip.isPinned && !barChip.isOverflow
              }

              PlainText {
                visible: barChip.isPinned
                anchors.verticalCenter: parent.verticalCenter
                text: barChip.pinSlot > 0 ? (barChip.isOverflow ? ("+" + barChip.pinSlot) : String(barChip.pinSlot)) : "󰐄"
                color: barChip.chipFg
                font.family: root.fontFamily
                font.pixelSize: Math.max(9, Style.font.caption - 1)
                font.bold: true
              }
            }

            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: {
                if (!root.service) return
                var ids = root._copyStringList(barChip.modelData.copyIds, barChip.modelData.metricId)
                root.service.setMetricPinned(ids, !barChip.isPinned)
              }
            }

            PanelToolTip {
              visible: chipHover.hovered && barChip.chipTooltip !== ""
              text: barChip.chipTooltip
              fontFamily: root.fontFamily
            }
          }
        }
      }
    }

    // Privacy Mode
    SettingToggleRow {
      width: parent.width
      label: root.t("settings.privacyMode", "Privacy Mode")
      checked: !!root.s.privacyMode
      foreground: root.foreground
      accent: root.accent
      fontFamily: root.fontFamily
      onToggled: root.patchSetting({ privacyMode: !root.s.privacyMode })
    }

    PanelSeparator { foreground: root.foreground }

    // Refresh Interval
    Column {
      width: parent.width
      spacing: Style.space(5)

      PanelSectionHeader {
        text: root.t("settings.refreshInterval", "Refresh Interval")
        foreground: root.foreground
        fontFamily: root.fontFamily
      }

      Row {
        width: parent.width
        spacing: Style.space(4)

        Repeater {
          model: [
            { sec: 60,   label: "1m" },
            { sec: 120,  label: "2m" },
            { sec: 300,  label: "5m" },
            { sec: 600,  label: "10m" },
            { sec: 900,  label: "15m" },
            { sec: 1800, label: "30m" }
          ]
          delegate: Button {
            required property var modelData
            width: Math.floor((parent.width - Style.space(20)) / 6)
            text: modelData.label
            selected: root.refreshIntervalSec === modelData.sec
            bordered: true
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            horizontalPadding: Style.space(4)
            verticalPadding: Style.space(4)
            onClicked: root.patchSetting({ refreshIntervalSec: modelData.sec })
          }
        }
      }
    }
  }

  // ==================== DISPLAY TAB ====================
  Column {
    visible: root.activeTab === "display"
    width: parent.width
    spacing: Style.space(10)

    // Show Usage As
    SettingChoiceRow {
      width: parent.width
      title: root.t("settings.showUsageAs", "Show Usage As")
      options: [
        { value: "remaining", label: root.t("settings.remaining", "Remaining (left)") },
        { value: "used",      label: root.t("settings.used", "Used") }
      ]
      value: root.s.displayMode
      foreground: root.foreground
      background: root.background
      accent: root.accent
      fontFamily: root.fontFamily
      onChosen: function(v) { root.patchSetting({ displayMode: v }) }
    }

    // Reset Times
    SettingChoiceRow {
      width: parent.width
      title: root.t("settings.resetTimes", "Reset Times")
      options: [
        { value: "relative", label: root.t("settings.relative", "Countdown") },
        { value: "absolute", label: root.t("settings.absolute", "Exact Time") }
      ]
      value: root.s.resetDisplayMode
      foreground: root.foreground
      background: root.background
      accent: root.accent
      fontFamily: root.fontFamily
      onChosen: function(v) { root.patchSetting({ resetDisplayMode: v }) }
    }

    // Pace Marks
    SettingToggleRow {
      width: parent.width
      label: root.t("settings.paceMarks", "Pace Marks")
      wrapLabel: false
      checked: !!root.s.showPaceMarks
      foreground: root.foreground
      accent: root.accent
      fontFamily: root.fontFamily
      onToggled: root.patchSetting({ showPaceMarks: !root.s.showPaceMarks })
    }

    // Time Format
    SettingChoiceRow {
      width: parent.width
      title: root.t("settings.timeFormat", "Time Format")
      options: [
        { value: "auto", label: "Auto" },
        { value: "12h",  label: "12h" },
        { value: "24h",  label: "24h" }
      ]
      value: root.s.timeFormat
      foreground: root.foreground
      background: root.background
      accent: root.accent
      fontFamily: root.fontFamily
      onChosen: function(v) { root.patchSetting({ timeFormat: v }) }
    }

    PanelSeparator { foreground: root.foreground }

    // Unify Accounts: providers the catalog marks unifiable
    Column {
      width: parent.width
      spacing: Style.space(6)

      PanelSectionHeader {
        text: root.t("settings.unifyAccounts", "Unify Accounts")
        foreground: root.foreground
        fontFamily: root.fontFamily
      }

      Repeater {
        model: root.unifiableProviders

        delegate: SettingToggleRow {
          required property var modelData
          width: parent.width
          showIcon: true
          icon: String(modelData.icon || "")
          label: Model.cleanText(modelData.displayName || modelData.id, 40)
          wrapLabel: false
          checked: root.isKindUnified(modelData.id)
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          onToggled: if (root.service) root.service.setProviderUnified(modelData.id, !root.isKindUnified(modelData.id))
        }
      }
    }

    PanelSeparator {
      visible: Array.isArray(root.hiddenMetrics) && root.hiddenMetrics.length > 0
      foreground: root.foreground
    }

    // Hidden Metrics restore list
    Column {
      visible: Array.isArray(root.hiddenMetrics) && root.hiddenMetrics.length > 0
      width: parent.width
      spacing: Style.space(6)

      Item {
        width: parent.width
        height: Math.max(hiddenHead.implicitHeight, restoreAllBtn.implicitHeight)

        PanelSectionHeader {
          id: hiddenHead
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          text: root.t("settings.hiddenMetrics", "Hidden Metrics")
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        Button {
          id: restoreAllBtn
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          text: root.t("settings.restoreAll", "Restore All")
          bordered: true
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          horizontalPadding: Style.space(8)
          verticalPadding: Style.space(3)
          onClicked: {
            if (!root.service || !Array.isArray(root.hiddenMetrics) || root.hiddenMetrics.length === 0) return
            var allIds = root.hiddenMetrics
              .map(function(m) { return m && m.metricId ? String(m.metricId) : "" })
              .filter(function(id) { return id !== "" })
            if (allIds.length > 0) root.service.setMetricHidden(allIds, false)
          }
        }
      }

      Repeater {
        model: Array.isArray(root.hiddenMetrics) ? root.hiddenMetrics : []
        delegate: BorderSurface {
          required property var modelData
          width: parent.width
          radius: Style.cornerRadius
          color: Style.normalFillFor(root.foreground, root.accent)
          borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
          implicitHeight: hmRow.implicitHeight + Style.space(10)

          Item {
            id: hmRow
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: Style.space(8)
            anchors.rightMargin: Style.space(8)
            implicitHeight: Math.max(hmLabel.implicitHeight, hmShowBtn.implicitHeight)
            height: implicitHeight

            PlainText {
              id: hmLabel
              anchors.left: parent.left
              anchors.right: hmShowBtn.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              text: (modelData && modelData.providerName ? String(modelData.providerName) + " · " : "")
                + String(modelData && (modelData.title || modelData.metricId) ? (modelData.title || modelData.metricId) : "")
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              elide: Text.ElideRight
            }

            Button {
              id: hmShowBtn
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: root.t("card.show", "Show")
              bordered: true
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              horizontalPadding: Style.space(8)
              verticalPadding: Style.space(2)
              onClicked: {
                if (root.service && modelData && modelData.metricId) {
                  root.service.setMetricHidden([String(modelData.metricId)], false)
                }
              }
            }
          }
        }
      }
    }
  }

  // ==================== ALERTS TAB ====================
  Column {
    visible: root.activeTab === "alerts"
    width: parent.width
    spacing: Style.space(10)

    // Threshold steppers (±5 stepped with linked push in backend)
    Column {
      width: parent.width
      spacing: Style.space(6)

      PanelSectionHeader {
        text: root.t("settings.thresholds", "Thresholds (% left)")
        foreground: root.foreground
        fontFamily: root.fontFamily
      }

      ThresholdStepper {
        width: parent.width
        settingKey: "warningThreshold"
        label: root.t("settings.warning", "Warning")
        value: root.warningThreshold
        tone: root.palette.warning || "#e59b3c"
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        onStepped: function(v) { root.patchSetting({ warningThreshold: v }) }
      }

      ThresholdStepper {
        width: parent.width
        settingKey: "criticalThreshold"
        label: root.t("settings.critical", "Critical")
        value: root.criticalThreshold
        tone: root.palette.critical || root.urgent
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        onStepped: function(v) { root.patchSetting({ criticalThreshold: v }) }
      }
    }

    PanelSeparator { foreground: root.foreground }

    // Priority & Notify matrix
    Column {
      width: parent.width
      spacing: Style.space(6)

      // Column header
      Item {
        width: parent.width
        height: colHeadPrio.implicitHeight

        PlainText {
          id: colHeadPrio
          anchors.right: colHeadNotify.left
          anchors.rightMargin: Style.space(12)
          width: Style.space(58)
          horizontalAlignment: Text.AlignHCenter
          text: root.t("settings.inPriority", "Priority")
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.6)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }

        PlainText {
          id: colHeadNotify
          anchors.right: parent.right
          width: Style.space(58)
          horizontalAlignment: Text.AlignHCenter
          text: root.t("settings.notify", "Notify")
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.6)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }
      }

      AlertMatrixRow {
        width: parent.width
        label: root.t("settings.warning", "Warning")
        priorityFixed: true
        notifyChecked: !!root.s.notifyWarning
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        onNotifyToggled: root.patchSetting({ notifyWarning: !root.s.notifyWarning })
      }

      AlertMatrixRow {
        width: parent.width
        label: root.t("settings.critical", "Critical")
        priorityFixed: true
        notifyChecked: !!root.s.notifyCritical
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        onNotifyToggled: root.patchSetting({ notifyCritical: !root.s.notifyCritical })
      }

      AlertMatrixRow {
        width: parent.width
        label: root.t("settings.burningFast", "Burning Fast")
        priorityChecked: root.s.burningFastPriority
        notifyChecked: !!root.s.notifyBurningFast
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        onPriorityToggled: root.patchSetting({ burningFastPriority: !root.s.burningFastPriority })
        onNotifyToggled: root.patchSetting({ notifyBurningFast: !root.s.notifyBurningFast })
      }

      AlertMatrixRow {
        width: parent.width
        label: root.t("settings.useItOrLoseIt", "Use It or Lose It")
        priorityChecked: root.s.useItOrLoseItPriority
        notifyChecked: !!root.s.notifyUseItOrLoseIt
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        onPriorityToggled: root.patchSetting({ useItOrLoseItPriority: !root.s.useItOrLoseItPriority })
        onNotifyToggled: root.patchSetting({ notifyUseItOrLoseIt: !root.s.notifyUseItOrLoseIt })
      }
    }
  }

  // ==================== ADVANCED TAB ====================
  Column {
    visible: root.activeTab === "advanced"
    width: parent.width
    spacing: Style.space(10)

    // Proxy configuration
    Column {
      width: parent.width
      spacing: Style.space(6)

      PanelSectionHeader {
        text: root.t("settings.proxy", "Proxy")
        foreground: root.foreground
        fontFamily: root.fontFamily
      }

      SettingToggleRow {
        width: parent.width
        label: root.t("settings.proxyEnabled", "Enable Proxy")
        wrapLabel: false
        checked: root.proxyEnabledDraft
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        onToggled: {
          root.proxyEnabledDraft = !root.proxyEnabledDraft
          root.proxyDirty = true
          if (!root.proxyEnabledDraft && root.service) {
            root.service.updateProxy(false, root.proxyUrlDraft)
            root.proxyDirty = false
          }
        }
      }

      TextField {
        id: proxyField
        width: parent.width
        placeholderText: root.t("settings.proxyUrl", "Proxy URL (socks5://, http://, https://)")
        text: root.proxyUrlDraft
        foreground: root.foreground
        accent: root.accent
        onTextEdited: {
          root.proxyUrlDraft = text
          root.proxyDirty = true
        }
        onAccepted: {
          if (root.service) {
            root.service.updateProxy(root.proxyEnabledDraft, root.proxyUrlDraft)
            root.proxyDirty = false
          }
        }
      }

      Row {
        anchors.right: parent.right
        spacing: Style.space(6)

        Button {
          text: root.t("accounts.save", "Save")
          bordered: true
          selected: root.proxyDirty
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          onClicked: {
            if (root.service) {
              root.service.updateProxy(root.proxyEnabledDraft, root.proxyUrlDraft)
              root.proxyDirty = false
            }
          }
        }
      }
    }

    PanelSeparator { foreground: root.foreground }

    // CLI hint box
    BorderSurface {
      width: parent.width
      radius: Style.cornerRadius
      color: Style.normalFillFor(root.foreground, root.accent)
      borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
      implicitHeight: cliRow.implicitHeight + Style.space(16)

      Row {
        id: cliRow
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.margins: Style.space(8)
        spacing: Style.space(8)

        PlainText {
          anchors.verticalCenter: parent.verticalCenter
          width: Math.max(80, cliRow.width - copyCliBtn.implicitWidth - Style.space(8))
          text: root.t("settings.cliHint", "CLI: run `omameter` for JSON (omameter.limits.v2)")
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.78)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        PanelActionButton {
          id: copyCliBtn
          anchors.verticalCenter: parent.verticalCenter
          iconText: "󰆏"
          tooltipText: Model.autoTextSafe("omameter", false)
          size: Style.space(22)
          fontSize: Style.font.bodySmall
          foreground: root.foreground
          hoverColor: root.accent
          fontFamily: root.fontFamily
          onClicked: if (root.service) root.service.copyText("omameter")
        }
      }
    }
  }
}
