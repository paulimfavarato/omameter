import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// A clickable list row on its own surface: provider icon, bold title (with an
// optional plan pill), optional caption subtitle, optional "›" chevron. The
// whole card is the click target; the caller handles `clicked`.
//
// `alert` paints the row as failing: icon and subtitle in `alertColor`, border in
// `alertBorderColor`, and no hover border. `hoverBorder: false` keeps the border
// at "normal" on hover too (the hub list does that). The surface fill always
// follows hover. Callers pass display-safe text (Model.cleanText for external strings).
BorderSurface {
  id: root

  property string icon: "omameter.svg"
  property bool isHub: false
  property color badgeBackground: Commons.Color.popups.background
  property string title: ""
  property string pillText: ""
  property string subtitle: ""
  // Default hides an empty subtitle; the account list keeps the line even when empty.
  property bool subtitleVisible: subtitle !== ""
  property bool showChevron: true
  property bool alert: false
  property bool hoverBorder: true
  property real verticalPadding: Style.space(14)

  property color foreground: Commons.Color.popups.text
  property color accent: Commons.Color.accent
  property color alertColor: Commons.Color.urgent
  property color alertBorderColor: Commons.Color.urgent
  property string fontFamily: Style.font.family

  readonly property bool hot: hover.hovered

  signal clicked()

  radius: Style.cornerRadius
  color: hot
    ? Style.hoverFillFor(root.foreground, root.accent)
    : Style.normalFillFor(root.foreground, root.accent)
  Behavior on color { ColorAnimation { duration: Style.duration(80) } }
  borderSpec: Border.controlSpec((root.alert || !root.hoverBorder) ? "normal" : (root.hot ? "hover-cursor" : "normal"),
    root.alert ? root.alertBorderColor : root.foreground,
    root.alert ? root.alertBorderColor : root.accent)
  implicitHeight: row.implicitHeight + root.verticalPadding

  HoverHandler { id: hover }

  Row {
    id: row
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    anchors.leftMargin: Style.space(8)
    anchors.rightMargin: Style.space(8)
    spacing: Style.space(8)

    BrandMark {
      anchors.verticalCenter: parent.verticalCenter
      icon: root.icon
      iconSize: Style.space(16)
      tintColor: root.alert ? root.alertColor : root.foreground
      badgeBackground: root.badgeBackground
      fontFamily: root.fontFamily
      isHub: root.isHub
    }

    Column {
      anchors.verticalCenter: parent.verticalCenter
      width: Math.max(80, row.width - Style.space(16) - chev.implicitWidth - Style.space(22))
      spacing: Style.space(2)

      Row {
        width: parent.width
        spacing: Style.space(5)

        PlainText {
          anchors.verticalCenter: parent.verticalCenter
          text: root.title
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
          elide: Text.ElideRight
          width: Math.min(implicitWidth, parent.width - (pill.visible ? pill.width + Style.space(6) : 0))
        }

        Pill {
          id: pill
          visible: root.pillText !== ""
          anchors.verticalCenter: parent.verticalCenter
          radiusCap: Style.space(3)
          paddingX: Style.space(6)
          paddingY: Style.space(2)
          color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.16)
          text: root.pillText
          textColor: root.accent
          fontFamily: root.fontFamily
          fontSize: Math.max(8, Style.font.caption - 1)
          bold: true
        }
      }

      PlainText {
        visible: root.subtitleVisible
        width: parent.width
        text: root.subtitle
        color: root.alert
          ? root.alertColor
          : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.62)
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
    }

    PlainText {
      id: chev
      visible: root.showChevron
      anchors.verticalCenter: parent.verticalCenter
      text: "›"
      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.55)
      font.family: root.fontFamily
      font.pixelSize: Style.font.title
    }
  }

  MouseArea {
    anchors.fill: parent
    cursorShape: Qt.PointingHandCursor
    onClicked: root.clicked()
  }
}
