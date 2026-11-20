import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model
import "../Settings.js" as Settings

// All Usage tab: provider cards with Unify/Separate, provider & metric
// reordering, per-account inline rename, external links, expandable unified
// member rows, Usage Breakdown (top 5 models + Other), and Limit Resets with
// two-step Use -> Reset confirmation.
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

  // Track interactive state
  property var expandedMetrics: ({})
  property var expandedLinks: ({})
  property string renamingAccountId: ""
  property string renameDraft: ""
  property string confirmResetKey: ""
  property bool inputFocused: false

  signal addAccountRequested()
  signal fixAccountRequested(string accountId)

  function t(key, fb) { return Model.tr(strings, key, fb) }

  function tn(key, count, fb) {
    return strings ? strings.tn(key, count, fb) : (fb || key)
  }

  function isMetricExpanded(metricId) {
    return !!expandedMetrics[String(metricId || "")]
  }

  function toggleMetricExpanded(metricId) {
    var id = String(metricId || "")
    var next = {}
    for (var k in expandedMetrics) next[k] = expandedMetrics[k]
    next[id] = !next[id]
    expandedMetrics = next
  }

  function isLinksOpen(kind) {
    return !!expandedLinks[String(kind || "")]
  }

  function toggleLinksOpen(kind) {
    var k = String(kind || "")
    var next = {}
    for (var key in expandedLinks) next[key] = expandedLinks[key]
    next[k] = !next[k]
    expandedLinks = next
  }

  function beginRename(accId, currentName) {
    renamingAccountId = String(accId || "")
    renameDraft = String(currentName || "")
  }

  function commitRename() {
    if (service && renamingAccountId !== "" && renameDraft.trim() !== "") {
      service.renameAccount(renamingAccountId, renameDraft.trim())
    }
    renamingAccountId = ""
    renameDraft = ""
    inputFocused = false
  }

  function cancelRename() {
    renamingAccountId = ""
    renameDraft = ""
    inputFocused = false
  }

  readonly property var cards: (dashboard && dashboard.allUsage) ? dashboard.allUsage : []
  readonly property bool hasAccounts: !!(dashboard && dashboard.hasAccounts)
  readonly property bool privacyMode: !!(settings && settings.privacyMode)

  spacing: Style.space(10)

  // Empty state
  EmptyState {
    visible: !root.hasAccounts
    width: parent.width
    title: root.t("empty.title", "No accounts yet")
    actionText: root.t("empty.addAccount", "Add Account")
    foreground: root.foreground
    accent: root.accent
    fontFamily: root.fontFamily
    onActionClicked: root.addAccountRequested()
  }

  // Provider Cards
  Repeater {
    model: root.cards

    delegate: BorderSurface {
      id: cardBox
      required property var modelData
      required property int index

      readonly property var card: modelData
      readonly property var cardAccounts: card.accounts || []
      readonly property var cardLinks: card.links || []
      readonly property bool showLinks: root.isLinksOpen(card.kind)

      width: parent.width
      radius: Style.cornerRadius
      color: Style.normalFillFor(root.foreground, root.accent)
      borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
      implicitHeight: cardCol.implicitHeight + Style.space(16)

      Column {
        id: cardCol
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.leftMargin: Style.space(10)
        anchors.rightMargin: Style.space(10)
        spacing: Style.space(8)

        // Provider Card Header
        Item {
          width: parent.width
          height: Math.max(headerLeft.implicitHeight, headerRight.implicitHeight)

          Row {
            id: headerLeft
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(7)

            BrandMark {
              anchors.verticalCenter: parent.verticalCenter
              icon: card.icon || "omameter.svg"
              iconSize: Style.space(16)
              tintColor: root.foreground
              badgeBackground: root.background
              fontFamily: root.fontFamily
            }

            PlainText {
              anchors.verticalCenter: parent.verticalCenter
              text: String(card.displayName || card.kind || "")
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }
          }

          Row {
            id: headerRight
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(3)

            // Unify / Separate Accounts toggle (Claude & ChatGPT with 2+ accounts)
            Button {
              visible: !!card.canUnify
              anchors.verticalCenter: parent.verticalCenter
              text: card.isUnified ? root.t("card.separate", "Separate Accounts") : root.t("card.unify", "Unify Accounts")
              bordered: true
              selected: !!card.isUnified
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              horizontalPadding: Style.space(6)
              verticalPadding: Style.space(2)
              onClicked: if (root.service) root.service.setProviderUnified(card.kind, !card.isUnified)
            }

            // Reorder provider up/down
            PanelActionButton {
              visible: root.cards.length > 1
              enabled: cardBox.index > 0
              opacity: enabled ? 1.0 : 0.25
              iconText: "↑"
              size: Style.space(20)
              fontSize: Style.font.caption
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: if (root.service) root.service.moveProvider(card.kind, -1)
            }

            PanelActionButton {
              visible: root.cards.length > 1
              enabled: cardBox.index < root.cards.length - 1
              opacity: enabled ? 1.0 : 0.25
              iconText: "↓"
              size: Style.space(20)
              fontSize: Style.font.caption
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: if (root.service) root.service.moveProvider(card.kind, 1)
            }

            // Links drawer toggle
            PanelActionButton {
              visible: cardBox.cardLinks.length > 0
              iconText: "󰌹"
              tooltipText: Model.autoTextSafe(root.t("card.usageDashboard", "Usage Dashboard"), false)
              size: Style.space(20)
              fontSize: Style.font.bodySmall
              foreground: cardBox.showLinks ? root.accent : root.foreground
              hoverColor: root.accent
              fontFamily: root.fontFamily
              onClicked: root.toggleLinksOpen(card.kind)
            }
          }
        }

        // Expandable Links chips bar
        Flow {
          visible: cardBox.showLinks && cardBox.cardLinks.length > 0
          width: parent.width
          spacing: Style.space(5)

          Repeater {
            model: cardBox.cardLinks
            delegate: Button {
              required property var modelData
              text: modelData.id ? root.t("card." + modelData.id, String(modelData.title || "")) : String(modelData.title || "")
              iconText: "↗"
              bordered: true
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              iconSize: Style.font.caption
              horizontalPadding: Style.space(7)
              verticalPadding: Style.space(3)
              onClicked: if (root.service) root.service.openUrl(modelData.url)
            }
          }
        }

        // Per-account sections inside this provider card
        Repeater {
          model: cardBox.cardAccounts

          delegate: Column {
            id: accSec
            required property var modelData
            required property int index

            readonly property var acc: modelData
            readonly property var lines: acc.lines || []
            readonly property bool isRenaming: !acc.isUnified && root.renamingAccountId === String(acc.id || "")
            readonly property string displayAccountName: acc.isUnified
              ? root.tn("card.accountsCount", acc.memberCount || 2, (acc.memberCount || 2) + " Accounts").arg(acc.memberCount || 2)
              : Model.displayAccountName(acc.accountName, { privacyMode: root.privacyMode })
            // Same rule as every "provider · account" header: an account named after its provider adds nothing.
            readonly property bool namedLikeProvider: !acc.isUnified
              && Model.accountHeader(card.displayName, acc.accountName, {}) === Model.accountHeader(card.displayName, "", {})

            width: parent.width
            spacing: Style.space(6)

            PanelSeparator {
              visible: accSec.index > 0
              foreground: root.foreground
            }

            // Account Sub-header (Plan badge, Stale, Hub host, Rename, Refresh)
            Item {
              width: parent.width
              height: Math.max(accLeftRow.implicitHeight, accRightRow.implicitHeight)

              Row {
                id: accLeftRow
                visible: !accSec.isRenaming
                anchors.left: parent.left
                anchors.right: accRightRow.left
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(6)

                PlainText {
                  visible: accSec.displayAccountName !== ""
                    && (cardBox.cardAccounts.length > 1 || !accSec.namedLikeProvider)
                  anchors.verticalCenter: parent.verticalCenter
                  text: accSec.displayAccountName
                  color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.82)
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                  elide: Text.ElideRight
                  width: Math.min(
                    implicitWidth,
                    Math.max(
                      Style.space(80),
                      accLeftRow.width
                        - (planPill.visible ? planPill.width + accLeftRow.spacing : 0)
                        - (hubPill.visible ? hubPill.width + accLeftRow.spacing : 0)
                        - (stalePill.visible ? stalePill.width + accLeftRow.spacing : 0)
                    )
                  )
                }

                // Plan badge pill
                Pill {
                  id: planPill
                  visible: !!acc.planMultiple || !!(acc.plan && String(acc.plan).trim() !== "")
                  anchors.verticalCenter: parent.verticalCenter
                  color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.16)
                  border.width: 1
                  border.color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.3)
                  text: acc.planMultiple ? root.t("card.multiplePlans", "Multiple") : Model.cleanText(acc.plan, 60)
                  textColor: root.accent
                  fontFamily: root.fontFamily
                  fontSize: Math.max(9, Style.font.caption - 1)
                  bold: true
                }

                // Hub badge
                Pill {
                  id: hubPill
                  visible: !!(acc.isHub && acc.hubHost)
                  anchors.verticalCenter: parent.verticalCenter
                  color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.10)
                  text: String(acc.hubHost || "")
                  textColor: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.72)
                  fontFamily: root.fontFamily
                  fontSize: Math.max(9, Style.font.caption - 1)
                }

                // Stale pill
                Pill {
                  id: stalePill
                  visible: !!acc.stale
                  anchors.verticalCenter: parent.verticalCenter
                  color: Qt.rgba(1, 0.65, 0, 0.18)
                  text: root.t("card.stale", "Stale data")
                  textColor: root.palette.warning || "#e59b3c"
                  fontFamily: root.fontFamily
                  fontSize: Math.max(9, Style.font.caption - 1)
                }
              }

              // Inline rename editor
              Row {
                visible: accSec.isRenaming
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(4)

                TextField {
                  id: renameInput
                  width: Math.max(100, parent.width - saveRenameBtn.implicitWidth - cancelRenameBtn.implicitWidth - Style.space(8))
                  verticalPadding: Style.space(3)
                  text: root.renameDraft
                  foreground: root.foreground
                  accent: root.accent
                  onTextChanged: if (accSec.isRenaming) root.renameDraft = text
                  onActiveFocusChanged: root.inputFocused = activeFocus
                  onAccepted: root.commitRename()
                  Keys.onEscapePressed: root.cancelRename()
                  Component.onCompleted: if (accSec.isRenaming) forceActiveFocus()
                }

                Button {
                  id: saveRenameBtn
                  text: root.t("accounts.save", "Save")
                  bordered: true
                  selected: true
                  foreground: root.foreground
                  accent: root.accent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  horizontalPadding: Style.space(7)
                  verticalPadding: Style.space(3)
                  onClicked: root.commitRename()
                }

                Button {
                  id: cancelRenameBtn
                  text: root.t("accounts.cancel", "Cancel")
                  bordered: true
                  foreground: root.foreground
                  accent: root.accent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  horizontalPadding: Style.space(7)
                  verticalPadding: Style.space(3)
                  onClicked: root.cancelRename()
                }
              }

              Row {
                id: accRightRow
                visible: !accSec.isRenaming
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(2)

                // Per-account extra links (e.g. Copilot Organization Billing)
                Repeater {
                  model: (!acc.isUnified && acc.organization && acc.links) ? acc.links.filter(function(l) { return l.id === "orgBilling" }) : []
                  delegate: PanelActionButton {
                    required property var modelData
                    iconText: "󰌹"
                    tooltipText: Model.autoTextSafe(root.t("card.orgBilling", "Organization Billing"), false)
                    size: Style.space(20)
                    fontSize: Style.font.caption
                    foreground: root.foreground
                    hoverColor: root.accent
                    fontFamily: root.fontFamily
                    onClicked: if (root.service) root.service.openUrl(modelData.url)
                  }
                }

                // Inline Rename trigger
                PanelActionButton {
                  visible: !acc.isUnified && !root.privacyMode
                  iconText: "󰏫"
                  tooltipText: Model.autoTextSafe(root.t("card.rename", "Rename"), false)
                  size: Style.space(20)
                  fontSize: Style.font.caption
                  foreground: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.72)
                  hoverColor: root.foreground
                  fontFamily: root.fontFamily
                  onClicked: root.beginRename(acc.id, acc.label || acc.accountName)
                }

                // Refresh button (spins while refreshing)
                Button {
                  iconText: "󰑐"
                  tooltipText: Model.autoTextSafe(root.t("card.refresh", "Refresh"), false)
                  iconSpinning: !!acc.refreshing
                  iconSize: Style.font.caption + 2
                  horizontalPadding: Style.space(4)
                  verticalPadding: Style.space(2)
                  foreground: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.78)
                  accent: root.accent
                  fontFamily: root.fontFamily
                  onClicked: {
                    if (!root.service) return
                    if (acc.isUnified) root.service.refreshAll(true)
                    else root.service.refreshAccount(acc.id, true)
                  }
                }
              }
            }

            // Account Error or Notice banner
            BorderSurface {
              id: accBanner
              readonly property bool isTransientErr: !!acc.error && Model.isTransientErrorKind(acc.errorKind)
              readonly property color bannerTone: acc.error
                ? (isTransientErr ? (root.palette.warning || "#e59b3c") : root.urgent)
                : root.accent
              readonly property color bannerTextColor: acc.error
                ? (isTransientErr ? (root.palette.warning || "#e59b3c") : (root.palette.critical || root.urgent))
                : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.85)
              visible: !!(acc.error || acc.notice)
              width: parent.width
              radius: Style.cornerRadius
              color: Qt.rgba(bannerTone.r, bannerTone.g, bannerTone.b, acc.error ? 0.10 : 0.08)
              borderSpec: Border.controlSpec("normal", bannerTone, bannerTone)
              implicitHeight: errRow.implicitHeight + Style.space(10)

              Row {
                id: errRow
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.margins: Style.space(6)
                spacing: Style.space(6)

                PlainText {
                  anchors.verticalCenter: parent.verticalCenter
                  width: Math.max(60, errRow.width - (accFixBtn.visible ? accFixBtn.implicitWidth + Style.space(6) : 0))
                  text: Model.cleanText(acc.error || acc.notice || "", 220)
                  color: accBanner.bannerTextColor
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  wrapMode: Text.WordWrap
                }

                Button {
                  id: accFixBtn
                  visible: !!acc.error && !acc.isUnified
                  anchors.verticalCenter: parent.verticalCenter
                  text: accBanner.isTransientErr
                    ? root.t("priority.retry", "Retry")
                    : root.t("priority.fix", "Fix")
                  bordered: true
                  foreground: accBanner.bannerTextColor
                  accent: accBanner.bannerTone
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  horizontalPadding: Style.space(6)
                  verticalPadding: Style.space(2)
                  onClicked: {
                    if (accBanner.isTransientErr) {
                      if (root.service) root.service.refreshAccount(String(acc.id || ""), true)
                    } else {
                      root.fixAccountRequested(String(acc.id || ""))
                    }
                  }
                }
              }
            }

            // No usage reported fallback
            PlainText {
              visible: accSec.lines.length === 0 && !acc.error && !acc.notice
              width: parent.width
              text: root.t("card.noQuota", "No usage reported")
              color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.52)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            // Metrics for this account
            Repeater {
              model: accSec.lines

              delegate: Column {
                id: metricDelegate
                required property var modelData
                required property int index

                readonly property var line: modelData
                readonly property var members: line.memberRows || []
                readonly property var breakdown: line.breakdown || null
                readonly property var resetItems: line.resetItems || []
                readonly property bool hasDetails: members.length > 0 || breakdown !== null || resetItems.length > 0
                readonly property bool isExpanded: root.isMetricExpanded(line.metricId)

                width: parent.width
                spacing: Style.space(4)

                MetricBar {
                  width: parent.width
                  line: metricDelegate.line
                  palette: root.palette
                  strings: root.strings
                  nowMs: root.nowMs
                  resetDisplayMode: String(root.settings && root.settings.resetDisplayMode ? root.settings.resetDisplayMode : "relative")
                  timeFormat: String(root.settings && root.settings.timeFormat ? root.settings.timeFormat : "auto")
                  privacyMode: root.privacyMode
                  foreground: root.foreground
                  accent: root.accent
                  urgent: root.urgent
                  fontFamily: root.fontFamily
                  showActions: true
                  canReorder: !acc.isUnified && accSec.lines.length > 1
                  canMoveUp: metricDelegate.index > 0
                  canMoveDown: metricDelegate.index < accSec.lines.length - 1
                  expandable: metricDelegate.hasDetails
                  expanded: metricDelegate.isExpanded
                  onToggleDisplayMode: root.service && root.service.toggleDisplayMode()
                  onToggleResetMode: root.service && root.service.toggleResetMode()
                  onTogglePin: {
                    if (!root.service) return
                    var ids = line.copyIds || [line.metricId]
                    root.service.setMetricPinned(ids, !line.isPinned)
                  }
                  onHideMetric: {
                    if (!root.service) return
                    var ids = line.copyIds || [line.metricId]
                    root.service.setMetricHidden(ids, true)
                  }
                  onMoveMetric: function(dir) {
                    if (root.service && !acc.isUnified) root.service.moveMetric(acc.id, line.key, dir)
                  }
                  onToggleExpand: root.toggleMetricExpanded(line.metricId)
                }

                // Expandable Detail Drawer: Unified Member Breakdown
                Column {
                  visible: metricDelegate.isExpanded && metricDelegate.members.length > 0
                  width: parent.width
                  leftPadding: Style.space(12)
                  spacing: Style.space(4)

                  Repeater {
                    model: metricDelegate.members
                    delegate: MetricBar {
                      required property var modelData
                      width: parent.width - Style.space(12)
                      line: modelData
                      privacyMode: root.privacyMode
                      palette: root.palette
                      strings: root.strings
                      nowMs: root.nowMs
                      resetDisplayMode: String(root.settings && root.settings.resetDisplayMode ? root.settings.resetDisplayMode : "relative")
                      timeFormat: String(root.settings && root.settings.timeFormat ? root.settings.timeFormat : "auto")
                      foreground: root.foreground
                      accent: root.accent
                      urgent: root.urgent
                      fontFamily: root.fontFamily
                      showActions: false
                      compact: true
                      onToggleDisplayMode: root.service && root.service.toggleDisplayMode()
                      onToggleResetMode: root.service && root.service.toggleResetMode()
                    }
                  }
                }

                // Expandable Detail Drawer: Usage Breakdown (top 5 models + Other, tokens, spend)
                BorderSurface {
                  visible: metricDelegate.isExpanded && metricDelegate.breakdown !== null
                  width: parent.width - Style.space(10)
                  x: Style.space(10)
                  radius: Style.cornerRadius
                  color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.04)
                  borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
                  implicitHeight: bdCol.implicitHeight + Style.space(12)

                  Column {
                    id: bdCol
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.margins: Style.space(8)
                    spacing: Style.space(6)

                    readonly property var bd: metricDelegate.breakdown || {}
                    readonly property var bdModels: bd.models || []
                    readonly property var tokenPartKeys: bd.tokenParts ? Object.keys(bd.tokenParts) : []

                    // Header: Breakdown title + totals
                    Item {
                      width: parent.width
                      height: Math.max(bdTitle.implicitHeight, bdTotals.implicitHeight)

                      PlainText {
                        id: bdTitle
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        text: bdCol.bd.title || root.t("breakdown.title", "Usage Breakdown")
                        color: root.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: true
                      }

                      Row {
                        id: bdTotals
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: Style.space(8)

                        PlainText {
                          visible: bdCol.bd.tokens !== null && bdCol.bd.tokens !== undefined
                          text: root.t("breakdown.tokens", "Tokens") + ": " + Model.formatCompactNumber(bdCol.bd.tokens)
                          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.75)
                          font.family: root.fontFamily
                          font.pixelSize: Style.font.caption
                        }

                        PlainText {
                          visible: bdCol.bd.spend !== null && bdCol.bd.spend !== undefined
                          text: root.t("breakdown.spend", "Spend") + ": " + Model.formatCurrency(bdCol.bd.spend, bdCol.bd.currencyCode)
                          color: root.accent
                          font.family: root.fontFamily
                          font.pixelSize: Style.font.caption
                          font.bold: true
                        }
                      }
                    }

                    // Token part chips (Input, Cache Read, Output, etc.)
                    Flow {
                      visible: bdCol.tokenPartKeys.length > 0
                      width: parent.width
                      spacing: Style.space(6)

                      Repeater {
                        model: bdCol.tokenPartKeys
                        delegate: PlainText {
                          required property var modelData
                          text: String(modelData) + ": " + Model.formatCompactNumber(bdCol.bd.tokenParts[modelData])
                          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.65)
                          font.family: root.fontFamily
                          font.pixelSize: Math.max(9, Style.font.caption - 1)
                        }
                      }
                    }

                    // Top 5 models + Other
                    Repeater {
                      model: bdCol.bdModels
                      delegate: Column {
                        required property var modelData
                        width: parent.width
                        spacing: Style.space(2)

                        Item {
                          width: parent.width
                          height: mName.implicitHeight

                          PlainText {
                            id: mName
                            anchors.left: parent.left
                            width: Math.max(60, parent.width - mVal.implicitWidth - Style.space(8))
                            text: modelData.isOther ? root.t("breakdown.other", "Other") : Model.cleanText(modelData.name, 120)
                            color: root.foreground
                            font.family: root.fontFamily
                            font.pixelSize: Style.font.caption
                            elide: Text.ElideRight
                          }

                          PlainText {
                            id: mVal
                            anchors.right: parent.right
                            text: {
                              var parts = []
                              if (modelData.spend !== null && modelData.spend !== undefined)
                                parts.push(Model.formatCurrency(modelData.spend, bdCol.bd.currencyCode))
                              if (modelData.tokens !== null && modelData.tokens !== undefined)
                                parts.push(Model.formatCompactNumber(modelData.tokens))
                              if (modelData.share !== null && modelData.share !== undefined)
                                parts.push(Math.round(Number(modelData.share) * 100) + "%")
                              return parts.join(" · ")
                            }
                            color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.72)
                            font.family: root.fontFamily
                            font.pixelSize: Style.font.caption
                          }
                        }

                        Rectangle {
                          visible: modelData.share !== null && modelData.share !== undefined && Number(modelData.share) > 0
                          width: parent.width
                          height: Style.space(3)
                          radius: 1
                          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)

                          Rectangle {
                            height: parent.height
                            width: Math.round(parent.width * Model.clamp(Number(modelData.share), 0, 1))
                            radius: 1
                            color: root.accent
                          }
                        }
                      }
                    }

                    PlainText {
                      visible: !!(bdCol.bd.note)
                      width: parent.width
                      text: String(bdCol.bd.note || "")
                      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.58)
                      font.family: root.fontFamily
                      font.pixelSize: Math.max(9, Style.font.caption - 1)
                      wrapMode: Text.WordWrap
                    }
                  }
                }

                // Expandable Detail Drawer: Limit Resets with two-step Use -> Reset confirmation
                BorderSurface {
                  visible: metricDelegate.isExpanded && metricDelegate.resetItems.length > 0
                  width: parent.width - Style.space(10)
                  x: Style.space(10)
                  radius: Style.cornerRadius
                  color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.04)
                  borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
                  implicitHeight: resetCol.implicitHeight + Style.space(12)

                  Column {
                    id: resetCol
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.margins: Style.space(8)
                    spacing: Style.space(6)

                    PlainText {
                      text: root.t("resets.title", "Limit Resets")
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: true
                    }

                    Repeater {
                      model: metricDelegate.resetItems
                      delegate: Item {
                        id: resetRow
                        required property var modelData
                        readonly property string rKey: String(acc.id || "") + ":" + String(modelData.id || "")
                        readonly property bool confirming: root.confirmResetKey === rKey
                        readonly property bool expiringSoon: Model.isResetExpiringSoon(modelData.expiresAt, root.nowMs)
                        readonly property string expiryStr: Model.formatResetExpiry(modelData.expiresAt, root.nowMs, root.strings)

                        width: parent.width
                        height: Math.max(rInfo.implicitHeight, rActions.implicitHeight)

                        Column {
                          id: rInfo
                          anchors.left: parent.left
                          anchors.right: rActions.left
                          anchors.rightMargin: Style.space(6)
                          anchors.verticalCenter: parent.verticalCenter
                          spacing: Style.space(1)

                          Row {
                            spacing: Style.space(5)

                            PlainText {
                              anchors.verticalCenter: parent.verticalCenter
                              text: String(modelData.title || "Reset")
                              color: root.foreground
                              font.family: root.fontFamily
                              font.pixelSize: Style.font.bodySmall
                            }

                            Pill {
                              visible: expiringSoon
                              anchors.verticalCenter: parent.verticalCenter
                              radiusCap: Style.space(3)
                              paddingX: Style.space(6)
                              paddingY: Style.space(2)
                              color: Qt.rgba(1, 0.6, 0, 0.18)
                              text: root.t("resets.expiring", "Expiring")
                              textColor: root.palette.warning || "#e59b3c"
                              fontFamily: root.fontFamily
                              fontSize: Math.max(8, Style.font.caption - 1)
                              bold: true
                            }
                          }

                          PlainText {
                            visible: text !== ""
                            text: !modelData.usableNow && modelData.unavailableReason
                              ? String(modelData.unavailableReason)
                              : (expiryStr ? root.t("resets.expiresIn", "Expires in %1").arg(expiryStr) : "")
                            color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.6)
                            font.family: root.fontFamily
                            font.pixelSize: Style.font.caption
                            elide: Text.ElideRight
                            width: parent.width
                          }
                        }

                        ConfirmStep {
                          id: rActions
                          anchors.right: parent.right
                          anchors.verticalCenter: parent.verticalCenter
                          spacing: Style.space(4)
                          confirming: resetRow.confirming
                          actionVisible: !!resetRow.modelData.usableNow && !acc.isUnified
                          destructiveAction: false
                          actionText: root.t("resets.use", "Use")
                          confirmText: root.t("resets.confirm", "Reset")
                          cancelText: root.t("resets.cancel", "Cancel")
                          foreground: root.foreground
                          accent: root.accent
                          dangerColor: root.palette.critical || root.urgent
                          dangerAccent: root.urgent
                          fontFamily: root.fontFamily
                          fontSize: Style.font.caption
                          horizontalPadding: Style.space(8)
                          verticalPadding: Style.space(3)
                          cancelHorizontalPadding: Style.space(7)
                          onActionClicked: root.confirmResetKey = resetRow.rKey
                          onCancelled: root.confirmResetKey = ""
                          onConfirmed: {
                            root.confirmResetKey = ""
                            if (root.service) root.service.claimReset(acc.id, resetRow.modelData)
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
  }
}
