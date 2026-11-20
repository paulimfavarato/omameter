import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// One row of the Priority tab: ring (with the brand mark inside), title + badge,
// account header, amount, note, and a Pin/Unpin button that fades in on hover.
// Everything it says comes from Model.describeLine(item, lineOpts), shared with
// the bar tooltip and Copy Summary. It never talks to the service: clicks become
// signals and the Priority tab wires them.
//
// Hover never changes the row's size: the pin slot is always laid out (when
// `showPin`) and only its opacity/enabled follow the hover.
BorderSurface {
  id: root

  property var item: ({})
  property bool dimmedRow: false
  // PriorityTab.lineOpts: resetDisplayMode, timeFormat, nowMs, privacyMode, palette, foreground, urgent.
  property var lineOpts: ({})
  // false keeps the pin slot out of the layout (no service to pin with).
  property bool showPin: true
  property var palette: ({})
  property var strings: null
  property color foreground: Commons.Color.popups.text
  property color background: Commons.Color.popups.background
  property color accent: Commons.Color.accent
  property string fontFamily: Style.font.family

  signal openAllUsage()
  signal toggleDisplayMode()
  signal toggleResetMode()
  signal togglePin()

  function t(key, fb) { return Model.tr(strings, key, fb) }

  readonly property var desc: Model.describeLine(item, lineOpts)
  readonly property string sev: desc.severity
  readonly property color sevColor: desc.color
  readonly property bool isBurningFast: item && item.priorityReason === "burningFast"
  readonly property bool isBounded: desc.isBounded
  readonly property bool isPinned: !!(item && item.isPinned)
  readonly property bool hovered: itemHover.hovered

  width: parent ? parent.width : Style.space(350)
  implicitHeight: innerRow.implicitHeight + Style.space(12)
  radius: Style.cornerRadius
  opacity: dimmedRow ? 0.72 : 1.0
  color: hovered
    ? Style.hoverFillFor(root.foreground, root.accent)
    : Style.normalFillFor(root.foreground, root.accent)
  borderSpec: Border.controlSpec(hovered ? "hover-cursor" : "normal", root.foreground, root.accent)

  Behavior on color { ColorAnimation { duration: Style.duration(80) } }

  HoverHandler { id: itemHover }

  Row {
    id: innerRow
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    anchors.leftMargin: Style.space(8)
    anchors.rightMargin: Style.space(8)
    spacing: Style.space(8)

    // Left: PriorityRing (with BrandMark centered or beside)
    Item {
      width: Style.space(30)
      height: Style.space(30)
      anchors.verticalCenter: parent.verticalCenter

      PriorityRing {
        visible: root.isBounded
        anchors.centerIn: parent
        size: Style.space(30)
        strokeWidth: Math.max(2.5, Style.spaceReal(3))
        fillFraction: root.desc.fillFraction
        paceMark: root.item ? root.item.paceMark : null
        ringColor: root.isBurningFast ? (root.palette.warning || root.accent) : root.sevColor
        trackColor: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.14)
        paceColor: root.foreground
      }

      BrandMark {
        anchors.centerIn: parent
        icon: root.item && root.item.icon ? String(root.item.icon) : "omameter.svg"
        iconSize: root.isBounded ? Style.space(13) : Style.space(16)
        tintColor: root.sev !== "normal" ? root.sevColor : root.foreground
        badgeBackground: root.background
        fontFamily: root.fontFamily
        isHub: !!(root.item && root.item.isHub)
      }

      MouseArea {
        id: ringHover
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.openAllUsage()
      }

      PanelToolTip {
        visible: root.desc.paceTip !== "" && ringHover.containsMouse
        text: root.desc.paceTip
        fontFamily: root.fontFamily
      }
    }

    // Center: Provider + Metric title + optional badge/account subtitle
    Item {
      anchors.verticalCenter: parent.verticalCenter
      width: Math.max(60, innerRow.width - Style.space(30) - rightBlock.implicitWidth - Style.space(16))
      height: centerCol.implicitHeight

      Column {
        id: centerCol
        anchors.left: parent.left
        anchors.right: parent.right
        spacing: Style.space(2)

        Row {
          width: parent.width
          spacing: Style.space(5)

          PlainText {
            anchors.verticalCenter: parent.verticalCenter
            text: root.desc.title
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            font.bold: true
            elide: Text.ElideRight
            width: Math.min(implicitWidth, parent.width - (badgePill.visible ? badgePill.width + Style.space(5) : 0))
          }

          Pill {
            id: badgePill
            visible: !!(root.item && root.item.badge)
            anchors.verticalCenter: parent.verticalCenter
            text: root.item && root.item.badge ? String(root.item.badge) : ""
            color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)
            textColor: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.85)
            fontFamily: root.fontFamily
            fontSize: Math.max(8, Style.font.caption - 1)
            bold: true
            paddingX: Style.space(6)
            paddingY: Style.space(2)
          }
        }

        PlainText {
          width: parent.width
          text: root.desc.header
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.65)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: root.openAllUsage()
      }
    }

    // Right: Amount + Priority Note + hover Pin/Unpin button
    Row {
      id: rightBlock
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(4)

      Column {
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(2)

        Item {
          anchors.right: parent.right
          width: amtTxt.implicitWidth
          height: amtTxt.implicitHeight

          PlainText {
            id: amtTxt
            text: root.desc.amountText
            color: root.sev !== "normal" ? root.sevColor : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.bold: true
          }

          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.toggleDisplayMode()
          }
        }

        Item {
          visible: noteTxt.text !== ""
          anchors.right: parent.right
          width: noteTxt.implicitWidth
          height: noteTxt.implicitHeight

          PlainText {
            id: noteTxt
            text: root.desc.noteText
            color: root.isBurningFast
              ? (root.palette.warning || root.accent)
              : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.62)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: root.isBurningFast
          }

          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.toggleResetMode()
          }
        }
      }

      // Fixed-size slot: hover only changes opacity/enabled, never the layout.
      PanelActionButton {
        visible: root.showPin
        opacity: root.hovered ? 1.0 : (root.isPinned ? 0.65 : 0.0)
        enabled: root.hovered || root.isPinned
        Behavior on opacity { NumberAnimation { duration: Style.duration(90) } }
        anchors.verticalCenter: parent.verticalCenter
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
    }
  }
}
