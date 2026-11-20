import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model
import "../Settings.js" as Settings

// Priority tab: surfaces Pinned, Running Out (including Burning Fast),
// Use It or Lose It, All Clear, Needs Attention, and Used Up.
Column {
  id: root

  property var service: null
  property var dashboard: Model.emptyDashboard()
  property var settings: Settings.defaultSettings()
  property var palette: ({})
  property var strings: null
  property real nowMs: Date.now()
  property color foreground: Commons.Color.popups.text
  property color background: Commons.Color.popups.background
  property color accent: Commons.Color.accent
  property color urgent: Commons.Color.urgent
  property string fontFamily: Style.font.family

  signal addAccountRequested()
  signal viewAllUsageRequested()
  signal fixAccountRequested(string accountId)

  function t(key, fb) { return Model.tr(strings, key, fb) }
  function togglePin(item) {
    if (!service || !item) return
    service.setMetricPinned(item.copyIds || [item.metricId], !item.isPinned)
  }

  readonly property var prio: (dashboard && dashboard.priority) ? dashboard.priority : Model.emptyDashboard().priority
  readonly property bool hasAccounts: !!(dashboard && dashboard.hasAccounts)
  readonly property bool privacyMode: !!(settings && settings.privacyMode)
  readonly property var pinnedList: prio.pinned || []
  readonly property var runningOutList: prio.runningOut || []
  readonly property var useItList: prio.useItOrLoseIt || []
  readonly property var noticesList: prio.notices || []
  readonly property var usedUpList: prio.usedUp || []
  readonly property bool showsClearRow: !!prio.showsClearRow
  readonly property string summaryState: String(prio.summary || "waitingForUsage")
  readonly property var lineOpts: ({
    resetDisplayMode: settings && settings.resetDisplayMode ? String(settings.resetDisplayMode) : "relative",
    timeFormat: settings && settings.timeFormat ? String(settings.timeFormat) : "auto",
    nowMs: nowMs,
    privacyMode: privacyMode,
    palette: palette,
    foreground: foreground,
    urgent: urgent,
    strings: strings
  })

  spacing: Style.space(10)

  // Empty state: No accounts yet
  EmptyState {
    visible: !root.hasAccounts
    width: parent.width
    title: root.t("empty.title", "No accounts yet")
    wrapTitle: true
    subtitle: root.t("empty.subtitle", "Add your AI coding subscriptions to track limits right from the top bar.")
    actionText: root.t("empty.addAccount", "Add Account")
    foreground: root.foreground
    accent: root.accent
    fontFamily: root.fontFamily
    onActionClicked: root.addAccountRequested()
  }

  // Waiting for first usage snapshot
  BorderSurface {
    visible: root.hasAccounts && root.summaryState === "waitingForUsage"
      && root.pinnedList.length === 0 && root.runningOutList.length === 0
      && root.noticesList.length === 0 && root.usedUpList.length === 0
    width: parent.width
    radius: Style.cornerRadius
    color: Style.normalFillFor(root.foreground, root.accent)
    borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
    implicitHeight: waitingRow.implicitHeight + Style.space(20)

    Row {
      id: waitingRow
      anchors.centerIn: parent
      spacing: Style.space(8)

      PlainText {
        anchors.verticalCenter: parent.verticalCenter
        text: "󰑐"
        color: root.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.title

        RotationAnimation on rotation {
          from: 0
          to: 360
          duration: Style.duration(1100)
          loops: Animation.Infinite
          running: visible && !Style.reduceMotion
        }
      }

      PlainText {
        anchors.verticalCenter: parent.verticalCenter
        text: root.t("priority.waiting", "Checking usage…")
        color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.8)
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }
    }
  }

  // A titled list of PriorityRow. Inline components do not see this file's ids,
  // so the tab passes itself as `tab` and the theme/wiring live here once.
  component PrioritySection: Column {
    id: section
    property var tab: null
    property string title: ""
    property var rows: []
    property bool dimmedRows: false

    width: parent.width
    spacing: Style.space(5)

    PanelSectionHeader {
      text: section.title
      foreground: section.tab.foreground
      fontFamily: section.tab.fontFamily
    }

    Repeater {
      model: section.rows
      delegate: PriorityRow {
        required property var modelData
        width: parent.width
        item: modelData
        dimmedRow: section.dimmedRows
        lineOpts: section.tab.lineOpts
        showPin: !!section.tab.service
        palette: section.tab.palette
        strings: section.tab.strings
        foreground: section.tab.foreground
        background: section.tab.background
        accent: section.tab.accent
        fontFamily: section.tab.fontFamily
        onOpenAllUsage: section.tab.viewAllUsageRequested()
        onToggleDisplayMode: section.tab.service && section.tab.service.toggleDisplayMode()
        onToggleResetMode: section.tab.service && section.tab.service.toggleResetMode()
        onTogglePin: section.tab.togglePin(modelData)
      }
    }
  }

  // 1. Pinned section
  PrioritySection {
    tab: root
    visible: root.hasAccounts && root.pinnedList.length > 0
    title: root.t("priority.pinned", "Pinned")
    rows: root.pinnedList
    dimmedRows: false
  }

  // 2. Running Out section (Low + Burning Fast)
  PrioritySection {
    tab: root
    visible: root.hasAccounts && root.runningOutList.length > 0
    title: root.t("priority.runningOut", "Running Out")
    rows: root.runningOutList
    dimmedRows: false
  }

  // 3. Use It or Lose It section
  PrioritySection {
    tab: root
    visible: root.hasAccounts && root.useItList.length > 0
    title: root.t("priority.useItOrLoseIt", "Use It or Lose It")
    rows: root.useItList
    dimmedRows: false
  }

  // 4. All Clear calm card
  BorderSurface {
    visible: root.hasAccounts && root.showsClearRow
    width: parent.width
    radius: Style.cornerRadius
    color: Style.normalFillFor(root.foreground, root.accent)
    borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
    implicitHeight: clearRow.implicitHeight + Style.space(20)

    Row {
      id: clearRow
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.margins: Style.space(12)
      spacing: Style.space(10)

      PlainText {
        anchors.verticalCenter: parent.verticalCenter
        text: "󰄬"
        color: root.palette.good || root.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.heading
      }

      Column {
        anchors.verticalCenter: parent.verticalCenter
        width: Math.max(80, clearRow.width - Style.space(24) - viewAllBtn.implicitWidth - Style.space(20))
        spacing: Style.space(2)

        PlainText {
          width: parent.width
          text: root.t("priority.allClear", "All Clear")
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
        }

        PlainText {
          width: parent.width
          text: root.t("priority.allClearDesc", "Every limit has plenty of headroom.")
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.68)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }
      }

      Button {
        id: viewAllBtn
        anchors.verticalCenter: parent.verticalCenter
        text: root.t("priority.viewAllUsage", "View All Usage")
        bordered: true
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        horizontalPadding: Style.space(8)
        verticalPadding: Style.space(4)
        onClicked: root.viewAllUsageRequested()
      }
    }
  }

  // 5. No Reported Limits
  BorderSurface {
    visible: root.hasAccounts && root.summaryState === "noReportedLimits" && !root.showsClearRow
    width: parent.width
    radius: Style.cornerRadius
    color: Style.normalFillFor(root.foreground, root.accent)
    borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
    implicitHeight: noLimRow.implicitHeight + Style.space(18)

    Row {
      id: noLimRow
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.margins: Style.space(10)
      spacing: Style.space(8)

      PlainText {
        anchors.verticalCenter: parent.verticalCenter
        width: Math.max(60, noLimRow.width - viewAllBtn2.implicitWidth - Style.space(8))
        text: root.t("priority.noReportedLimits", "No reported limits")
        color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.72)
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
      }

      Button {
        id: viewAllBtn2
        anchors.verticalCenter: parent.verticalCenter
        text: root.t("priority.viewAllUsage", "View All Usage")
        bordered: true
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        horizontalPadding: Style.space(8)
        verticalPadding: Style.space(4)
        onClicked: root.viewAllUsageRequested()
      }
    }
  }

  // 6. Needs Attention section (errors & notices)
  Column {
    visible: root.hasAccounts && root.noticesList.length > 0
    width: parent.width
    spacing: Style.space(5)

    PanelSectionHeader {
      text: root.t("priority.needsAttention", "Needs Attention")
      foreground: root.palette.critical || root.urgent
      fontFamily: root.fontFamily
    }

    Repeater {
      model: root.noticesList
      delegate: BorderSurface {
        id: noticeCard
        required property var modelData
        readonly property bool isTransient: Model.isTransientErrorKind(modelData.errorKind)
        readonly property color noticeColor: isTransient ? (root.palette.warning || "#e59b3c") : (root.palette.critical || root.urgent)
        readonly property color noticeBorder: isTransient ? (root.palette.warning || "#e59b3c") : root.urgent
        width: parent.width
        radius: Style.cornerRadius
        color: Qt.rgba(noticeBorder.r, noticeBorder.g, noticeBorder.b, 0.08)
        borderSpec: Border.controlSpec("normal", noticeBorder, noticeBorder)
        implicitHeight: noticeRow.implicitHeight + Style.space(14)

        Row {
          id: noticeRow
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          anchors.leftMargin: Style.space(8)
          anchors.rightMargin: Style.space(8)
          spacing: Style.space(8)

          BrandMark {
            anchors.verticalCenter: parent.verticalCenter
            icon: modelData.icon || "omameter.svg"
            iconSize: Style.space(16)
            tintColor: noticeCard.noticeColor
            fontFamily: root.fontFamily
          }

          Column {
            anchors.verticalCenter: parent.verticalCenter
            width: Math.max(60, noticeRow.width - Style.space(16) - fixBtn.implicitWidth - Style.space(16))
            spacing: Style.space(2)

            PlainText {
              width: parent.width
              text: Model.accountHeader(modelData.providerName, modelData.accountName, { privacyMode: root.privacyMode })
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
              elide: Text.ElideRight
            }

            PlainText {
              width: parent.width
              text: Model.cleanText(modelData.message || "", 200)
              color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.78)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          Button {
            id: fixBtn
            anchors.verticalCenter: parent.verticalCenter
            text: noticeCard.isTransient
              ? root.t("priority.retry", "Retry")
              : root.t("priority.fix", "Fix")
            bordered: true
            foreground: noticeCard.noticeColor
            accent: noticeCard.noticeBorder
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            horizontalPadding: Style.space(8)
            verticalPadding: Style.space(4)
            onClicked: {
              var accId = String(modelData.accountId || "")
              if (noticeCard.isTransient) {
                if (root.service && accId !== "") root.service.refreshAccount(accId, true)
              } else {
                root.fixAccountRequested(accId)
              }
            }
          }
        }
      }
    }
  }

  // 7. Used Up section
  PrioritySection {
    tab: root
    visible: root.hasAccounts && root.usedUpList.length > 0
    title: root.t("priority.usedUp", "Used Up")
    rows: root.usedUpList
    dimmedRows: true
  }
}
