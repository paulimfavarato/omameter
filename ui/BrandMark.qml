import QtQuick
import QtQuick.Effects
import qs.Commons
import qs.Commons as Commons

// Renders a provider SVG icon tinted via MultiEffect, with optional
// CLIProxyAPI corner badge and optional multi-account initials pill.
Row {
  id: root

  property string icon: "omameter.svg"
  property string fallback: "󰚩"
  property color tintColor: Commons.Color.popups.text
  property color badgeBackground: Commons.Color.popups.background
  property string fontFamily: Style.font.family
  property real iconSize: Style.font.body
  property bool isHub: false
  property string badge: ""
  property bool compactBadge: false

  spacing: badge !== "" ? Style.space(compactBadge ? 3 : 4) : 0

  Item {
    id: iconBox
    width: Math.max(10, Math.round(root.iconSize))
    height: Math.max(10, Math.round(root.iconSize))
    anchors.verticalCenter: parent.verticalCenter

    Image {
      id: artwork
      anchors.fill: parent
      visible: false
      source: root.icon !== "" ? Qt.resolvedUrl("../icons/" + root.icon) : ""
      fillMode: Image.PreserveAspectFit
      sourceSize.width: Math.max(16, Math.round(iconBox.width * 2))
      sourceSize.height: Math.max(16, Math.round(iconBox.height * 2))
      cache: true
      smooth: true
    }

    MultiEffect {
      anchors.fill: artwork
      source: artwork
      visible: root.icon !== "" && artwork.status === Image.Ready
      colorization: 1.0
      colorizationColor: root.tintColor
    }

    PlainText {
      visible: root.icon === "" || artwork.status !== Image.Ready
      anchors.centerIn: parent
      text: root.fallback
      color: root.tintColor
      font.family: root.fontFamily
      font.pixelSize: root.iconSize
    }

    // Mini CLIProxyAPI corner mark for hub-managed logins
    Rectangle {
      visible: root.isHub
      readonly property real miniSize: Math.max(7, Math.round(iconBox.width * 0.52))
      width: miniSize
      height: miniSize
      radius: Math.round(miniSize / 2)
      anchors.right: parent.right
      anchors.bottom: parent.bottom
      anchors.rightMargin: -Math.round(miniSize * 0.22)
      anchors.bottomMargin: -Math.round(miniSize * 0.22)
      color: root.badgeBackground
      border.width: 1
      border.color: Qt.rgba(root.tintColor.r, root.tintColor.g, root.tintColor.b, 0.35)

      Image {
        id: hubArt
        anchors.fill: parent
        anchors.margins: 1
        visible: false
        source: root.isHub ? Qt.resolvedUrl("../icons/cliproxyapi.svg") : ""
        fillMode: Image.PreserveAspectFit
        sourceSize.width: 16
        sourceSize.height: 16
        cache: true
      }

      MultiEffect {
        anchors.fill: hubArt
        source: hubArt
        visible: root.isHub && hubArt.status === Image.Ready
        colorization: 1.0
        colorizationColor: root.tintColor
      }
    }
  }

  // Optional multi-account initials pill (e.g. "PF", "W")
  Rectangle {
    visible: root.badge !== ""
    anchors.verticalCenter: parent.verticalCenter
    radius: Style.cornerRadius > 0 ? Math.min(Style.cornerRadius, Style.space(4)) : 0
    color: Qt.rgba(root.tintColor.r, root.tintColor.g, root.tintColor.b, 0.16)
    border.width: 1
    border.color: Qt.rgba(root.tintColor.r, root.tintColor.g, root.tintColor.b, 0.3)
    implicitWidth: badgeText.implicitWidth + Style.space(root.compactBadge ? 4 : 6)
    implicitHeight: badgeText.implicitHeight + Style.space(root.compactBadge ? 1 : 2)

    PlainText {
      id: badgeText
      anchors.centerIn: parent
      text: root.badge
      color: root.tintColor
      font.family: root.fontFamily
      font.pixelSize: Math.max(8, Math.round(root.compactBadge ? Style.font.caption - 1 : Style.font.caption))
      font.bold: true
    }
  }
}
