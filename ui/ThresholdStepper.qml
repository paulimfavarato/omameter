import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Settings.js" as Settings

// One threshold row in Settings -> Alerts: colored dot + label on the left,
// "− value% +" on the right. Bounds and step come from the settings schema
// (Settings.field / Settings.stepThreshold); the caller writes the new value.
Item {
  id: root

  property string settingKey: ""
  property string label: ""
  property int value: 0
  property color tone: Commons.Color.accent
  property color foreground: Commons.Color.popups.text
  property color accent: Commons.Color.accent
  property string fontFamily: Style.font.family

  readonly property var field: Settings.field(settingKey)
  readonly property int minimum: field ? field.min : value
  readonly property int maximum: field ? field.max : value

  // New value after one click, already snapped and clamped by the schema.
  signal stepped(int value)

  height: Math.max(labelRow.implicitHeight, stepperRow.implicitHeight)

  Row {
    id: labelRow
    anchors.left: parent.left
    anchors.verticalCenter: parent.verticalCenter
    spacing: Style.space(6)

    Rectangle {
      width: Style.space(8)
      height: Style.space(8)
      radius: width / 2
      anchors.verticalCenter: parent.verticalCenter
      color: root.tone
    }

    PlainText {
      anchors.verticalCenter: parent.verticalCenter
      text: root.label
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
    }
  }

  Row {
    id: stepperRow
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    spacing: Style.space(6)

    Button {
      text: "−"
      bordered: true
      enabled: root.value > root.minimum
      foreground: root.foreground
      accent: root.accent
      fontFamily: root.fontFamily
      fontSize: Style.font.bodySmall
      horizontalPadding: Style.space(8)
      verticalPadding: Style.space(2)
      onClicked: root.stepped(Settings.stepThreshold(root.settingKey, root.value, -1))
    }

    PlainText {
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(36)
      horizontalAlignment: Text.AlignHCenter
      text: root.value + "%"
      color: root.tone
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      font.bold: true
    }

    Button {
      text: "+"
      bordered: true
      enabled: root.value < root.maximum
      foreground: root.foreground
      accent: root.accent
      fontFamily: root.fontFamily
      fontSize: Style.font.bodySmall
      horizontalPadding: Style.space(8)
      verticalPadding: Style.space(2)
      onClicked: root.stepped(Settings.stepThreshold(root.settingKey, root.value, 1))
    }
  }
}
