import QtQuick
import qs.Commons
import qs.Commons as Commons
import "../Model.js" as Model

// Compact circular gauge drawn clockwise from 12 o'clock, with an optional
// radial tick mark indicating even-pace position along the window.
Item {
  id: root

  property real fillFraction: 0.0
  property var paceMark: null
  property color ringColor: Commons.Color.popups.text
  property color trackColor: Qt.rgba(Commons.Color.popups.text.r, Commons.Color.popups.text.g, Commons.Color.popups.text.b, 0.16)
  property color paceColor: Commons.Color.popups.text
  property real strokeWidth: Math.max(2.5, Style.spaceReal(3))
  property real size: Style.space(26)

  // Smoothly animate fill changes without scaling from 0
  property real animatedFill: Model.clamp(fillFraction, 0, 1)
  Behavior on animatedFill {
    NumberAnimation {
      duration: Style.duration(220)
      easing.type: Easing.OutCubic
    }
  }

  implicitWidth: size
  implicitHeight: size
  width: size
  height: size

  onAnimatedFillChanged: canvas.requestPaint()
  onPaceMarkChanged: canvas.requestPaint()
  onRingColorChanged: canvas.requestPaint()
  onTrackColorChanged: canvas.requestPaint()
  onPaceColorChanged: canvas.requestPaint()
  onWidthChanged: canvas.requestPaint()
  onHeightChanged: canvas.requestPaint()

  Canvas {
    id: canvas
    anchors.fill: parent
    antialiasing: true
    renderStrategy: Canvas.Cooperative

    onPaint: {
      var ctx = getContext("2d")
      var w = width
      var h = height
      if (w <= 0 || h <= 0) return

      ctx.reset()
      ctx.clearRect(0, 0, w, h)

      var cx = w / 2
      var cy = h / 2
      var sw = Math.max(2, root.strokeWidth)
      var radius = Math.max(2, Math.min(cx, cy) - sw - 1)
      var startAngle = -Math.PI / 2

      // Track circle
      ctx.beginPath()
      ctx.arc(cx, cy, radius, 0, Math.PI * 2, false)
      ctx.strokeStyle = root.trackColor
      ctx.lineWidth = sw
      ctx.lineCap = "butt"
      ctx.stroke()

      // Filled arc clockwise from 12 o'clock
      var frac = Model.clamp(root.animatedFill, 0, 1)
      if (frac > 0.001) {
        ctx.beginPath()
        ctx.arc(cx, cy, radius, startAngle, startAngle + frac * Math.PI * 2, false)
        ctx.strokeStyle = root.ringColor
        ctx.lineWidth = sw
        ctx.lineCap = frac >= 0.999 ? "butt" : "round"
        ctx.stroke()
      }

      // Optional even-pace radial tick mark
      if (root.paceMark !== null && root.paceMark !== undefined) {
        var pm = Number(root.paceMark)
        if (isFinite(pm) && pm >= 0 && pm <= 1) {
          var angle = startAngle + pm * Math.PI * 2
          var innerR = Math.max(1, radius - sw * 0.9)
          var outerR = radius + sw * 0.9
          ctx.beginPath()
          ctx.moveTo(cx + Math.cos(angle) * innerR, cy + Math.sin(angle) * innerR)
          ctx.lineTo(cx + Math.cos(angle) * outerR, cy + Math.sin(angle) * outerR)
          ctx.strokeStyle = root.paceColor
          ctx.lineWidth = Math.max(1.5, sw * 0.5)
          ctx.lineCap = "round"
          ctx.stroke()
        }
      }
    }
  }
}
