import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// Rich hover summary card for the top-bar widget:
// grouped by provider with tinted SVG icons, mini progress bars,
// and vertically aligned metric/value/time columns.
PopupCard {
  id: root

  property var data: ({ state: "empty", title: "Omameter", modeTag: "", nextText: "", message: "", groups: [], notices: [] })
  property var palette: ({})
  property color foreground: Commons.Color.popups.text
  property color background: Commons.Color.popups.background
  property color accent: Commons.Color.accent
  property color urgent: Commons.Color.urgent
  property string fontFamily: Style.font.family

  readonly property color warningColor: palette && palette.warning ? palette.warning : root.accent
  readonly property color goodColor: palette && palette.good ? palette.good : root.accent
  readonly property string cardState: data && data.state ? String(data.state) : "empty"
  readonly property real desiredWidth: cardState === "metrics"
    ? Style.space(332)
    : (cardState === "notices" ? Style.space(300) : Style.space(248))

  triggerMode: "hover"
  padding: Style.space(12)
  contentWidth: root.fittedContentWidth(root.desiredWidth)
  contentHeight: root.fittedContentHeight(body.implicitHeight)

  Column {
    id: body
    width: parent.width
    spacing: Style.space(8)

    // Header: [Omameter icon] Omameter [Used/Left]           Next 3m
    Item {
      width: parent.width
      height: Math.max(headerLeft.implicitHeight, headerRight.implicitHeight)

      Row {
        id: headerLeft
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(6)

        BrandMark {
          anchors.verticalCenter: parent.verticalCenter
          icon: "omameter.svg"
          iconSize: Style.space(13)
          tintColor: root.accent
          badgeBackground: root.background
          fontFamily: root.fontFamily
        }

        PlainText {
          anchors.verticalCenter: parent.verticalCenter
          text: root.data && root.data.title ? String(root.data.title) : "Omameter"
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.bold: true
        }

        Pill {
          visible: !!(root.data && root.data.modeTag)
          anchors.verticalCenter: parent.verticalCenter
          paddingX: Style.space(5)
          paddingY: Style.space(1)
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.1)
          text: root.data && root.data.modeTag ? String(root.data.modeTag) : ""
          textColor: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.72)
          fontFamily: root.fontFamily
          fontSize: Math.max(9, Style.font.caption - 1)
        }
      }

      PlainText {
        id: headerRight
        visible: !!(root.data && root.data.nextText)
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        text: root.data && root.data.nextText ? String(root.data.nextText) : ""
        color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.56)
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
    }

    PanelSeparator {
      foreground: root.foreground
    }

    // Grouped metrics table
    Column {
      visible: root.cardState === "metrics"
      width: parent.width
      spacing: Style.space(9)

      Repeater {
        model: (root.data && root.data.groups) ? root.data.groups : []

        delegate: Column {
          id: groupCol
          required property var modelData
          readonly property var groupRows: (modelData && modelData.rows) ? modelData.rows : []
          width: parent.width
          spacing: Style.space(4)

          // Provider group header
          Row {
            width: parent.width
            spacing: Style.space(6)

            BrandMark {
              anchors.verticalCenter: parent.verticalCenter
              icon: groupCol.modelData.icon || "omameter.svg"
              iconSize: Style.space(13)
              tintColor: root.foreground
              badgeBackground: root.background
              fontFamily: root.fontFamily
              badge: groupCol.modelData.badge ? String(groupCol.modelData.badge) : ""
              isHub: !!groupCol.modelData.isHub
              compactBadge: true
            }

            PlainText {
              anchors.verticalCenter: parent.verticalCenter
              text: groupCol.modelData.providerName || ""
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
              elide: Text.ElideRight
            }
          }

          // Aligned rows under this provider
          Repeater {
            model: groupCol.groupRows

            delegate: Row {
              id: metricRow
              required property var modelData
              readonly property color rowColor: modelData.severity === "normal"
                ? root.accent
                : (modelData.color || root.foreground)
              readonly property real colLabelW: Style.space(82)
              readonly property real colBarW: Style.space(52)
              readonly property real colValW: Style.space(38)
              readonly property real colIndentW: Style.space(12)
              readonly property real colTimeW: Math.max(
                Style.space(72),
                width - colIndentW - colLabelW - colBarW - colValW - spacing * 4
              )

              width: parent.width
              spacing: Style.space(7)

              // Indent aligns metric label directly beneath provider name
              Item {
                width: metricRow.colIndentW
                height: 1
                anchors.verticalCenter: parent.verticalCenter
              }

              // Col 1: Metric window label
              PlainText {
                width: metricRow.colLabelW
                anchors.verticalCenter: parent.verticalCenter
                text: metricRow.modelData.label || ""
                color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.78)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }

              // Col 2: Mini progress bar
              Item {
                width: metricRow.colBarW
                height: Style.space(4)
                anchors.verticalCenter: parent.verticalCenter

                Rectangle {
                  visible: !!metricRow.modelData.isBounded
                  anchors.fill: parent
                  radius: Style.cornerRadius > 0 ? Math.min(height / 2, Style.cornerRadius) : 0
                  color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.14)

                  Rectangle {
                    anchors.left: parent.left
                    anchors.top: parent.top
                    anchors.bottom: parent.bottom
                    width: {
                      var f = Number(metricRow.modelData.fillFraction) || 0
                      return Math.max(f > 0.005 ? Math.min(parent.width, height) : 0, Math.round(parent.width * f))
                    }
                    radius: parent.radius
                    color: metricRow.rowColor
                  }
                }
              }

              // Col 3: Right-aligned percentage / value
              PlainText {
                width: metricRow.colValW
                anchors.verticalCenter: parent.verticalCenter
                horizontalAlignment: Text.AlignRight
                text: metricRow.modelData.value || ""
                color: metricRow.modelData.severity !== "normal" ? metricRow.rowColor : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
              }

              // Col 4: Right-aligned compact reset / burn countdown
              Item {
                width: metricRow.colTimeW
                height: Math.max(timeRow.implicitHeight, Style.font.caption)
                anchors.verticalCenter: parent.verticalCenter

                Row {
                  id: timeRow
                  visible: !!(metricRow.modelData.timeText && String(metricRow.modelData.timeText) !== "")
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(3)

                  PlainText {
                    visible: !!(metricRow.modelData.timeGlyph && String(metricRow.modelData.timeGlyph) !== "")
                    anchors.verticalCenter: parent.verticalCenter
                    text: metricRow.modelData.timeGlyph || ""
                    color: metricRow.modelData.isBurning
                      ? root.warningColor
                      : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.5)
                    font.family: root.fontFamily
                    font.pixelSize: Math.max(9, Style.font.caption - 1)
                  }

                  PlainText {
                    anchors.verticalCenter: parent.verticalCenter
                    text: metricRow.modelData.timeText || ""
                    color: metricRow.modelData.isBurning
                      ? root.warningColor
                      : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.68)
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: !!metricRow.modelData.isBurning
                  }
                }
              }
            }
          }
        }
      }
    }

    // Notices state (when no metrics are listed, only provider notices/errors)
    Column {
      visible: root.cardState === "notices"
      width: parent.width
      spacing: Style.space(6)

      Repeater {
        model: (root.data && root.data.notices) ? root.data.notices : []

        delegate: Row {
          id: noticeRow
          required property var modelData
          width: parent.width
          spacing: Style.space(6)

          BrandMark {
            anchors.verticalCenter: parent.verticalCenter
            icon: noticeRow.modelData.icon || "omameter.svg"
            iconSize: Style.space(13)
            tintColor: root.urgent
            badgeBackground: root.background
            fontFamily: root.fontFamily
          }

          PlainText {
            width: parent.width - Style.space(19)
            anchors.verticalCenter: parent.verticalCenter
            text: (noticeRow.modelData.providerName ? (noticeRow.modelData.providerName + ": ") : "") + (noticeRow.modelData.message || "")
            color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.85)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
        }
      }
    }

    // All Clear / Privacy / Empty / Summary single-row state
    Row {
      visible: root.cardState !== "metrics" && root.cardState !== "notices"
      width: parent.width
      spacing: Style.space(6)

      PlainText {
        visible: root.cardState === "clear" || root.cardState === "privacy"
        anchors.verticalCenter: parent.verticalCenter
        text: root.cardState === "clear" ? "󰄬" : "󰈉"
        color: root.cardState === "clear" ? root.goodColor : root.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      PlainText {
        width: parent.width - (root.cardState === "clear" || root.cardState === "privacy" ? Style.space(20) : 0)
        anchors.verticalCenter: parent.verticalCenter
        text: root.data && root.data.message ? String(root.data.message) : ""
        color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.82)
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }
    }
  }
}
