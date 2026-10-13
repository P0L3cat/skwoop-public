# Barge-in

Alex talks over Skwoop: playback stops, the bridge listens, and the first words are kept.

## Protocol

| Step | Message |
|---|---|
| Pod hello | `features: {"barge_in": "client" \| "server"}` |
| Bridge hello reply | `features: {"barge_in": true}`, sent only when the pod asked |
| Client detection | pod sends `{"type":"listen","state":"start","mode":"auto","barge":true}`, then its preroll and live mic audio |
| Server detection | pod keeps streaming the mic during TTS; the bridge runs `barge.BargeInDetector` |
| Either way | bridge sends `tts stop`, cancels the reply, replays the held audio into a new turn |

Pods without the feature are unchanged: mic audio during TTS is dropped, and a wake word or PTT `listen start` still cuts in.

## Detector (`barge.py`; `webpod/static/app.js` has an identical copy)

Each 20 ms frame compares the mic level with the reference level, which is the loudest output sent in the last 0.8 s.

- Reference below -55 dBFS: Skwoop is silent, so nothing counts as a barge.
- The first 300 ms of each reply only learn the echo: how loud Skwoop sounds in the mic relative to the reference.
- A frame is voice when it is ≥12 dB above the noise floor and ≥10 dB above the learned echo.
- 240 ms of voice fires the barge. The tests check that the JS and Python copies fire on the same frame.

Sim result (`tests/test_barge.py`): `tts stop` arrives 182 ms after the first voiced frame. Echo alone at -10 dB plays the whole reply without firing.

## Web pod

- Client detection. Echo cancellation comes from the browser (`echoCancellation: true`), plus the detector on an AnalyserNode tap of the playback.
- 400 ms preroll ring.
- Turn it off with `?barge=0`.
- `webpodState()` exposes `barges`, `bargeEcho` and `bargeFloor`.

## ESP32 pods (design only; nothing flashed)

1. Hello sends `features.barge_in = "server"`. Only do this on boards whose audio front end runs AEC with a speaker reference, so the mic stream during TTS is mostly free of Skwoop's voice.
2. While TTS plays, keep the mic open and stream Opus as in `realtime` listening. Do not gate the mic on `tts start`.
3. On `tts stop`, flush the decoder and the I2S queue right away. Don't play out the buffered frames.
4. Optional client detection (instead of 1–2): run the same thresholds on the AFE output against the I2S output level, keep a 400 ms ring, and send `listen start auto barge:true` plus the ring.
5. Boards without AEC: keep the current behavior (wake word or button only).

Bridge side, nothing changes per board. Test with `tests/test_barge.py` (`test_sim_pod_*`, server-detection pod sim) before any flash.

## Rollback

- Web pod: `?barge=0`, or redeploy the previous `webpod/static/app.js`.
- Bridge: pods that don't declare `barge_in` get the old path.
