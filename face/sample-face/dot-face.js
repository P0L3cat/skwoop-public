// "Dot": a sample custom Skwoop face for the web pod. It is only a drawing layer: every value it reads comes from the
// FaceSim snapshot (face.js), so it moves with the same mood engine as the robot face (valence/arousal spring,
// pose blend, blinks, speaking mouth). Load it after face.js and call drawDot(ctx, snapshot, title) in a 466 px space.
(function (root) {
  "use strict";

  const C = 233;                                   // the pod screen is 466 x 466
  const PAPER = "#f4efe6", INK = "#2b2b33";

  function drawDot(ctx, st, title) {
    const fx = st.fx, bob = st.bob || 0;
    ctx.save();
    ctx.beginPath();
    ctx.arc(C, C, C, 0, 2 * Math.PI);
    ctx.fillStyle = PAPER;
    ctx.fill();
    ctx.clip();

    // Eyes: size from the pose, closed by the lid and blinks. A strong squint (happy) turns them into "^" arcs.
    const ew = st.eye_w * 0.55, eh = Math.max(3, st.eye_h * 0.55 * (1 - fx.lid) * st.blink);
    const ey = C - 30 + st.eye_dy * 0.6 + bob;
    ctx.fillStyle = INK;
    ctx.strokeStyle = INK;
    ctx.lineCap = "round";
    for (const side of [-1, 1]) {
      const ex = C + side * 62 + st.eye_dx * 0.6;
      if (fx.squint >= 0.6) {
        ctx.lineWidth = 9;
        ctx.beginPath();
        ctx.arc(ex, ey + ew * 0.25, ew * 0.45, Math.PI * 1.15, Math.PI * 1.85);
        ctx.stroke();
      } else {
        ctx.beginPath();
        ctx.ellipse(ex, ey, ew / 2, eh / 2, 0, 0, 2 * Math.PI);
        ctx.fill();
      }

      // Brows: opacity, height and tilt from the pose (they appear for strong moods only).
      if (fx.brow > 0.05) {
        const by = ey - eh / 2 - 18 - fx.brow_raise, tilt = side * fx.brow_tilt * 0.6;
        ctx.globalAlpha = Math.min(1, fx.brow);
        ctx.lineWidth = 7;
        ctx.beginPath();
        ctx.moveTo(ex - 20, by - tilt);
        ctx.lineTo(ex + 20, by + tilt);
        ctx.stroke();
        ctx.globalAlpha = 1;
      }
    }

    // Cheeks: the pose's blush.
    if (st.blush_opa >= 4) {
      ctx.fillStyle = `rgba(240,120,130,${st.blush_opa / 255})`;
      for (const side of [-1, 1]) {
        ctx.beginPath();
        ctx.ellipse(C + side * 105, ey + 48, 26, 14, 0, 0, 2 * Math.PI);
        ctx.fill();
      }
    }

    // Mouth: width and opening from the pose (they flap while speaking); curve > 0 smiles, < 0 frowns.
    const mw = Math.max(10, st.mouth_w * 0.9), my = C + 62 + bob;
    ctx.lineWidth = Math.max(5, Math.min(22, st.mouth_h));
    ctx.beginPath();
    ctx.moveTo(C - mw / 2, my);
    ctx.quadraticCurveTo(C, my + 2.8 * st.mouth_curve, C + mw / 2, my);
    ctx.stroke();

    if (title) {
      ctx.fillStyle = INK;
      ctx.font = "500 18px Montserrat, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(title, C, C + 150);
    }

    ctx.restore();
  }

  root.drawDot = drawDot;
})(typeof self !== "undefined" ? self : this);
