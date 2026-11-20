import QtQuick
import qs.Commons
import qs.Commons as Commons

// Safe plain-text component used everywhere in the Omameter UI.
// Explicitly pins textFormat: Text.PlainText so untrusted provider or
// account strings can never be promoted to Qt rich text.
Text {
  textFormat: Text.PlainText
  color: Commons.Color.popups.text
  font.family: Style.font.family
  font.pixelSize: Style.font.body
  renderType: Text.NativeRendering
}
