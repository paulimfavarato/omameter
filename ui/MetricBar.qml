import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// Renders a single metric row (bounded progress bar or open values/badge),
// with optional pace tick & tooltip, clickable amount/reset mode toggles,
// and inline hover actions (pin, hide, reorder, expand).
Item {
  id: root

  property var line: ({})
  property var palette: ({})
  property var strings: null
  property real nowMs: Date.now()
  property string resetDisplayMode: "relative"
  property string timeFormat: "auto"
  property bool privacyMode: false
  property color foreground: Commons.Color.popups.text
  property color accent: Commons.Color.accent
  property color urgent: Commons.Color.urgent
  property string fontFamily: Style.font.family

  property bool showActions: true
  property bool canReorder: false
  property bool canMoveUp: false
  property bool canMoveDown: false
  property bool expandable: false
  property bool expanded: false
  property bool compact: false

  signal toggleDisplayMode()
  signal toggleResetMode()
  signal togglePin()
  signal hideMetric()
  signal moveMetric(int direction)
  signal toggleExpand()

  function t(key, fb) { return Model.tr(strings, key, fb) }

  // Everything this row says comes from Model.describeLine (shared with the bar tooltip and Copy Summary).
  readonly property var desc: Model.describeLine(line, {
    resetDisplayMode: resetDisplayMode,
    timeFormat: timeFormat,
    nowMs: nowMs,
    privacyMode: privacyMode,
    palette: palette,
    foreground: foreground,
    urgent: urgent,
    strings: strings
  })
  readonly property bool isBounded: desc.isBounded
  readonly property bool isPinned: !!(line && line.isPinned)
  readonly property string severity: desc.severity
  readonly property color sevColor: desc.color
  readonly property real fillFraction: desc.fillFraction
  readonly property bool hasPaceMark: desc.hasPaceMark
  readonly property real paceFraction: desc.paceFraction
  readonly property string paceTip: desc.paceTip
  readonly property string meterState: line && line.meterState ? String(line.meterState) : ""
  readonly property string leftMetaText: desc.metaText
  readonly property string resetText: desc.resetText
  readonly property bool hovered: rowHover.hovered

  implicitWidth: parent ? parent.width : Style.space(320)
  implicitHeight: contentCol.implicitHeight + Style.space(compact ? 6 : 8)

  HoverHandler {
    id: rowHover
  }

  // Subtle hover tint
  Rectangle {
    anchors.fill: parent
    radius: Style.cornerRadius
    color: root.hovered ? Style.hoverFillFor(root.foreground, root.accent) : "transparent"
    Behavior on color { ColorAnimation { duration: Style.duration(80) } }
  }

  Column {
    id: contentCol
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    anchors.leftMargin: Style.space(compact ? 4 : 6)
    anchors.rightMargin: Style.space(compact ? 4 : 6)
    spacing: Style.space(compact ? 3 : 4)

    // Top row: Title + optional pin/expand badge + hover action buttons (or inline value for non-bounded)
    Item {
      width: parent.width
      height: Math.max(Style.space(20), titleRow.implicitHeight, rightControls.implicitHeight)

      Item {
        id: titleBox
        anchors.left: parent.left
        anchors.right: rightControls.left
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        height: parent.height

        Row {
          id: titleRow
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(5)

          // Optional expand chevron button
          PlainText {
            visible: root.expandable
            anchors.verticalCenter: parent.verticalCenter
            text: root.expanded ? "▾" : "▸"
            color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.7)
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          // Initials pill when showing a unified member sub-row
          BrandMark {
            visible: root.compact && root.line && !!root.line.badge
            anchors.verticalCenter: parent.verticalCenter
            icon: root.line ? (root.line.icon || "") : ""
            iconSize: Style.font.caption + 2
            tintColor: root.sevColor
            fontFamily: root.fontFamily
            badge: root.line && root.line.badge ? String(root.line.badge) : ""
            compactBadge: true
            isHub: !!(root.line && root.line.isHub)
          }

          PlainText {
            id: titleLabel
            anchors.verticalCenter: parent.verticalCenter
            width: Math.max(24, titleRow.width - (root.expandable ? Style.space(14) : 0) - (pinDot.visible ? Style.space(14) : 0))
            text: root.compact && root.desc.accountLabel !== "" ? root.desc.accountLabel : root.desc.title
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: root.compact ? Style.font.bodySmall : Style.font.body
            font.bold: !root.compact && root.severity !== "normal"
            elide: Text.ElideRight
          }

          PlainText {
            id: pinDot
            visible: root.isPinned && !root.compact
            anchors.verticalCenter: parent.verticalCenter
            text: "󰐃"
            color: root.accent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        MouseArea {
          anchors.fill: parent
          enabled: root.expandable
          cursorShape: root.expandable ? Qt.PointingHandCursor : Qt.ArrowCursor
          onClicked: root.toggleExpand()
        }
      }

      // Constant-size right slot: hover actions sit to the left of the figure so
      // hovering never hides the amount or breaks clicking it to toggle left/used.
      Row {
        id: rightControls
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(6)

        // Hover inline actions: Reorder Up/Down, Pin/Unpin, Hide
        Row {
          id: actionsRow
          visible: root.showActions
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(2)
          opacity: root.hovered ? 1.0 : 0.0
          enabled: root.hovered
          Behavior on opacity { NumberAnimation { duration: Style.duration(90) } }

          PanelActionButton {
            visible: root.canReorder
            enabled: root.canMoveUp
            opacity: enabled ? 1.0 : 0.25
            iconText: "↑"
            size: Style.space(20)
            fontSize: Style.font.caption
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: root.moveMetric(-1)
          }

          PanelActionButton {
            visible: root.canReorder
            enabled: root.canMoveDown
            opacity: enabled ? 1.0 : 0.25
            iconText: "↓"
            size: Style.space(20)
            fontSize: Style.font.caption
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: root.moveMetric(1)
          }

          PanelActionButton {
            iconText: root.isPinned ? "󰐄" : "󰐃"
            tooltipText: Model.autoTextSafe(
              root.isPinned
                ? root.t("card.unpinTooltip", "Unpin from top bar & Priority")
                : root.t("card.pinTooltip", "Pin to top bar & Priority"),
              false
            )
            size: Style.space(20)
            fontSize: Style.font.caption
            foreground: root.isPinned ? root.accent : root.foreground
            hoverColor: root.accent
            fontFamily: root.fontFamily
            onClicked: root.togglePin()
          }

          PanelActionButton {
            iconText: "󰈉"
            tooltipText: Model.autoTextSafe(root.t("card.hide", "Hide"), false)
            size: Style.space(20)
            fontSize: Style.font.caption
            foreground: root.foreground
            hoverColor: root.urgent
            fontFamily: root.fontFamily
            onClicked: root.hideMetric()
          }
        }

        // Figure slot (bounded amount or non-bounded badge/value), always visible and clickable
        Item {
          id: valueSlot
          anchors.verticalCenter: parent.verticalCenter
          implicitWidth: Math.max(
            boundedAmountBox.visible ? boundedAmountBox.implicitWidth : 0,
            nonBoundedRect.visible ? nonBoundedRect.implicitWidth : 0
          )
          implicitHeight: Math.max(
            Style.space(20),
            boundedAmountBox.visible ? boundedAmountBox.implicitHeight : 0,
            nonBoundedRect.visible ? nonBoundedRect.implicitHeight : 0
          )
          width: implicitWidth
          height: implicitHeight

          // Figure-first bounded amount on top-right; clickable to toggle remaining/used
          Item {
            id: boundedAmountBox
            visible: root.isBounded
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            implicitWidth: boundedAmountText.implicitWidth
            implicitHeight: boundedAmountText.implicitHeight
            width: implicitWidth
            height: implicitHeight

            PlainText {
              id: boundedAmountText
              anchors.centerIn: parent
              text: root.desc.amountText
              color: root.severity !== "normal" ? root.sevColor : root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
            }

            MouseArea {
              anchors.fill: parent
              enabled: root.isBounded
              cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
              onClicked: root.toggleDisplayMode()
            }
          }

          // For non-bounded metrics (open balances, badges, reset counts), show value inline on the right
          Pill {
            id: nonBoundedRect
            visible: !root.isBounded
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            readonly property bool isBadge: root.line && root.line.lineType === "badge"
            // Only a "badge" line is drawn as a pill; any other open value is the bare text.
            radius: isBadge ? (Style.cornerRadius > 0 ? Math.min(Style.cornerRadius, Style.space(4)) : 0) : 0
            color: isBadge ? Qt.rgba(root.sevColor.r, root.sevColor.g, root.sevColor.b, 0.16) : "transparent"
            paddingX: isBadge ? Style.space(8) : 0
            paddingY: isBadge ? Style.space(3) : 0
            text: root.desc.amountText
            textColor: root.sevColor
            fontFamily: root.fontFamily
            fontSize: root.compact ? Style.font.caption : Style.font.bodySmall
            bold: root.severity !== "normal"

            MouseArea {
              anchors.fill: parent
              enabled: root.expandable
              cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
              onClicked: root.toggleExpand()
            }
          }
        }
      }
    }

    // Bounded progress bar track + fill + optional pace tick mark
    Item {
      id: barContainer
      visible: root.isBounded
      width: parent.width
      height: Style.space(root.compact ? 5 : 6)

      Rectangle {
        id: track
        anchors.fill: parent
        radius: Style.cornerRadius > 0 ? Math.min(height / 2, Style.cornerRadius) : 0
        color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.14)

        Rectangle {
          id: fillRect
          anchors.left: parent.left
          anchors.top: parent.top
          anchors.bottom: parent.bottom
          width: Math.max(root.fillFraction > 0.005 ? Math.min(parent.width, height) : 0, Math.round(parent.width * root.fillFraction))
          radius: parent.radius
          color: root.sevColor

          Behavior on width {
            NumberAnimation {
              duration: Style.duration(200)
              easing.type: Easing.OutCubic
            }
          }
          Behavior on color {
            ColorAnimation { duration: Style.duration(140) }
          }
        }
      }

      // Even-pace tick mark
      Rectangle {
        id: paceTick
        visible: root.hasPaceMark
        width: Math.max(2, Style.space(2))
        height: barContainer.height + Style.space(4)
        anchors.verticalCenter: barContainer.verticalCenter
        x: Math.round(Model.clamp(barContainer.width * root.paceFraction - width / 2, 0, Math.max(0, barContainer.width - width)))
        radius: 1
        color: root.foreground
        opacity: 0.9
      }

      MouseArea {
        id: barHoverArea
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: root.expandable ? Qt.PointingHandCursor : Qt.ArrowCursor
        onClicked: if (root.expandable) root.toggleExpand()
      }

      PanelToolTip {
        visible: root.paceTip !== "" && root.leftMetaText === "" && barHoverArea.containsMouse
        text: root.paceTip
        fontFamily: root.fontFamily
      }
    }

    // Bottom meta row: inline pace projection or subtitle (left) + clickable live reset time (right)
    Item {
      visible: root.leftMetaText !== "" || root.resetText !== ""
      width: parent.width
      height: Math.max(leftMetaLabel.implicitHeight, resetLabel.implicitHeight)

      // Left: inline pace projection (bounded) or subtitle (non-bounded)
      PlainText {
        id: leftMetaLabel
        visible: root.leftMetaText !== ""
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        width: Math.min(implicitWidth, Math.max(0, parent.width - (resetBox.visible ? resetBox.width + Style.space(8) : 0)))
        text: root.leftMetaText
        color: {
          if (root.isBounded) {
            if (root.severity === "critical") return root.sevColor
            if (root.meterState === "close_to_limit" || root.meterState === "running_out") {
              return root.palette.warning || root.accent
            }
            return Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.66)
          }
          return Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.62)
        }
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }

      // Clickable reset time flips relative/absolute
      Item {
        id: resetBox
        visible: root.resetText !== ""
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        width: resetLabel.implicitWidth
        height: resetLabel.implicitHeight

        PlainText {
          id: resetLabel
          text: root.resetText
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.6)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        MouseArea {
          anchors.fill: parent
          enabled: root.resetText !== ""
          cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
          onClicked: root.toggleResetMode()
        }
      }
    }
  }
}
