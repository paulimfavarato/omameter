import QtQuick

// Stacked bar chart (Qt Quick Canvas) for hourly/daily token & API-equivalent cost trends.
// Supports hover tooltip, click-to-filter hour/bucket, and live Omarchy theme colors.
Item {
  id: root

  property var series: []              // [{ ts, label, totalTokens, totalCost, byProvider: { id: { tokens, cost } } }]
  property string metricMode: "cost"   // "cost" | "tokens"
  property string granularity: "hourly"
  property var selectedHourStart: null
  property color foreground: "#e5e9f0"
  property color background: "#1b1f27"
  property color accent: "#88c0d0"
  property var providerColors: ({
    claude: "#d97757",
    codex: "#10a37f",
    antigravity: "#5e81ac",
    opencode: "#b48ead"
  })
  property var providerOrder: ["claude", "codex", "antigravity", "opencode"]
  property var providerNames: ({
    claude: "Claude Code",
    codex: "GPT Codex",
    antigravity: "Antigravity",
    opencode: "OpenCode"
  })
  property string fontFamily: "monospace"
  property string emptyText: "No data in this window"
  property int cornerRadius: 0

  signal bucketClicked(var bucket)

  property int hoveredIndex: -1
  property real hoverX: 0
  property real hoverY: 0

  readonly property real padLeft: 54
  readonly property real padRight: 14
  readonly property real padTop: 14
  readonly property real padBottom: 26

  function _bucketVal(b) {
    if (!b) return 0
    return metricMode === "tokens" ? Number(b.totalTokens || 0) : Number(b.totalCost || 0)
  }

  function _provVal(b, pid) {
    if (!b || !b.byProvider || !b.byProvider[pid]) return 0
    var entry = b.byProvider[pid]
    return metricMode === "tokens" ? Number(entry.tokens || 0) : Number(entry.cost || 0)
  }

  function formatAxisValue(v) {
    var n = Number(v || 0)
    if (metricMode === "tokens") {
      if (n >= 1000000000) return (n / 1000000000).toFixed(1) + "B"
      if (n >= 1000000) return (n / 1000000).toFixed(1) + "M"
      if (n >= 1000) return (n / 1000).toFixed(n >= 100000 ? 0 : 1) + "k"
      return String(Math.round(n))
    }
    if (n >= 100) return "$" + n.toFixed(0)
    if (n >= 10) return "$" + n.toFixed(1)
    if (n >= 1) return "$" + n.toFixed(2)
    if (n > 0) return "$" + n.toFixed(3)
    return "$0"
  }

  function formatDetailValue(v) {
    var n = Number(v || 0)
    if (metricMode === "tokens") {
      if (n >= 1000000000) return (n / 1000000000).toFixed(2) + "B tok"
      if (n >= 1000000) return (n / 1000000).toFixed(2) + "M tok"
      if (n >= 1000) return (n / 1000).toFixed(1) + "k tok"
      return String(Math.round(n)) + " tok"
    }
    if (n >= 100) return "$" + n.toFixed(2)
    if (n >= 1) return "$" + n.toFixed(2)
    if (n > 0) return "$" + n.toFixed(4)
    return "$0.00"
  }

  function _niceMax(rawMax) {
    if (!(rawMax > 0)) return 1
    var exp = Math.floor(Math.log(rawMax) / Math.LN10)
    var base = Math.pow(10, exp)
    var frac = rawMax / base
    var nice = 1
    if (frac <= 1) nice = 1
    else if (frac <= 2) nice = 2
    else if (frac <= 5) nice = 5
    else nice = 10
    return nice * base
  }

  function _maxValue() {
    var arr = Array.isArray(series) ? series : []
    var m = 0
    for (var i = 0; i < arr.length; i++) {
      var v = _bucketVal(arr[i])
      if (v > m) m = v
    }
    return _niceMax(m)
  }

  function _hasAnyData() {
    var arr = Array.isArray(series) ? series : []
    for (var i = 0; i < arr.length; i++) {
      if (_bucketVal(arr[i]) > 0) return true
    }
    return false
  }

  onSeriesChanged: chartCanvas.requestPaint()
  onMetricModeChanged: chartCanvas.requestPaint()
  onSelectedHourStartChanged: chartCanvas.requestPaint()
  onHoveredIndexChanged: chartCanvas.requestPaint()
  onForegroundChanged: chartCanvas.requestPaint()
  onAccentChanged: chartCanvas.requestPaint()
  onWidthChanged: chartCanvas.requestPaint()
  onHeightChanged: chartCanvas.requestPaint()

  Canvas {
    id: chartCanvas
    anchors.fill: parent
    antialiasing: true

    onPaint: {
      var ctx = getContext("2d")
      var w = width
      var h = height
      ctx.clearRect(0, 0, w, h)
      if (w < 40 || h < 40) return

      var arr = Array.isArray(root.series) ? root.series : []
      var plotX = root.padLeft
      var plotY = root.padTop
      var plotW = Math.max(10, w - root.padLeft - root.padRight)
      var plotH = Math.max(10, h - root.padTop - root.padBottom)
      var maxV = root._maxValue()

      // Horizontal gridlines & Y ticks
      var ticks = 4
      ctx.font = "10px \"" + root.fontFamily + "\""
      ctx.textBaseline = "middle"
      ctx.textAlign = "right"
      for (var t = 0; t <= ticks; t++) {
        var ratio = t / ticks
        var gy = Math.round(plotY + plotH - ratio * plotH) + 0.5
        ctx.strokeStyle = Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, t === 0 ? 0.22 : 0.09)
        ctx.lineWidth = 1
        ctx.beginPath()
        ctx.moveTo(plotX, gy)
        ctx.lineTo(plotX + plotW, gy)
        ctx.stroke()

        ctx.fillStyle = Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.55)
        ctx.fillText(root.formatAxisValue(maxV * ratio), plotX - 8, gy)
      }

      if (arr.length === 0) return

      var slotW = plotW / arr.length
      var barW = Math.max(3, Math.min(28, slotW * 0.68))
      var labelEvery = Math.max(1, Math.ceil(arr.length / Math.max(4, Math.floor(plotW / 56))))

      for (var i = 0; i < arr.length; i++) {
        var b = arr[i]
        var cx = plotX + i * slotW + slotW / 2
        var bx = Math.round(cx - barW / 2)
        var isHovered = (i === root.hoveredIndex)
        var isSelected = (root.selectedHourStart !== null && Number(b.ts) === Number(root.selectedHourStart))

        if (isHovered || isSelected) {
          ctx.fillStyle = isSelected
            ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.16)
            : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.07)
          ctx.fillRect(plotX + i * slotW + 1, plotY, Math.max(2, slotW - 2), plotH)
        }

        // Stacked segments per provider
        var yCursor = plotY + plotH
        for (var p = 0; p < root.providerOrder.length; p++) {
          var pid = root.providerOrder[p]
          var pv = root._provVal(b, pid)
          if (!(pv > 0)) continue
          var segH = Math.max(1.5, (pv / maxV) * plotH)
          var segY = Math.max(plotY, yCursor - segH)
          var drawH = yCursor - segY
          if (drawH <= 0) continue

          var col = root.providerColors[pid] || root.accent
          ctx.fillStyle = col
          ctx.globalAlpha = (root.hoveredIndex >= 0 && !isHovered) ? 0.65 : 0.94
          ctx.fillRect(bx, segY, barW, drawH)
          ctx.globalAlpha = 1.0
          yCursor = segY
        }

        if (isSelected) {
          ctx.strokeStyle = root.accent
          ctx.lineWidth = 1.5
          ctx.strokeRect(bx - 0.5, yCursor - 0.5, barW + 1, (plotY + plotH) - yCursor + 1)
        }

        // X-axis labels
        if (i % labelEvery === 0 || i === arr.length - 1) {
          ctx.fillStyle = isHovered || isSelected
            ? root.foreground
            : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.55)
          ctx.textAlign = "center"
          ctx.textBaseline = "top"
          ctx.fillText(String(b.label || ""), cx, plotY + plotH + 7)
        }
      }
    }
  }

  PlainText {
    visible: !root._hasAnyData()
    anchors.centerIn: parent
    text: root.emptyText
    color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.45)
    font.family: root.fontFamily
    font.pixelSize: 12
  }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: root.hoveredIndex >= 0 && root.granularity === "hourly" ? Qt.PointingHandCursor : Qt.ArrowCursor

    function _indexForX(mx) {
      var arr = Array.isArray(root.series) ? root.series : []
      if (arr.length === 0) return -1
      var plotX = root.padLeft
      var plotW = Math.max(10, root.width - root.padLeft - root.padRight)
      if (mx < plotX || mx > plotX + plotW) return -1
      var idx = Math.floor(((mx - plotX) / plotW) * arr.length)
      return Math.max(0, Math.min(arr.length - 1, idx))
    }

    onPositionChanged: function(mouse) {
      root.hoveredIndex = _indexForX(mouse.x)
      root.hoverX = mouse.x
      root.hoverY = mouse.y
    }
    onExited: root.hoveredIndex = -1
    onClicked: function(mouse) {
      var idx = _indexForX(mouse.x)
      if (idx >= 0 && idx < root.series.length) {
        root.bucketClicked(root.series[idx])
      }
    }
  }

  // Hover tooltip overlay
  Rectangle {
    id: tooltip
    readonly property var bucket: (root.hoveredIndex >= 0 && root.series && root.hoveredIndex < root.series.length)
      ? root.series[root.hoveredIndex]
      : null
    visible: bucket !== null
    z: 10
    radius: root.cornerRadius
    color: Qt.rgba(root.background.r, root.background.g, root.background.b, 0.97)
    border.width: 1
    border.color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.22)
    width: Math.max(145, tipCol.implicitWidth + 18)
    height: tipCol.implicitHeight + 14
    x: {
      var desired = root.hoverX + 14
      if (desired + width > root.width - 6) desired = root.hoverX - width - 14
      return Math.max(6, Math.min(root.width - width - 6, desired))
    }
    y: Math.max(6, Math.min(root.height - height - 6, root.hoverY - height / 2))

    Column {
      id: tipCol
      anchors.left: parent.left
      anchors.top: parent.top
      anchors.margins: 8
      spacing: 3

      PlainText {
        text: tooltip.bucket ? (String(tooltip.bucket.date || tooltip.bucket.label || "") + (tooltip.bucket.date ? "" : "")) : ""
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: 11
        font.bold: true
      }

      PlainText {
        text: tooltip.bucket
          ? (root.formatDetailValue(root._bucketVal(tooltip.bucket))
             + (root.metricMode === "cost"
                ? ("  ·  " + root.formatAxisValue(Number(tooltip.bucket.totalTokens || 0)) + " tok")
                : ("  ·  $" + Number(tooltip.bucket.totalCost || 0).toFixed(2))))
          : ""
        color: root.accent
        font.family: root.fontFamily
        font.pixelSize: 11
        font.bold: true
      }

      Repeater {
        model: root.providerOrder
        delegate: Row {
          required property string modelData
          readonly property real val: tooltip.bucket ? root._provVal(tooltip.bucket, modelData) : 0
          visible: val > 0
          spacing: 6

          Rectangle {
            width: 7
            height: 7
            radius: 3.5
            anchors.verticalCenter: parent.verticalCenter
            color: root.providerColors[modelData] || root.accent
          }

          PlainText {
            anchors.verticalCenter: parent.verticalCenter
            text: String(root.providerNames[modelData] || modelData) + ": " + root.formatDetailValue(val)
            color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.85)
            font.family: root.fontFamily
            font.pixelSize: 10
          }
        }
      }
    }
  }
}
