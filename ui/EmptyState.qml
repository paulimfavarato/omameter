import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// The "nothing here yet" card: centred brand icon, title, optional subtitle and
// one call-to-action button. The caller owns the action: handle `actionClicked`.
//
// `wrapTitle` keeps the two title layouts the tabs already had (Priority wraps,
// All Usage does not).
BorderSurface {
  id: root

  property string icon: "omameter.svg"
  property string title: ""
  property string subtitle: ""
  property bool wrapTitle: false
  property string actionText: ""
  property string actionIcon: "+"
  property color foreground: Commons.Color.popups.text
  property color accent: Commons.Color.accent
  property string fontFamily: Style.font.family

  signal actionClicked()

  radius: Style.cornerRadius
  color: Style.normalFillFor(root.foreground, root.accent)
  borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
  implicitHeight: col.implicitHeight + Style.space(28)

  Column {
    id: col
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    anchors.margins: Style.space(16)
    spacing: Style.space(10)

    BrandMark {
      anchors.horizontalCenter: parent.horizontalCenter
      icon: root.icon
      iconSize: Style.space(30)
      tintColor: root.accent
      fontFamily: root.fontFamily
    }

    PlainText {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      text: root.title
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.subtitle
      font.bold: true
      wrapMode: root.wrapTitle ? Text.WordWrap : Text.NoWrap
    }

    PlainText {
      visible: root.subtitle !== ""
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      text: root.subtitle
      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.72)
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.WordWrap
    }

    Button {
      anchors.horizontalCenter: parent.horizontalCenter
      text: root.actionText
      iconText: root.actionIcon
      bordered: true
      selected: true
      foreground: root.foreground
      accent: root.accent
      fontFamily: root.fontFamily
      onClicked: root.actionClicked()
    }
  }
}
