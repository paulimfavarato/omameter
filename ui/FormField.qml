import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// A form input: optional caption label, the Omarchy TextField, an optional inline
// submit button to its right and an optional error line underneath.
//
// The caller owns the value (Navigation.js drafts): bind `text`, write it back in
// `onEdited(value)`, submit in `onAccepted` (Enter) / `onButtonClicked`.
// `fieldFocused` mirrors the TextField's activeFocus (the view's `inputFocused`
// reads it so the panel's keyboard shortcuts stay off while typing).
//
// The copies this replaced differ in label opacity, label wrapping, spacing and
// the field's vertical padding, so those are parameters with the most common value
// as default. The TextField keeps the kit's own font, as before.
Column {
  id: root

  property string label: ""
  property real labelOpacity: 0.78
  property bool wrapLabel: false
  property string text: ""
  property string placeholderText: ""
  property bool password: false
  property real fieldVerticalPadding: Style.spacing.inputPaddingY
  // Inline submit button; none when buttonText is "".
  property string buttonText: ""
  property bool buttonSelected: false
  property bool buttonEnabled: true
  property string errorText: ""
  property color foreground: Commons.Color.popups.text
  property color accent: Commons.Color.accent
  property color errorColor: Commons.Color.urgent
  property string fontFamily: Style.font.family

  readonly property alias fieldFocused: input.activeFocus

  signal edited(string value)
  signal accepted()
  signal buttonClicked()

  spacing: Style.space(4)

  PlainText {
    visible: root.label !== ""
    width: root.wrapLabel ? parent.width : implicitWidth
    text: root.label
    color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, root.labelOpacity)
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
    wrapMode: root.wrapLabel ? Text.WordWrap : Text.NoWrap
  }

  Row {
    width: parent.width
    spacing: Style.space(6)

    TextField {
      id: input
      width: submitBtn.visible
        ? Math.max(120, parent.width - submitBtn.implicitWidth - Style.space(6))
        : parent.width
      verticalPadding: root.fieldVerticalPadding
      password: root.password
      placeholderText: root.placeholderText
      text: root.text
      foreground: root.foreground
      accent: root.accent
      onTextEdited: root.edited(text)
      onAccepted: root.accepted()
    }

    Button {
      id: submitBtn
      visible: root.buttonText !== ""
      text: root.buttonText
      bordered: true
      selected: root.buttonSelected
      enabled: root.buttonEnabled
      foreground: root.foreground
      accent: root.accent
      fontFamily: root.fontFamily
      fontSize: Style.font.bodySmall
      onClicked: root.buttonClicked()
    }
  }

  PlainText {
    visible: text !== ""
    width: parent.width
    text: root.errorText
    color: root.errorColor
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
    wrapMode: Text.WordWrap
  }
}
