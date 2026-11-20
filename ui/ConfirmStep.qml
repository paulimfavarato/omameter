import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Two-step destructive action, inline: one button (step 1) that turns into a
// confirm button plus Cancel (step 2). It holds no state: the owner keeps
// `confirming` (Navigation.js for Remove Account / Remove Hub, the tab for a
// limit reset) and reacts to the three signals.
//
// The callers' layouts differ, so these are parameters:
//   actionVisible       step 1 may be hidden (a reset that cannot be used now)
//   destructiveAction   step 1 painted in the danger colour (Remove) or the normal one (Use)
//   cancelFirst         step 2 order: [Cancel, Confirm] (true) or [Confirm, Cancel] (false)
//   cancelHorizontalPadding  Cancel's own padding when it differs from the other two
Row {
  id: root

  property bool confirming: false
  property bool actionVisible: true
  property string actionText: ""
  property string confirmText: actionText
  property string cancelText: ""
  property bool destructiveAction: true
  property bool cancelFirst: true

  property color foreground: Commons.Color.popups.text
  property color accent: Commons.Color.accent
  // Text/border colour of the destructive buttons and their accent.
  property color dangerColor: Commons.Color.urgent
  property color dangerAccent: Commons.Color.urgent
  property string fontFamily: Style.font.family
  property real fontSize: Style.font.bodySmall
  property real horizontalPadding: Style.spacing.controlPaddingX
  property real verticalPadding: Style.spacing.controlPaddingY
  property real cancelHorizontalPadding: horizontalPadding

  signal actionClicked()
  signal confirmed()
  signal cancelled()

  spacing: Style.space(6)

  // Step 1
  Button {
    visible: !root.confirming && root.actionVisible
    text: root.actionText
    bordered: true
    foreground: root.destructiveAction ? root.dangerColor : root.foreground
    accent: root.destructiveAction ? root.dangerAccent : root.accent
    fontFamily: root.fontFamily
    fontSize: root.fontSize
    horizontalPadding: root.horizontalPadding
    verticalPadding: root.verticalPadding
    onClicked: root.actionClicked()
  }

  // Step 2: Cancel before Confirm
  Button {
    visible: root.confirming && root.cancelFirst
    text: root.cancelText
    bordered: true
    foreground: root.foreground
    accent: root.accent
    fontFamily: root.fontFamily
    fontSize: root.fontSize
    horizontalPadding: root.cancelHorizontalPadding
    verticalPadding: root.verticalPadding
    onClicked: root.cancelled()
  }

  Button {
    visible: root.confirming
    text: root.confirmText
    bordered: true
    selected: true
    foreground: root.dangerColor
    accent: root.dangerAccent
    fontFamily: root.fontFamily
    fontSize: root.fontSize
    horizontalPadding: root.horizontalPadding
    verticalPadding: root.verticalPadding
    onClicked: root.confirmed()
  }

  // Step 2: Cancel after Confirm
  Button {
    visible: root.confirming && !root.cancelFirst
    text: root.cancelText
    bordered: true
    foreground: root.foreground
    accent: root.accent
    fontFamily: root.fontFamily
    fontSize: root.fontSize
    horizontalPadding: root.cancelHorizontalPadding
    verticalPadding: root.verticalPadding
    onClicked: root.cancelled()
  }
}
