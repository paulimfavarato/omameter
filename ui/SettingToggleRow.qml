import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// A settings line: optional provider icon + label on the left, ToggleSwitch on
// the right, optional caption-size description underneath. The caller owns the
// value: bind `checked` and flip it in `onToggled`.
//
// `wrapLabel` keeps the two label layouts the screen already had: true makes the
// label stop 8px before the switch and wrap; false leaves it at its natural width.
Column {
  id: root

  property string label: ""
  property string description: ""
  // Provider icon before the label (BrandMark, which falls back to a glyph when
  // `icon` is empty), used by Unify Accounts.
  property bool showIcon: false
  property string icon: ""
  property bool checked: false
  property bool wrapLabel: true
  property color foreground: Commons.Color.popups.text
  property color accent: Commons.Color.accent
  property string fontFamily: Style.font.family

  signal toggled()

  spacing: Style.space(3)

  Item {
    id: line
    width: parent.width
    height: Math.max(labelRow.implicitHeight, toggle.implicitHeight)

    Row {
      id: labelRow
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(6)

      BrandMark {
        id: brand
        visible: root.showIcon
        anchors.verticalCenter: parent.verticalCenter
        icon: root.icon
        iconSize: Style.space(14)
        tintColor: root.foreground
        fontFamily: root.fontFamily
      }

      PlainText {
        anchors.verticalCenter: parent.verticalCenter
        width: root.wrapLabel
          ? Math.max(0, line.width - toggle.width - Style.space(8) - (brand.visible ? brand.width + labelRow.spacing : 0))
          : implicitWidth
        text: root.label
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        wrapMode: root.wrapLabel ? Text.WordWrap : Text.NoWrap
      }
    }

    ToggleSwitch {
      id: toggle
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      checked: root.checked
      foreground: root.foreground
      accent: root.accent
      onToggled: root.toggled()
    }
  }

  PlainText {
    visible: root.description !== ""
    width: parent.width
    text: root.description
    color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.62)
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
    wrapMode: Text.WordWrap
  }
}
