# Make your own face

On the web, a face is one drawing function. The mood engine in `face.js`
already turns a state into a snapshot. Your function only draws that snapshot.

This page is the easy path. A firmware face is not easy yet, and nothing
here has run on hardware.

## The sample: Dot

[`sample-face/dot-face.js`](sample-face/dot-face.js) is the worked example.
It only draws. Every value it reads comes from a `FaceSim` snapshot, the same
one the robot face uses, so blinks, the valence/arousal spring, and the
speaking mouth are already done.

Load it after `face.js`. It sets `drawDot(ctx, snapshot, title)` and draws
in a 466×466 circle, the 1.75-inch round display. The screen center is 233.

Dot uses:

- eyes — `eye_w`, `eye_h`, `eye_dx`, `eye_dy`, `blink`, and `fx.lid`.
  A strong `fx.squint` (0.6 or more) turns them into "^" arcs
- brows — `fx.brow`, `fx.brow_raise`, and `fx.brow_tilt`, and only when
  `fx.brow` is above 0.05
- cheeks — `blush_opa`
- mouth — `mouth_w`, `mouth_h`, and `mouth_curve` (positive curves up,
  negative curves down)
- bob — `bob`, added to the eye and mouth height

## Try it

Serve this directory and open the dot sample. `demo.js` stays as it is:

```sh
python3 -m http.server
```

Then open <http://localhost:8000/?face=dot>. That calls `drawDot` instead of
`SkwoopFace.drawRobot`.

`?pad=1` cycles the named expressions in `expressions.json`. The buttons
under the canvas switch idle, listening, thinking, and speaking.

## Wiring

The demo in this directory is the wiring. It is not a voice page. There is
no bridge and no microphone.

1. `index.html` loads `face-data.js`, `face.js`, `pad-face.js`,
   `sample-face/dot-face.js`, then `demo.js`.
2. `demo.js` uses `window.FACE_DATA` when the page is opened from disk, and
   otherwise fetches `face-data.json`. It calls `SkwoopFace.init` and
   constructs `new SkwoopFace.FaceSim("robot")`.
3. `sim.settleOn("idle")` runs the sim forward so the first frame is settled.
4. Each frame calls `sim.step(now)` and either `SkwoopFace.drawRobot` or
   `drawDot`.

`step` returns the snapshot. The draw function expects that object: eye size
and position, `fx`, `bob`, `blush_opa`, `glow`, `eye`, mouth width, height,
and curve, `mouth`, and `accent`. The third argument is the optional state
title (`st.label` is "Ready" while idle).

Your own face is the same shape: one function `(ctx, snapshot, title)` that
paints a 466 px circle, loaded after `face.js`, called with the snapshot
from `FaceSim.step`.

## Firmware

A face on the pod is a different job. Firmware is separate from this web
face. It has not run on hardware.

A new firmware face is not one drawing file. It needs a style chosen at
build time, plus layout and drawing in the display code. There is no short
path for that yet.

This web face does not read that build-time style. Here, the function you
call is the face: `SkwoopFace.drawRobot`, or `drawDot`, or your own.
