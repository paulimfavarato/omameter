import QtQuick
import qs.Commons
import qs.Commons as Commons
import "../Model.js" as Model

// One item in the top-bar chip strip:
//   [BrandMark (+ badge)] [window glyph | label] [optional progress meter] [value]
// Supports both normal single-row sizing and compact 2-row stacked sizing.
Row {
  id: root

  property var item: ({})
  property var palette: ({})
  property color foreground: Commons.Color.popups.text
  property color urgent: Commons.Color.urgent
  property color badgeBackground: Commons.Color.background
  property string fontFamily: Style.font.family
  property string barLabelStyle: "full"
  property string barMeterStyle: "none"
  property bool useProviderColors: false
  property bool compact: false
  property real rowHeight: 0

  readonly property var chipDisplay: Model.barChipDisplay(item, barLabelStyle)
  readonly property bool isAlert: !!(item && (item.severity === "critical" || item.severity === "warning"))
  readonly property color accentTint: Model.barItemColor(item, {
    palette: palette,
    foreground: foreground,
    urgent: urgent,
    useProviderColors: useProviderColors
  })
  readonly property color textTint: isAlert ? accentTint : foreground

  readonly property bool isBounded: !!(item && item.isBounded)
  readonly property real fillFraction: (item && isFinite(Number(item.fillFraction)))
    ? Model.clamp(Number(item.fillFraction), 0, 1)
    : 0
  readonly property bool hasPaceMark: !!(item
    && item.paceMark !== null
    && item.paceMark !== undefined
    && item.paceMark !== ""
    && isFinite(Number(item.paceMark)))
  readonly property real paceFraction: hasPaceMark ? Model.clamp(Number(item.paceMark), 0, 1) : 0

  readonly property bool showGraphicalBar: isBounded && barMeterStyle === "bar"
  readonly property string textMeter: isBounded
    ? Model.formatBarMeter(fillFraction, barMeterStyle, compact ? 5 : 6)
    : ""

  readonly property real targetRowH: rowHeight > 0
    ? rowHeight
    : Math.max(10, Math.floor((Style.bar.sizeHorizontal - 2) / 2))
  readonly property real iconPx: compact
    ? Math.max(9, Math.min(Style.bar.iconSize - 2, Math.floor(targetRowH * 0.82)))
    : Style.bar.iconSize
  readonly property real fontPx: compact
    ? Math.max(8, Math.min(Style.font.bodySmall - 2, Math.floor(targetRowH * 0.78)))
    : Style.font.bodySmall
  readonly property real glyphPx: Math.max(8, fontPx - 1)
  readonly property real meterWidth: compact ? Style.space(26) : Style.space(34)
  readonly property real meterHeight: compact ? Math.max(3, Style.space(3)) : Math.max(4, Style.space(5))

  spacing: compact
    ? Style.space(2)
    : (barLabelStyle === "full" ? Style.space(4) : Style.space(3))

  BrandMark {
    anchors.verticalCenter: parent.verticalCenter
    icon: (root.item && root.item.icon) ? String(root.item.icon) : "omameter.svg"
    iconSize: root.iconPx
    tintColor: root.accentTint
    badgeBackground: root.badgeBackground
    fontFamily: root.fontFamily
    badge: (root.item && root.item.badge) ? String(root.item.badge) : ""
    compactBadge: true
  }

  PlainText {
    visible: root.chipDisplay.glyph !== ""
    anchors.verticalCenter: parent.verticalCenter
    text: root.chipDisplay.glyph
    color: Qt.rgba(root.textTint.r, root.textTint.g, root.textTint.b, 0.72)
    font.family: root.fontFamily
    font.pixelSize: root.glyphPx
  }

  PlainText {
    visible: root.chipDisplay.label !== ""
    anchors.verticalCenter: parent.verticalCenter
    text: root.chipDisplay.label
    color: Qt.rgba(root.textTint.r, root.textTint.g, root.textTint.b, root.barLabelStyle === "compact" ? 0.72 : 0.82)
    font.family: root.fontFamily
    font.pixelSize: root.fontPx
  }

  // Native graphical mini progress bar ("bar")
  Item {
    visible: root.showGraphicalBar
    anchors.verticalCenter: parent.verticalCenter
    width: root.meterWidth
    height: root.meterHeight

    Rectangle {
      id: barTrack
      anchors.fill: parent
      radius: Style.cornerRadius > 0 ? Math.min(height / 2, Style.cornerRadius) : 0
      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.18)

      Rectangle {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: {
          var f = root.fillFraction
          if (f <= 0.005) return 0
          return Math.max(Math.min(parent.width, Math.max(2, height)), Math.round(parent.width * f))
        }
        radius: parent.radius
        color: root.accentTint

        Behavior on width {
          NumberAnimation {
            duration: Style.duration(180)
            easing.type: Easing.OutCubic
          }
        }
      }

      Rectangle {
        visible: root.hasPaceMark && root.paceFraction > 0.02 && root.paceFraction < 0.98
        x: Math.max(0, Math.min(parent.width - width, Math.round(parent.width * root.paceFraction)))
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: 1
        color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.72)
      }
    }
  }

  // Unicode sub-block / braille progress meter ("blocks" | "braille")
  PlainText {
    visible: root.textMeter !== ""
    anchors.verticalCenter: parent.verticalCenter
    text: root.textMeter
    color: root.accentTint
    font.family: root.fontFamily
    font.pixelSize: Math.max(8, root.fontPx - (root.compact ? 0 : 1))
  }

  PlainText {
    visible: !!(root.item && root.item.value && String(root.item.value) !== "")
    anchors.verticalCenter: parent.verticalCenter
    text: (root.item && root.item.value) ? String(root.item.value) : ""
    color: root.textTint
    font.family: root.fontFamily
    font.pixelSize: root.fontPx
    font.bold: root.isAlert
  }
}
