import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Section header + Omarchy ButtonGroup: the "pick one of N" settings block
// (Default Tab, Show Usage As, Reset Times, Time Format). `options` is the
// ButtonGroup list ({ value, label }); `chosen(value)` fires on a new pick.
Column {
  id: root

  property string title: ""
  property var options: []
  property string value: ""
  property color foreground: Commons.Color.popups.text
  property color background: Commons.Color.popups.background
  property color accent: Commons.Color.accent
  property string fontFamily: Style.font.family

  signal chosen(string value)

  spacing: Style.space(5)

  PanelSectionHeader {
    text: root.title
    foreground: root.foreground
    fontFamily: root.fontFamily
  }

  ButtonGroup {
    options: root.options
    value: root.value
    foreground: root.foreground
    background: root.background
    accent: root.accent
    fontFamily: root.fontFamily
    fontSize: Style.font.bodySmall
    focusable: false
    onChanged: function(v) { root.chosen(v) }
  }
}
