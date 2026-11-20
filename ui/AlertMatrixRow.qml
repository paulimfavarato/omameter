import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// One row of the Settings -> Alerts "Priority / Notify" matrix: label on the
// left, then two fixed 58px columns with compact switches. A milestone that is
// always in Priority (Warning, Critical) sets `priorityFixed` and shows a dim
// check mark instead of a switch; a notify-only event (Quota Reset) sets
// `notifyOnly` and shows a dim dash. The caller owns both values.
Item {
  id: root

  property string label: ""
  property bool priorityFixed: false
  property bool notifyOnly: false
  property bool priorityChecked: false
  property bool notifyChecked: false
  property color foreground: Commons.Color.popups.text
  property color accent: Commons.Color.accent
  property string fontFamily: Style.font.family

  signal priorityToggled()
  signal notifyToggled()

  height: Style.space(28)

  PlainText {
    anchors.left: parent.left
    anchors.verticalCenter: parent.verticalCenter
    text: root.label
    color: root.foreground
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
  }

  Item {
    anchors.right: notifyCell.left
    anchors.rightMargin: Style.space(12)
    anchors.verticalCenter: parent.verticalCenter
    width: Style.space(58)
    height: parent.height

    PlainText {
      visible: root.priorityFixed || root.notifyOnly
      anchors.verticalCenter: parent.verticalCenter
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      text: root.notifyOnly ? "—" : "✓"
      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, root.notifyOnly ? 0.35 : 0.5)
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
    }

    ToggleSwitch {
      visible: !root.priorityFixed && !root.notifyOnly
      anchors.centerIn: parent
      trackHeight: 18
      checked: root.priorityChecked
      foreground: root.foreground
      accent: root.accent
      onToggled: root.priorityToggled()
    }
  }

  Item {
    id: notifyCell
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    width: Style.space(58)
    height: parent.height

    ToggleSwitch {
      anchors.centerIn: parent
      trackHeight: 18
      checked: root.notifyChecked
      foreground: root.foreground
      accent: root.accent
      onToggled: root.notifyToggled()
    }
  }
}
