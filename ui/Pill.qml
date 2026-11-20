import QtQuick
import qs.Commons
import qs.Commons as Commons

// A small rounded label (plan, hub host, "Added", "Stale data", "Expiring", +N…).
// The copies this replaced disagree on padding, radius and font, so every knob
// is a parameter and each caller passes what it had:
//   paddingX / paddingY  total extra width / height around the text
//   radiusCap            corner radius = min(Style.cornerRadius, radiusCap), 0 when the theme is square
//   color, border.*      the Rectangle's own fill and border (no border unless the caller sets one)
// Callers pass display-safe text (Model.cleanText for external strings).
Rectangle {
  id: root

  property string text: ""
  property color textColor: Commons.Color.popups.text
  property string fontFamily: Style.font.family
  property real fontSize: Style.font.caption
  property bool bold: false
  property real paddingX: Style.space(8)
  property real paddingY: Style.space(3)
  property real radiusCap: Style.space(4)

  radius: Style.cornerRadius > 0 ? Math.min(Style.cornerRadius, radiusCap) : 0
  border.width: 0
  implicitWidth: label.implicitWidth + paddingX
  implicitHeight: label.implicitHeight + paddingY

  PlainText {
    id: label
    anchors.centerIn: parent
    text: root.text
    color: root.textColor
    font.family: root.fontFamily
    font.pixelSize: root.fontSize
    font.bold: root.bold
  }
}
