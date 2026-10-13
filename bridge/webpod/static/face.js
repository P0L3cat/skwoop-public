// Skwoop pod face for the browser: a line-for-line port of the sim (tools/render_va.py FaceSim, tools/pod_mood.py,
// tools/face_layout.py robot layout), which mirrors the firmware. Every number
// comes from face-data.json, generated from the firmware sources by webpod/build_assets.py.
// tests/test_webpod.py runs the math below under Node and compares it with the Python sim.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.SkwoopFace = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const DEG = 0.0174533;
  let D = null;           // face-data.json

  function init(data) {
    D = data;
  }

  // std::lround
  function R(x) {
    return x >= 0 ? Math.floor(x + 0.5) : -Math.floor(-x + 0.5);
  }

  function clamp(x, lo, hi) {
    return x < lo ? lo : x > hi ? hi : x;
  }

  function unit(x) {
    return Number.isNaN(x) ? 0 : clamp(x, -1, 1);
  }

  // ------------------------------------------------------------------ pod_mood
  function clampPose(p) {
    const out = {};
    for (const f of D.fields) out[f] = Math.max(D.pose_min[f], Math.min(D.pose_max[f], p[f]));

    const lim = 0.4 * out.mouth_w;
    out.mouth_curve = Math.max(-lim, Math.min(lim, out.mouth_curve));

    return out;
  }

  function blend(v, a) {
    v = unit(v);
    a = unit(a);
    const P = D.poses;
    const pv = v >= 0, pa = a >= 0;
    const c = P[pv ? (pa ? 1 : 2) : (pa ? 4 : 3)];
    const cv = P[pv ? (pa ? 2 : 1) : (pa ? 3 : 4)];
    const ca = P[pv ? (pa ? 4 : 3) : (pa ? 1 : 2)];
    const u = Math.abs(v), w = Math.abs(a);
    const wn = (1 - u) * (1 - w), wv = u * (1 - w), wa = (1 - u) * w, wc = u * w;
    const out = {};
    for (const f of D.fields) {
      out[f] = wn * P[0][f] + wv * 0.5 * (c[f] + cv[f]) + wa * 0.5 * (c[f] + ca[f]) + wc * c[f];
    }

    return clampPose(out);
  }

  class Smoother {
    constructor() {
      this.v = 0; this.a = 0; this.dv = 0; this.da = 0;
      this.primed = false;
    }

    static cd(x, vel, target, dt) {
      const omega = 2.0 / D.smooth_s;
      const k = omega * dt;
      const e = 1.0 / (1.0 + k + 0.48 * k * k + 0.235 * k * k * k);
      const change = x - target;
      const temp = (vel + omega * change) * dt;

      return [target + (change + temp) * e, (vel - omega * temp) * e];
    }

    step(tv, ta, dt) {
      tv = unit(tv);
      ta = unit(ta);
      if (!this.primed) {
        this.v = tv; this.a = ta; this.dv = 0; this.da = 0; this.primed = true;

        return this;
      }

      dt = Math.max(0, Math.min(0.25, dt));
      [this.v, this.dv] = Smoother.cd(this.v, this.dv, tv, dt);
      [this.a, this.da] = Smoother.cd(this.a, this.da, ta, dt);
      this.v = unit(this.v);
      this.a = unit(this.a);

      return this;
    }
  }

  function moodForName(name) {
    const m = D.mood_names[name];

    return m ? [m[0], m[1]] : null;
  }

  // ------------------------------------------------------------------ colours
  function mixRgb(a, b, t) {
    let out = 0;
    for (const sh of [16, 8, 0]) {
      const ca = (a >> sh) & 255, cb = (b >> sh) & 255;
      out |= (R(ca + (cb - ca) * t) & 255) << sh;
    }

    return out;
  }

  function near(a, b) {
    return [0, 8, 16].every((k) => Math.abs(((a >> k) & 255) - ((b >> k) & 255)) < 3);
  }

  function css(rgb, alpha) {
    const r = (rgb >> 16) & 255, g = (rgb >> 8) & 255, b = rgb & 255;

    return alpha === undefined ? `rgb(${r},${g},${b})` : `rgba(${r},${g},${b},${alpha})`;
  }

  // ------------------------------------------------------------------ FaceSim (SkwoopDisplay::AnimateFace)
  class FaceSim {
    constructor(style, seed) {
      this.style = style;
      this.seed = (seed >>> 0) || 7;
      this.mood = new Smoother();
      this.face = "idle";
      this.eye_w = 58; this.eye_h = 10; this.eye_dx = 0; this.eye_dy = 0;
      this.mouth_w = 28; this.mouth_h = 4; this.mouth_curve = 0; this.blush = 0;
      this.fx = Object.assign({}, D.poses[0]);
      this.gaze_y = 0; this.blink_f = 1; this.phase = 0;
      this.eye_rgb = D.pal.kGray; this.body_rgb = D.styles.idle.body;
      this.blink_step = -1; this.next_blink = 0; this.next_glance = 0; this.glance_x = 0;
      this.tick = 0; this.last_ms = 0;
    }

    rand() {                       // xorshift32: the device uses esp_random(); any uniform source will do
      let x = this.seed;
      x ^= x << 13; x >>>= 0;
      x ^= x >>> 17;
      x ^= x << 5; x >>>= 0;
      this.seed = x;

      return x;
    }

    setFace(face) {
      if (face !== this.face && D.styles[face]) {
        this.face = face;
        this.blink_step = -1;
        this.next_blink = 0;
      }
    }

    settleOn(face, va) {
      this.setFace(face);
      const s = D.styles[this.face];
      this.eye_rgb = s.eye;
      this.body_rgb = s.body;
      for (let t = 1; t < 80; t++) this.step(t * D.tick_ms, { moodOverride: va, blinks: false, glances: false });

      this.last_ms = 0;
      this.tick = 0;
    }

    step(now, opts) {
      const o = Object.assign({ moodOverride: null, tone: 0, blinks: true, glances: true, bob: true }, opts || {});
      this.tick += 1;
      const s = D.styles[this.face];
      let name = s.mood;
      if (this.face === "speaking") name = o.tone === 1 ? "talking_happy" : o.tone === 2 ? "talking_sad" : "talking";

      let [tv, ta] = moodForName(name);
      if (o.moodOverride && (this.face === "idle" || this.face === "speaking")) [tv, ta] = o.moodOverride;

      const dt = this.last_ms === 0 ? 0 : (now - this.last_ms) / 1000;
      this.mood.step(tv, ta, dt);
      this.last_ms = now;
      // PAD prototype hook (pad-face.js): an expression layer can supply the pose; without one, the v/a blend.
      const pose = this.poseFor ? this.poseFor(this.face, dt) : blend(this.mood.v, this.mood.a);
      const fixed = s.fixed;
      const tgt = Object.assign({}, pose);
      let tdx = s.eye_dx;
      let tw, th, tdy, mw, mh, mc, tb;
      if (fixed) {
        Object.assign(tgt, { brow: 0, brow_tilt: 0, brow_raise: 0, lid: 0, squint: 0, glow: 0.6, bob_px: 0 });
        [tw, th, tdy, mw, mh, mc, tb] = [s.eye_w, s.eye_h, s.eye_dy, s.mouth_w, s.mouth_h, 0, 0];
      } else {
        [tw, th, tdy] = [pose.eye_w, pose.eye_h, pose.eye_dy + s.eye_dy];
        tdx += pose.gaze_dx ?? 0;
        [mw, mh, mc, tb] = [pose.mouth_w, pose.mouth_h, pose.mouth_curve, pose.blush];
      }

      let tgy = s.eye_dy;
      let blink = 1.0;
      if (o.blinks && ["idle", "alert", "listening", "message"].includes(this.face)) {
        if (this.next_blink === 0) this.next_blink = now + 1500 + this.rand() % 2500;
        if (this.blink_step < 0 && now >= this.next_blink) this.blink_step = 0;
        if (this.blink_step >= 0) {
          blink = D.blink[this.blink_step];
          th *= blink;
          this.blink_step += 1;
          if (this.blink_step >= D.blink.length) {
            this.blink_step = -1;
            this.next_blink = now + Math.trunc(pose.blink_ms * (0.55 + (this.rand() % 1000) * 0.0009));
          }
        }
      }

      if (o.glances && this.face === "idle") {
        if (now >= this.next_glance) {
          const r = this.rand() % 5;
          this.glance_x = r === 0 ? -10 : r === 1 ? 10 : 0;
          this.next_glance = now + 2000 + this.rand() % 5000;
        }
        tdx += this.glance_x;
      }

      if (o.glances && this.face === "message") {
        if (now >= this.next_glance) {
          this.glance_x = this.rand() % 3 === 0 ? 12 : 0;
          this.next_glance = now + 1800 + this.rand() % 3500;
        }
        tdx += this.glance_x;
        tdy -= this.glance_x > 0 ? 6 : 0;
        tgy -= this.glance_x > 0 ? 6 : 0;
      }

      if (this.face === "speaking" && this.tick % 2 === 0) {
        this.mouth_w = Math.min(mw + 6 + this.rand() % 50, D.pose_max.mouth_w);
        this.mouth_h = Math.min(mh + 2 + this.rand() % 12, D.pose_max.mouth_h);
      }

      if (this.face === "listening") {
        mh = 12 + 8 * Math.sin((now % 1200) / 1200.0 * 6.2832);
        mw = mh;
        mc = 0;
      }

      const ease = (cur, t, k) => cur + (t - cur) * k;
      this.eye_w = ease(this.eye_w, tw, 0.35);
      this.eye_h = this.blink_step >= 0 ? th : ease(this.eye_h, th, 0.35);
      this.eye_dx = ease(this.eye_dx, tdx, 0.25);
      this.eye_dy = ease(this.eye_dy, tdy, 0.25);
      if (this.face !== "speaking") {
        this.mouth_w = ease(this.mouth_w, mw, 0.3);
        this.mouth_h = ease(this.mouth_h, mh, 0.3);
      }
      this.mouth_curve = ease(this.mouth_curve, mc, 0.3);
      this.blush = ease(this.blush, tb, 0.3);
      for (const f of D.fields) this.fx[f] = ease(this.fx[f], tgt[f], 0.3);

      this.gaze_y = ease(this.gaze_y, tgy, 0.25);
      this.blink_f = blink;
      if (o.bob) this.phase = (this.phase + 6.2832 * this.fx.bob_hz * dt) % 6.2832;

      if (this.eye_rgb !== s.eye) {
        this.eye_rgb = mixRgb(this.eye_rgb, s.eye, 0.35);
        if (near(this.eye_rgb, s.eye)) this.eye_rgb = s.eye;
      }


      return this.snapshot(o.bob);
    }

    snapshot(bob) {
      const s = D.styles[this.face];

      return {
        style: this.style, face: this.face, eye_w: this.eye_w, eye_h: this.eye_h, eye_dx: this.eye_dx,
        eye_dy: this.eye_dy, mouth_w: R(this.mouth_w), mouth_h: R(this.mouth_h), mouth_curve: R(this.mouth_curve),
        mouth_wf: this.mouth_w, mouth_hf: this.mouth_h, mouth_cf: this.mouth_curve, blush: this.blush,
        blush_opa: R(this.blush * D.geo.kBlushOpaMax), fx: Object.assign({}, this.fx), gaze_x: this.eye_dx,
        gaze_y: this.gaze_y, blink: this.blink_f, phase: this.phase,
        bob: bob === false ? 0 : this.fx.bob_px * Math.sin(this.phase),
        eye: this.eye_rgb, body: this.body_rgb, mouth: s.mouth, accent: s.accent, glow: s.glow, label: s.label,
        v: this.mood.v, a: this.mood.a,
      };
    }
  }

  // ------------------------------------------------------------------ face_layout
  function box(x, y, w, h, show) {
    return [R(x), R(y), R(w), R(h), show === undefined ? true : Boolean(show)];
  }

  function browHalf(side, ln, tilt) {
    const a = tilt * DEG;

    return [R(-side * ln / 2 * Math.cos(a)), R(-ln / 2 * Math.sin(a))];
  }

  function robotLayout(eyeW, eyeH, eyeDx, eyeDy, fx, bob) {
    const K = D.layout;
    const arcs = fx.squint >= D.squint_arc;
    const t = clamp((fx.squint - 0.35) / (D.squint_arc - 0.35), 0, 1);
    const asym = fx.asym ?? 0;      // PAD prototype: > 0 narrows the left eye and lifts the right brow (< 0 mirrors)
    const o = { eye: [], arc: [], brow_c: [], brow_d: [] };
    for (const side of [-1, 1]) {
      const lid = clamp(fx.lid + 0.35 * Math.max(0, -side * asym), 0, 0.6);
      const raise = fx.brow_raise + 6 * Math.max(0, side * asym);
      const h = Math.max(eyeH * (1 - lid) * (1 - 0.55 * t), 4.0);
      const cy = K.kRbEyeY + eyeDy + eyeH * lid / 2 + bob;
      const cx = side * K.kRbEyeGap + eyeDx;
      o.eye.push(box(cx, cy, eyeW, h, !arcs));
      const acy = K.kRbEyeY + eyeDy + bob + eyeW * 0.15;
      o.arc.push(box(cx, acy, eyeW, eyeW, arcs));
      const top = arcs ? acy - eyeW / 2 : cy - h / 2;
      o.brow_c.push([R(cx), R(top - K.kRbBrowGap - raise)]);
      o.brow_d.push(browHalf(side, K.kRbBrowLen, fx.brow_tilt));
    }

    const bo = clamp(fx.brow, 0, 1) * 230;
    o.brow_opa = bo < 8 ? 0 : R(bo);
    o.glow_opa = R(50 + 150 * clamp(fx.glow, 0, 1));
    o.dim = R(255 * clamp((0.6 - fx.glow) * 0.5, 0, 0.25));

    return o;
  }

  function part(dx, dy, sx, sy, rotDeg, opa) {
    return { dx: R(dx), dy: R(dy), sx: R(sx * 256.0), sy: R(sy * 256.0), rot: R(rotDeg * 10.0), opa: R(clamp(opa, 0, 255)) };
  }

  function safeTextWidth(y, boxH) {
    const e = Math.abs(y) + Math.floor((boxH + 1) / 2);
    if (e >= D.text_safe_r) return 0;

    return 2 * Math.floor(Math.sqrt(D.text_safe_r * D.text_safe_r - e * e));
  }

  // ------------------------------------------------------------------ canvas drawing (LVGL look-alikes)
  function pill(ctx, cx, cy, w, h, fill) {
    if (w <= 0 || h <= 0) return;

    const r = Math.min(w, h) / 2;
    ctx.beginPath();
    ctx.roundRect(cx - w / 2, cy - h / 2, w, h, r);
    ctx.fillStyle = fill;
    ctx.fill();
  }

  function gradPill(ctx, cx, cy, w, h, top, bot, border, borderCol) {
    const g = ctx.createLinearGradient(0, cy - h / 2, 0, cy + h / 2);
    g.addColorStop(0, css(top));
    g.addColorStop(1, css(bot));
    if (border) pill(ctx, cx, cy, w, h, css(borderCol));

    pill(ctx, cx, cy, w - 2 * border, h - 2 * border, g);
  }

  function lvArc(ctx, cx, cy, size, width, a0, a1, color) {
    const r = (size - width) / 2;
    if (r <= 0) return;

    ctx.beginPath();
    ctx.arc(cx, cy, r, a0 * Math.PI / 180, (a1 > a0 ? a1 : a1 + 360) * Math.PI / 180);
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineCap = "round";
    ctx.stroke();
  }

  function lvLine(ctx, pts, width, color) {
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (const [x, y] of pts.slice(1)) ctx.lineTo(x, y);

    ctx.strokeStyle = color;
    ctx.lineWidth = Math.max(1, width);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.stroke();
  }

  function ring(ctx) {        // render_va.ring: 0 deg = 12 o'clock, clockwise
    const C = D.screen / 2, P = D.pal;
    const seg = (diameter, width, a0, a1, color, round) => {
      ctx.beginPath();
      ctx.arc(C, C, diameter / 2 - width, (a0 - 90) * Math.PI / 180, (a1 - 90) * Math.PI / 180);
      ctx.strokeStyle = css(color);
      ctx.lineWidth = width;
      ctx.lineCap = round ? "round" : "butt";
      ctx.stroke();
    };
    seg(444, 2, 0, 360, P.kTrack, false);
    [P.kBlue, P.kMint, P.kMint, P.kMint].forEach((col, i) => seg(444, 6, i * 90 + 3.5, (i + 1) * 90 - 3.5, col, false));
  }

  function fitText(ctx, text, maxW, spacing) {
    const width = (t) => ctx.measureText(t).width + spacing * Math.max(0, t.length - 1);
    if (!maxW || width(text) <= maxW) return text;

    for (let cut = text.length - 1; cut > 0; cut--) {
      const s = text.slice(0, cut).trimEnd();
      if (!s) break;
      if (width(s + "...") <= maxW) return s + "...";
    }

    return "";
  }

  function label(ctx, text, size, color, y, opts) {
    const o = Object.assign({ spacing: 0, weight: 400, maxW: 0, pill: null }, opts || {});
    const C = D.screen / 2;
    ctx.font = `${o.weight} ${size}px Montserrat, sans-serif`;
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    const t = fitText(ctx, text, o.maxW, o.spacing);
    if (!t) return;

    const w = ctx.measureText(t).width + o.spacing * Math.max(0, t.length - 1);
    if (o.pill) {
      const [px, py, opa] = o.pill;
      pill(ctx, C, C + y, w + 2 * px, size * 1.1 + 2 * py, `rgba(0,0,0,${opa / 255})`);
    }

    ctx.fillStyle = css(color);
    let x = C - w / 2;
    for (const ch of t) {
      ctx.fillText(ch, x, C + y);
      x += ctx.measureText(ch).width + o.spacing;
    }
  }

  function clipRound(ctx) {
    const C = D.screen / 2;
    ctx.beginPath();
    ctx.arc(C, C, C, 0, 2 * Math.PI);
    ctx.clip();
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, D.screen, D.screen);
  }

  function drawRobot(ctx, st, title) {
    const G = D.geo, P = D.pal, K = D.layout, C = D.screen / 2;
    ctx.save();
    clipRound(ctx);
    ring(ctx);
    const vy = C + G.kVisorY;
    gradPill(ctx, C, vy, G.kVisorW, G.kVisorH, P.kVisorTop, P.kVisorBot, 1, P.kVisorEdge);
    const lay = robotLayout(st.eye_w, st.eye_h, st.eye_dx, st.eye_dy, st.fx, st.bob);
    const bobI = R(st.bob);
    const eyeCol = mixRgb(st.eye, 0, lay.dim / 255);
    if (st.blush_opa >= 4) {
      for (const sx of [-1, 1]) pill(ctx, C + sx * G.kBlushX, vy + G.kBlushY + bobI, G.kBlushW, G.kBlushH, css(P.kBlush, st.blush_opa / 255));
    }

    if (st.glow) {
      ctx.save();
      ctx.shadowColor = css(eyeCol, Math.min(1, 0.55 * lay.glow_opa / 127));
      ctx.shadowBlur = 13;
      for (const [x, y, w, h, show] of lay.eye) if (show) pill(ctx, C + x, vy + y, w, h, css(eyeCol));
      ctx.restore();
    }

    for (const [x, y, w, h, show] of lay.eye) if (show) pill(ctx, C + x, vy + y, w, h, css(eyeCol));
    for (const [x, y, w, , show] of lay.arc) if (show) lvArc(ctx, C + x, vy + y, w, K.kRbArcW, K.kRbArcA0, K.kRbArcA1, css(eyeCol));

    if (lay.brow_opa) {
      lay.brow_c.forEach(([bx, by], i) => {
        const [dx, dy] = lay.brow_d[i];
        lvLine(ctx, [[C + bx + dx, vy + by + dy], [C + bx - dx, vy + by - dy]], K.kRbBrowW, css(eyeCol, lay.brow_opa / 255));
      });
    }

    const mw = st.mouth_w, mh = st.mouth_h, mc = st.mouth_curve;
    const my = vy + G.kMouthY + bobI;
    if (mw >= 2 && mh >= 2) {
      if (mc === 0) {
        pill(ctx, C, my, mw, mh, css(st.mouth));
      } else {
        const ln = Math.max(1, mw - mh), bw = G.kSmileBoxW, bh = G.kSmileBoxH;
        const pts = [];
        for (let i = 0; i < 9; i++) {
          const t = -1 + 2 * i / 8;
          pts.push([C + R(bw / 2 + t * ln / 2) - bw / 2, my + R(bh / 2 + mc / 2 - mc * t * t) - bh / 2]);
        }
        lvLine(ctx, pts, mh, css(st.mouth));
      }
    }

    label(ctx, "SKWOOP", 14, P.kTextDim, -148, { spacing: 5 });
    if (title) label(ctx, title, 20, st.accent, D.robot_state_y, { weight: 500, maxW: safeTextWidth(D.robot_state_y, 22) });

    ctx.restore();
  }

  return {
    init, R, blend, clampPose, Smoother, moodForName, mixRgb, FaceSim, robotLayout, safeTextWidth, drawRobot,
  };
});
