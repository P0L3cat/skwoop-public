// Expression layer for the robot face. A mood point {v, a, d} (valence, arousal,
// dominance, each -1..1) or a mix of named expressions becomes a pose, through
// the same spring as the face sim.
//
//   PadFace.install(faceData, expressions)   once, after SkwoopFace.init
//   const pf = new PadFace.PadFace(sim, SkwoopFace.Smoother)
//   pf.listen({v, a, d})                     mood while the face is listening
//   pf.reply({v, a, d})                      mood while the face is speaking
//   pf.replyMix({content: 0.7, sad: 0.3}, k) explicit blend at intensity k (0..1)
//
// The numbers live in expressions.json. The demo cycles them with ?pad=1.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.PadFace = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  let D = null;           // face-data.json
  let X = null;           // expressions.json
  let NEUTRAL = null;     // face-data pose 0 plus the extra fields

  function clamp(x, lo, hi) {
    return x < lo ? lo : x > hi ? hi : x;
  }

  function unit(x) {
    return Number.isFinite(x) ? clamp(x, -1, 1) : 0;
  }

  // Parse a mood point once at the boundary.
  function parsePad(p) {
    if (p === null || typeof p !== "object") throw new TypeError("PAD must be an object {v, a, d}");

    return [unit(Number(p.v)), unit(Number(p.a)), unit(Number(p.d))];
  }

  function install(faceData, expressions) {
    D = faceData;
    X = expressions;
    for (const [f, spec] of Object.entries(X.extra_fields)) {
      if (!D.fields.includes(f)) D.fields.push(f);

      D.pose_min[f] = spec.min;
      D.pose_max[f] = spec.max;
      for (const p of D.poses) if (p[f] === undefined) p[f] = spec.neutral;
    }

    NEUTRAL = Object.assign({}, D.poses[0]);
    X.byName = {};
    X.expressions.forEach((e, i) => {
      e.index = i;
      e.full = Object.assign({}, NEUTRAL, e.pose);
      e.norm = Math.hypot(e.pad[0], e.pad[1], e.pad[2]);
      X.byName[e.name] = e;
    });
  }

  function names() {
    return X.expressions.map((e) => e.name);
  }

  function zeros() {
    return new Array(X.expressions.length).fill(0);
  }

  function smoothstep(lo, hi, x) {
    const t = clamp((x - lo) / (hi - lo), 0, 1);

    return t * t * (3 - 2 * t);
  }

  // A mood point -> expression weights. Direction picks the expressions (inverse-angle weights over the anchors
  // within ~60 degrees), distance from neutral sets the intensity k. Sum of weights = k, so 30% sad is 30% of the way
  // from neutral to sad, and a point between two anchors shows both.
  function weightsFromPad(pad) {
    // Display-side gain on valence and arousal (face only; the voice tag source is not affected). 1.0 = off.
    const g = X.display.va_gain;
    const [v, a, d] = [unit(g * pad[0]), unit(g * pad[1]), pad[2]], B = X.pad_blend;
    const r = Math.hypot(v, a, d);
    const w = zeros();
    if (r < B.min_radius) return w;

    let best = 0, bestCos = -2;
    X.expressions.forEach((e, i) => {
      const c = (v * e.pad[0] + a * e.pad[1] + d * e.pad[2]) / (r * e.norm);
      if (c > bestCos) [best, bestCos] = [i, c];

      const gate = smoothstep(B.cos_lo, B.cos_hi, c);
      if (gate > 0) w[i] = gate / Math.max(Math.acos(clamp(c, -1, 1)), 1e-4) ** B.idw_power;
    });
    let sum = w.reduce((s, x) => s + x, 0);
    if (sum === 0) {
      w[best] = 1;
      sum = 1;
    }

    const mean = [0, 0, 0];
    for (let i = 0; i < w.length; i++) {
      w[i] /= sum;
      for (let k = 0; k < 3; k++) mean[k] += w[i] * X.expressions[i].pad[k];
    }

    const m2 = mean[0] ** 2 + mean[1] ** 2 + mean[2] ** 2;
    // Dead zone: a point within min_radius of neutral shows nothing, then fades in by full_radius.
    const fade = smoothstep(B.min_radius, B.full_radius, r);
    const k = m2 > 0 ? fade * clamp((v * mean[0] + a * mean[1] + d * mean[2]) / m2, 0, 1) : 0;

    return w.map((x) => x * k);
  }

  // An explicit blend {name: weight} -> expression weights, scaled to intensity k. Named blends expand first.
  function weightsFromMix(mix, k) {
    const w = zeros();
    const add = (name, x) => {
      const nb = X.named_blends[name];
      if (nb) {
        for (const [n2, x2] of Object.entries(nb)) add(n2, x * x2);
        return;
      }

      const e = X.byName[name];
      if (!e) throw new RangeError(`unknown expression ${name}`);

      w[e.index] += x;
    };
    for (const [name, x] of Object.entries(mix)) add(name, Math.max(0, Number(x) || 0));

    const sum = w.reduce((s, x) => s + x, 0);
    const kk = clamp(k === undefined ? 1 : k, 0, 1);

    return sum > 0 ? w.map((x) => x / sum * kk) : w;
  }

  // Listening: a soft, safe mirror. The user's expression maps through the empathy table (bad news -> sympathy, never
  // anger back), at mirror_gain of the user's intensity (capped), on top of a small attentive base.
  function listenWeights(userPad) {
    const L = X.listening;
    const user = weightsFromPad(userPad);
    const k = user.reduce((s, x) => s + x, 0);
    const out = weightsFromMix(L.attentive, Object.values(L.attentive).reduce((s, x) => s + x, 0));
    if (k === 0) return out;

    const mirrored = zeros();
    user.forEach((x, i) => {
      for (const [name, y] of Object.entries(L.empathy[X.expressions[i].name])) mirrored[X.byName[name].index] += x * y;
    });
    const gain = Math.min(L.mirror_gain * k, L.mirror_cap) / k;
    const base = 1 - Math.min(L.mirror_gain * k, L.mirror_cap);    // the attentive base fades as the mirror grows

    return out.map((x, i) => x * base + mirrored[i] * gain);
  }

  // Weights -> pose: neutral plus each expression's offset, then the face's clamps.
  // Two display rules keep a blend readable:
  // - minority boost: each share of the mix is raised to minority_gamma (< 1)
  //   and renormalised, so a 20% part is drawn nearer 30% and does not vanish.
  //   Intensity k is unchanged.
  // - brow geometry by strength: tilt and raise come only from expressions that
  //   show brows, weighted by brow strength. Brow strength itself blends linearly.
  function displayWeights(weights) {
    const k = weights.reduce((s, x) => s + x, 0);
    if (k === 0) return weights;

    const g = X.display.minority_gamma;
    const raised = weights.map((x) => (x / k) ** g);
    const sum = raised.reduce((s, x) => s + x, 0);

    return raised.map((x) => x / sum * k);
  }

  function pose(weights) {
    const w = displayWeights(weights);
    const out = Object.assign({}, NEUTRAL);
    let browW = 0, tilt = 0, raise = 0;
    w.forEach((x, i) => {
      if (x === 0) return;

      const full = X.expressions[i].full;
      for (const f of D.fields) out[f] += x * (full[f] - NEUTRAL[f]);

      const b = x * full.brow;
      browW += b;
      tilt += b * full.brow_tilt;
      raise += b * full.brow_raise;
    });
    if (X.display.brow_geometry_by_strength && browW > 0) {
      out.brow_tilt = tilt / browW;
      out.brow_raise = raise / browW;
    }

    for (const f of D.fields) out[f] = clamp(out[f], D.pose_min[f], D.pose_max[f]);

    const lim = 0.4 * out.mouth_w;
    out.mouth_curve = clamp(out.mouth_curve, -lim, lim);

    return out;
  }

  // Mood point shown for a weight vector (for captions and the voice tag).
  function padOf(weights) {
    const p = [0, 0, 0];
    weights.forEach((x, i) => {
      for (let k = 0; k < 3; k++) p[k] += x * X.expressions[i].pad[k];
    });

    return p;
  }

  // Voice tag: the strongest expression's tag once the mood is strong enough, else the default. Proposed tags fall
  // back to an approved one until the owner approves them.
  function voiceTag(weights, allowProposed) {
    const V = X.voice;
    const k = weights.reduce((s, x) => s + x, 0);
    if (k < V.min_intensity) return V.default_tag;

    let top = 0;
    weights.forEach((x, i) => { if (x > weights[top]) top = i; });
    const tag = X.expressions[top].tag;
    if (V.approved_tags.includes(tag)) return tag;

    return allowProposed ? tag : V.proposed_tags[tag] ?? V.default_tag;
  }

  // Tone index 0 (neutral), 1 (upbeat), or 2 (down) as a PAD point.
  function toneFallback(tone) {
    const p = X.tone_fallback[String(tone)] ?? X.tone_fallback["0"];

    return { v: p[0], a: p[1], d: p[2] };
  }

  class PadFace {
    constructor(sim, Smoother) {
      this.sim = sim;
      this.cd = Smoother.cd;
      this.w = zeros();
      this.vel = zeros();
      this.resetListen();
      this.replyTarget = zeros();
      this.idleTarget = zeros();
      this.override = null;            // a fixed target for every face state (contact sheets)
      sim.poseFor = (face, dt) => this.step(face, dt);
    }

    resetListen() {
      this.listenTarget = weightsFromMix(X.listening.attentive, 0.2);
    }

    listen(pad) {
      this.listenTarget = listenWeights(parsePad(pad));
    }

    reply(pad) {
      this.replyTarget = weightsFromPad(parsePad(pad));
    }

    replyMix(mix, k) {
      this.replyTarget = weightsFromMix(mix, k);
    }

    hold(weights) {
      this.override = weights;
    }

    target(face) {
      if (this.override) return this.override;

      if (face === "listening" || face === "thinking") return this.listenTarget;

      return face === "speaking" ? this.replyTarget : this.idleTarget;
    }

    // One tick: each expression weight springs to its target (the face's critically damped spring), then poses.
    step(face, dt) {
      const tgt = this.target(face);
      for (let i = 0; i < this.w.length; i++) {
        [this.w[i], this.vel[i]] = this.cd(this.w[i], this.vel[i], tgt[i], clamp(dt, 0, 0.25));
        this.w[i] = Math.max(0, this.w[i]);
      }

      const p = padOf(this.w);
      [this.sim.mood.v, this.sim.mood.a, this.d] = p;

      return pose(this.w);
    }

    settle(weights) {
      this.w = weights.slice();
      this.vel = zeros();
    }
  }

  return {
    install, names, parsePad, toneFallback, weightsFromPad, weightsFromMix,
    listenWeights, displayWeights, pose, padOf, voiceTag, PadFace,
  };
});
