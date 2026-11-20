import QtQuick

// Phase 1 Quota Burn-Rate Line Chart (Qt Quick Canvas).
// Plots recorded `quota_samples` (% used 0..100%) with warning/critical threshold lines
// and interactive hover inspection.
Item {
  id: root

  property var points: []             // [{ ts, usedPct, usedAmount, limitAmount, resetsAt }]
  property real warningUsedPct: 70    // 100 - warningThreshold (% left)
  property real criticalUsedPct: 90   // 100 - criticalThreshold (% left)
  property color foreground: "#e5e9f0"
  property color background: "#1b1f27"
  property color accent: "#88c0d0"
  property color warningColor: "#ebcb8b"
  property color criticalColor: "#bf616a"
  property string fontFamily: "monospace"
  property string emptyText: "No quota history recorded yet — samples are captured on every Omameter refresh."
  property int cornerRadius: 0

  property int hoveredIndex: -1
  property real hoverX: 0
  property real hoverY: 0

  readonly property real padLeft: 44
  readonly property real padRight: 16
  readonly property real padTop: 14
  readonly property real padBottom: 26

  function _validPoints() {
    var arr = Array.isArray(points) ? points : []
    var out = []
    for (var i = 0; i < arr.length; i++) {
      var p = arr[i]
      if (p && p.usedPct !== null && p.usedPct !== undefined && isFinite(Number(p.usedPct))) {
        out.push(p)
      }
    }
    return out
  }

  function _formatTs(tsSec) {
    var d = new Date(Number(tsSec || 0) * 1000)
    var mm = String(d.getMonth() + 1).padStart(2, "0")
    var dd = String(d.getDate()).padStart(2, "0")
    var hh = String(d.getHours()).padStart(2, "0")
    var mi = String(d.getMinutes()).padStart(2, "0")
    return mm + "-" + dd + " " + hh + ":" + mi
  }

  onPointsChanged: lineCanvas.requestPaint()
  onWarningUsedPctChanged: lineCanvas.requestPaint()
  onCriticalUsedPctChanged: lineCanvas.requestPaint()
  onHoveredIndexChanged: lineCanvas.requestPaint()
  onForegroundChanged: lineCanvas.requestPaint()
  onAccentChanged: lineCanvas.requestPaint()
  onWidthChanged: lineCanvas.requestPaint()
  onHeightChanged: lineCanvas.requestPaint()

  Canvas {
    id: lineCanvas
    anchors.fill: parent
    antialiasing: true

    onPaint: {
      var ctx = getContext("2d")
      var w = width
      var h = height
      ctx.clearRect(0, 0, w, h)
      if (w < 40 || h < 40) return

      var plotX = root.padLeft
      var plotY = root.padTop
      var plotW = Math.max(10, w - root.padLeft - root.padRight)
      var plotH = Math.max(10, h - root.padTop - root.padBottom)

      // Y grid 0%, 25%, 50%, 75%, 100%
      ctx.font = "10px \"" + root.fontFamily + "\""
      ctx.textBaseline = "middle"
      ctx.textAlign = "right"
      var yMarks = [0, 25, 50, 75, 100]
      for (var i = 0; i < yMarks.length; i++) {
        var pct = yMarks[i]
        var gy = Math.round(plotY + plotH - (pct / 100.0) * plotH) + 0.5
        ctx.strokeStyle = Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, pct === 0 ? 0.22 : 0.09)
        ctx.lineWidth = 1
        ctx.beginPath()
        ctx.moveTo(plotX, gy)
        ctx.lineTo(plotX + plotW, gy)
        ctx.stroke()

        ctx.fillStyle = Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.55)
        ctx.fillText(pct + "%", plotX - 6, gy)
      }

      // Warning & Critical threshold dashed lines
      function drawThreshold(pctVal, col) {
        if (!(pctVal > 0 && pctVal < 100)) return
        var ty = Math.round(plotY + plotH - (pctVal / 100.0) * plotH) + 0.5
        ctx.save()
        ctx.setLineDash([4, 4])
        ctx.strokeStyle = Qt.rgba(col.r, col.g, col.b, 0.55)
        ctx.lineWidth = 1
        ctx.beginPath()
        ctx.moveTo(plotX, ty)
        ctx.lineTo(plotX + plotW, ty)
        ctx.stroke()
        ctx.restore()
      }
      drawThreshold(root.warningUsedPct, root.warningColor)
      drawThreshold(root.criticalUsedPct, root.criticalColor)

      var pts = root._validPoints()
      if (pts.length === 0) return

      var minTs = Number(pts[0].ts)
      var maxTs = Number(pts[pts.length - 1].ts)
      var span = Math.max(60, maxTs - minTs)

      function px(ts) {
        if (pts.length === 1) return plotX + plotW / 2
        return plotX + ((Number(ts) - minTs) / span) * plotW
      }
      function py(pct) {
        var clamped = Math.max(0, Math.min(100, Number(pct)))
        return plotY + plotH - (clamped / 100.0) * plotH
      }

      // Area gradient fill under curve
      if (pts.length >= 2) {
        var grad = ctx.createLinearGradient(0, plotY, 0, plotY + plotH)
        grad.addColorStop(0, Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.28))
        grad.addColorStop(1, Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.02))
        ctx.fillStyle = grad
        ctx.beginPath()
        ctx.moveTo(px(pts[0].ts), plotY + plotH)
        for (var j = 0; j < pts.length; j++) {
          ctx.lineTo(px(pts[j].ts), py(pts[j].usedPct))
        }
        ctx.lineTo(px(pts[pts.length - 1].ts), plotY + plotH)
        ctx.closePath()
        ctx.fill()
      }

      // Main line stroke
      ctx.strokeStyle = root.accent
      ctx.lineWidth = 2
      ctx.beginPath()
      for (var k = 0; k < pts.length; k++) {
        var xk = px(pts[k].ts)
        var yk = py(pts[k].usedPct)
        if (k === 0) ctx.moveTo(xk, yk)
        else ctx.lineTo(xk, yk)
      }
      ctx.stroke()

      // Dots when few points or hovered
      for (var d = 0; d < pts.length; d++) {
        var isH = (d === root.hoveredIndex)
        if (pts.length <= 28 || isH) {
          var xd = px(pts[d].ts)
          var yd = py(pts[d].usedPct)
          if (isH) {
            ctx.strokeStyle = Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.25)
            ctx.lineWidth = 1
            ctx.beginPath()
            ctx.moveTo(xd, plotY)
            ctx.lineTo(xd, plotY + plotH)
            ctx.stroke()
          }
          ctx.fillStyle = isH ? root.foreground : root.accent
          ctx.beginPath()
          ctx.arc(xd, yd, isH ? 4.5 : 2.5, 0, Math.PI * 2)
          ctx.fill()
        }
      }

      // X labels (start, mid, end)
      ctx.fillStyle = Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.55)
      ctx.textBaseline = "top"
      ctx.textAlign = "left"
      ctx.fillText(root._formatTs(minTs), plotX, plotY + plotH + 7)
      if (pts.length >= 3) {
        ctx.textAlign = "center"
        ctx.fillText(root._formatTs(minTs + span / 2), plotX + plotW / 2, plotY + plotH + 7)
      }
      if (pts.length >= 2) {
        ctx.textAlign = "right"
        ctx.fillText(root._formatTs(maxTs), plotX + plotW, plotY + plotH + 7)
      }
    }
  }

  PlainText {
    visible: root._validPoints().length === 0
    anchors.centerIn: parent
    width: Math.min(parent.width - 32, 420)
    horizontalAlignment: Text.AlignHCenter
    wrapMode: Text.WordWrap
    text: root.emptyText
    color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.48)
    font.family: root.fontFamily
    font.pixelSize: 12
  }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true

    onPositionChanged: function(mouse) {
      var pts = root._validPoints()
      if (pts.length === 0) {
        root.hoveredIndex = -1
        return
      }
      var plotX = root.padLeft
      var plotW = Math.max(10, root.width - root.padLeft - root.padRight)
      if (mouse.x < plotX || mouse.x > plotX + plotW) {
        root.hoveredIndex = -1
        return
      }
      var minTs = Number(pts[0].ts)
      var maxTs = Number(pts[pts.length - 1].ts)
      var span = Math.max(60, maxTs - minTs)
      var targetTs = minTs + ((mouse.x - plotX) / plotW) * span
      var bestIdx = 0
      var bestDist = Math.abs(Number(pts[0].ts) - targetTs)
      for (var i = 1; i < pts.length; i++) {
        var dist = Math.abs(Number(pts[i].ts) - targetTs)
        if (dist < bestDist) {
          bestDist = dist
          bestIdx = i
        }
      }
      root.hoveredIndex = bestIdx
      root.hoverX = mouse.x
      root.hoverY = mouse.y
    }
    onExited: root.hoveredIndex = -1
  }

  Rectangle {
    id: tip
    readonly property var pt: {
      var pts = root._validPoints()
      return (root.hoveredIndex >= 0 && root.hoveredIndex < pts.length) ? pts[root.hoveredIndex] : null
    }
    visible: pt !== null
    z: 10
    radius: root.cornerRadius
    color: Qt.rgba(root.background.r, root.background.g, root.background.b, 0.97)
    border.width: 1
    border.color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.22)
    width: Math.max(135, tipCol.implicitWidth + 16)
    height: tipCol.implicitHeight + 12
    x: {
      var desired = root.hoverX + 12
      if (desired + width > root.width - 6) desired = root.hoverX - width - 12
      return Math.max(6, Math.min(root.width - width - 6, desired))
    }
    y: Math.max(6, Math.min(root.height - height - 6, root.hoverY - height / 2))

    Column {
      id: tipCol
      anchors.left: parent.left
      anchors.top: parent.top
      anchors.margins: 7
      spacing: 2

      PlainText {
        text: tip.pt ? root._formatTs(tip.pt.ts) : ""
        color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.72)
        font.family: root.fontFamily
        font.pixelSize: 10
      }

      PlainText {
        text: tip.pt
          ? (Number(tip.pt.usedPct).toFixed(1) + "% used  ·  " + Math.max(0, 100 - Number(tip.pt.usedPct)).toFixed(1) + "% left")
          : ""
        color: root.accent
        font.family: root.fontFamily
        font.pixelSize: 11
        font.bold: true
      }
    }
  }
}
