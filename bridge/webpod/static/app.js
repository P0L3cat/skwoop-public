// Skwoop web pod page: the face (face.js), push-to-talk mic capture, bridge audio playback, and the /pod WebSocket
// to webpod/server.py, which relays to the bridge as a pod named web-pod-<name>.
(function () {
  "use strict";

  const IN_RATE = 16000, OUT_RATE = 24000, TICK_MS = 50;
  const $ = (id) => document.getElementById(id);
  const canvas = $("face"), ctx = canvas.getContext("2d");
  const talk = $("talk"), statusEl = $("status"), caption = $("caption"), heard = $("heard"), nameEl = $("name");

  const store = {
    get: (k, d) => localStorage.getItem("webpod." + k) || d,
    set: (k, v) => localStorage.setItem("webpod." + k, v),
  };

  const urlKey = new URLSearchParams(location.search).get("k");
  if (urlKey) {
    store.set("key", urlKey);
    history.replaceState(null, "", location.pathname);
  }

  function defaultName() {
    const ua = navigator.userAgent;
    const kind = /iPad/.test(ua) ? "ipad" : /iPhone/.test(ua) ? "iphone" : /Android/.test(ua) ? "android"
      : /Macintosh/.test(ua) ? (navigator.maxTouchPoints > 1 ? "ipad" : "mac") : "browser";

    return `${kind}-${Math.random().toString(16).slice(2, 6)}`;
  }

  nameEl.value = store.get("name", defaultName());
  store.set("name", nameEl.value);

  // ------------------------------------------------------------------ face
  let style = store.get("style", "robot");
  if (style !== "robot") style = "robot";
  let sim = null, snap = null, face = "offline", tone = 0, title = "Offline";
  // Emotion engine (bridge SKWOOP_TONE=pad): the PAD expression layer. It switches on with the
  // first "llm" message that carries a pad, so with SKWOOP_TONE=keyword the face draws the keyword pose.
  let padFace = null, padOn = false;

  function setStatus(text, bad) {
    statusEl.textContent = text;
    statusEl.classList.toggle("bad", Boolean(bad));
  }

  function setFace(next, label) {
    face = next;
    if (sim) sim.setFace(next);

    title = label !== undefined ? label : (sim ? SkwoopFace.FaceData.styles[next].label : "");
  }

  function makeSim() {
    sim = new SkwoopFace.FaceSim(style, (Math.random() * 0xffffffff) >>> 0);
    padFace = padOn && style === "robot" ? new PadFace.PadFace(sim, SkwoopFace.Smoother) : null;
    sim.settleOn(face, [0, 0]);
  }

  function tickFace(now) {
    if (!sim) return;

    snap = sim.step(now, { tone });
  }

  function fitCanvas() {           // a sharp face: back the canvas with device pixels, draw in the pod's 466 px space
    const px = Math.round(canvas.clientWidth * Math.min(3, window.devicePixelRatio || 1));
    if (px > 0 && canvas.width !== px) {
      canvas.width = px;
      canvas.height = px;
    }
  }

  function draw() {
    if (snap) {
      fitCanvas();
      ctx.setTransform(canvas.width / 466, 0, 0, canvas.height / 466, 0, 0);
      ctx.clearRect(0, 0, 466, 466);
      SkwoopFace.drawRobot(ctx, snap, title);
    }

    requestAnimationFrame(draw);
  }

  function selectStyle(next) {
    style = next;
    store.set("style", next);
    for (const b of document.querySelectorAll("#style button")) b.classList.toggle("sel", b.dataset.style === next);

    if (sim) makeSim();
  }

  async function loadFace() {
    const data = await (await fetch("/static/face-data.json")).json();
    SkwoopFace.init(data);
    SkwoopFace.FaceData = data;
    PadFace.install(data, await (await fetch("/static/expressions.json")).json());
    try { await document.fonts.load("500 20px Montserrat"); } catch (e) { /* the face still draws in a fallback font */ }

    makeSim();
    const t0 = performance.now();
    setInterval(() => tickFace(performance.now() - t0 + TICK_MS), TICK_MS);
    requestAnimationFrame(draw);
  }

  // ------------------------------------------------------------------ speaker: 24 kHz PCM from the bridge
  let audio = null, playAt = 0, playing = [], speakingTimer = 0, audioBytes = 0, out = null, refBuf = null;

  function audioContext() {
    if (!audio) {
      audio = new (window.AudioContext || window.webkitAudioContext)();
      out = audio.createAnalyser();                    // speech goes through it: the barge-in echo reference
      out.fftSize = 1024;
      refBuf = new Float32Array(out.fftSize);
      out.connect(audio.destination);
    }
    if (audio.state === "suspended") audio.resume();

    return audio;
  }

  function playPcm(buf) {
    const ac = audioContext();
    audioBytes += buf.byteLength;
    const pcm = new Int16Array(buf);
    if (!pcm.length) return;

    const ab = ac.createBuffer(1, pcm.length, OUT_RATE);
    const ch = ab.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;

    const src = ac.createBufferSource();
    src.buffer = ab;
    src.connect(out);
    playAt = Math.max(playAt, ac.currentTime + 0.06);
    src.start(playAt);
    playAt += ab.duration;
    playing.push(src);
    src.onended = () => { playing = playing.filter((s) => s !== src); };
  }

  function stopPlayback() {
    for (const s of playing) { try { s.stop(); } catch (e) { /* already stopped */ } }

    playing = [];
    playAt = 0;
  }

  function afterPlayback(fn) {           // run fn once the queued speech has actually played out
    clearTimeout(speakingTimer);
    const left = audio ? Math.max(0, playAt - audio.currentTime) : 0;
    speakingTimer = setTimeout(fn, left * 1000 + 120);
  }

  // ------------------------------------------------------------------ barge-in: Alex talks over Skwoop
  // Same rules as barge.py: per 20 ms mic frame, the level against the noise floor and against what is playing.
  // The browser's echo canceller removes most of Skwoop's voice; what is left is learned per reply (WARMUP_MS)
  // and must be beaten by ECHO_DB. VOICE_MS of voice stops playback and opens a listen with ~400 ms of preroll.
  const SILENT_DB = -55, FLOOR_DB = 12, ECHO_DB = 10, VOICE_MS = 240, WARMUP_MS = 300, BARGE_FRAME = 320;
  const bargeOn = new URLSearchParams(location.search).get("barge") !== "0";
  const barge = { floor: -60, echo: -20, elapsed: 0, acc: 0, frame: new Int16Array(BARGE_FRAME), fill: 0,
    pre: [], preLen: 0, refs: [], muted: false, listen: false, timer: 0, count: 0 };

  function dbOf(x, full) {
    let e = 0;
    for (let i = 0; i < x.length; i++) e += x[i] * x[i];

    return 10 * Math.log10(e / Math.max(1, x.length) / (full * full) + 1e-12);
  }

  function bargeReset() { barge.echo = -20; barge.elapsed = 0; barge.acc = 0; }

  function speakingNow() { return !!audio && playing.length > 0 && playAt > audio.currentTime; }

  function refDb() {
    const now = performance.now();
    out.getFloatTimeDomainData(refBuf);
    barge.refs.push([now, dbOf(refBuf, 1)]);
    barge.refs = barge.refs.filter(([t]) => t >= now - 400);

    return barge.refs.reduce((m, [, d]) => Math.max(m, d), -120);
  }

  function bargePush(micDb, ref, ms) {          // true: Alex is talking over Skwoop (barge.py push)
    barge.elapsed += ms;
    const playingNow = ref > SILENT_DB, rel = micDb - ref;
    if (barge.elapsed <= WARMUP_MS) {
      if (playingNow) barge.echo = barge.elapsed <= ms ? rel : Math.max(barge.echo, 0.7 * barge.echo + 0.3 * rel);
      return false;
    }
    const voice = micDb > barge.floor + FLOOR_DB && (!playingNow || rel > barge.echo + ECHO_DB);
    if (!voice) {
      if (playingNow) barge.echo += 0.05 * (rel - barge.echo);
      else barge.floor = micDb < barge.floor ? micDb : barge.floor + 0.02 * (micDb - barge.floor);
    }
    barge.acc = voice ? barge.acc + ms : Math.max(0, barge.acc - ms / 2);
    if (barge.acc >= VOICE_MS) { barge.acc = 0; return true; }

    return false;
  }

  function bargeWatch(pcm) {                    // mic audio while not listening: keep a preroll, watch for voice
    barge.pre.push(pcm);
    barge.preLen += pcm.length;
    while (barge.preLen - barge.pre[0].length >= 6400) barge.preLen -= barge.pre.shift().length;   // ~400 ms
    if (!bargeOn || !speakingNow() || barge.muted) { barge.fill = 0; return; }

    for (let i = 0; i < pcm.length; i++) {
      barge.frame[barge.fill++] = pcm[i];
      if (barge.fill === BARGE_FRAME) {
        barge.fill = 0;
        if (bargePush(dbOf(barge.frame, 32768), refDb(), 20)) { bargeIn(); return; }
      }
    }
  }

  function bargeIn() {
    stopPlayback();
    barge.count += 1;
    barge.muted = true;                          // the cut reply may still have audio in flight: drop it
    barge.listen = true;
    listening = true;
    talk.classList.add("on");
    talk.textContent = "Listening…";
    caption.textContent = "";
    setFace("listening");
    sendJson({ type: "listen", state: "start", mode: "auto", barge: true });
    for (const c of barge.pre) ws.send(c.buffer);
    barge.pre = [];
    barge.preLen = 0;
    clearTimeout(barge.timer);
    barge.timer = setTimeout(bargeDone, 10000);  // nothing was heard: give the page back
  }

  function bargeDone() {
    clearTimeout(barge.timer);
    barge.muted = false;
    if (!barge.listen) return;
    barge.listen = false;
    listening = false;
    talk.classList.remove("on");
    talk.textContent = mode === "hold" ? "Hold to talk" : "Tap to talk";
  }

  // ------------------------------------------------------------------ mic: 16 kHz 16-bit PCM to the sidecar
  let mic = null, listening = false;

  const WORKLET = `class Tap extends AudioWorkletProcessor {
    process(inputs) { const ch = inputs[0] && inputs[0][0]; if (ch) this.port.postMessage(ch.slice(0)); return true; }
  }
  registerProcessor("tap", Tap);`;

  function makeResampler(fromRate) {
    const step = fromRate / IN_RATE;
    let pos = 0, prev = 0;

    return function (input) {   // linear interpolation, carried across chunks
      const out = [];
      while (pos < input.length) {
        const i = Math.floor(pos), f = pos - i;
        const a = i === 0 ? prev : input[i - 1], b = input[i];
        const x = a + (b - a) * (f === 0 ? 1 : f);
        out.push(Math.max(-1, Math.min(1, x)) * 32767);
        pos += step;
      }
      pos -= input.length;
      prev = input[input.length - 1];

      return Int16Array.from(out);
    };
  }

  async function openMic() {
    if (mic) return mic;

    const ac = audioContext();
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const source = ac.createMediaStreamSource(stream);
    const resample = makeResampler(ac.sampleRate);
    const send = (floats) => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const pcm = resample(floats);
      if (listening) ws.send(pcm.buffer);
      else bargeWatch(pcm);
    };
    if (ac.audioWorklet) {
      await ac.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" })));
      const node = new AudioWorkletNode(ac, "tap");
      node.port.onmessage = (e) => send(e.data);
      source.connect(node);
      mic = { stream, node };
    } else {
      const node = ac.createScriptProcessor(2048, 1, 1);
      node.onaudioprocess = (e) => send(e.inputBuffer.getChannelData(0));
      source.connect(node);
      node.connect(ac.destination);
      mic = { stream, node };
    }

    return mic;
  }

  // ------------------------------------------------------------------ the /pod socket
  let ws = null, retry = 1000, mode = store.get("mode", "hold");

  function sendJson(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  function connect() {
    setStatus("connecting…");
    ws = new WebSocket(`wss://${location.host}/pod`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => sendJson({ type: "hello", device: nameEl.value, key: store.get("key", "") });
    ws.onmessage = (e) => {
      if (typeof e.data !== "string") {
        if (!barge.muted) playPcm(e.data);

        return;
      }

      let msg;
      try { msg = JSON.parse(e.data); } catch (err) { return; }

      onEvent(msg);
    };
    ws.onclose = () => {
      talk.disabled = true;
      talk.textContent = "Reconnecting…";
      setFace("offline");
      setStatus("offline", true);
      setTimeout(connect, retry);
      retry = Math.min(retry * 2, 15000);
    };
  }

  function onEvent(msg) {
    if (barge.muted) {                           // after a barge-in: the cut reply's last events are stale
      if (msg.type === "tts" || msg.type === "llm") return;
      if (msg.type === "stt") bargeDone();
    }
    // hands-free: the bridge's VAD ended the turn, so its reply (stt / tts) ends our listening state
    if (mode === "tap" && listening && (msg.type === "stt" || msg.type === "tts")) stopTalking();

    switch (msg.type) {
      case "webpod":
        if (msg.error) {
          setStatus(msg.error, true);
        } else if (msg.connected) {
          retry = 1000;
          setStatus(`online as ${msg.device}`);
          talk.disabled = false;
          talk.textContent = mode === "hold" ? "Hold to talk" : "Tap to talk";
          setFace("idle");
        }
        break;
      case "stt":
        heard.textContent = msg.text ? `“${msg.text}”` : "";
        setFace("thinking");
        break;
      case "llm":
        if (msg.emotion === "thinking") {
          setFace("thinking");
        } else {
          tone = ["happy", "laughing", "funny", "loving", "winking", "cool", "delicious"].includes(msg.emotion) ? 1
            : ["sad", "crying"].includes(msg.emotion) ? 2 : 0;
          // per-sentence {v, a, d} from the bridge's mood model (or today's 3 tones placed on PAD once it's on)
          if (msg.pad && !padOn && style === "robot") {
            padOn = true;
            padFace = new PadFace.PadFace(sim, SkwoopFace.Smoother);
          }
          if (padFace) padFace.reply(msg.pad ?? PadFace.toneFallback(tone));
        }
        break;
      case "tts":
        if (msg.state === "start") {
          clearTimeout(speakingTimer);
          bargeReset();
          setFace("speaking");
        } else if (msg.state === "sentence_start") {
          // a thinking filler ("Hm, looking into this.") keeps the thinking face while it plays
          caption.textContent = msg.text || "";
          caption.classList.toggle("filler", msg.filler === true);
          setFace(msg.filler === true ? "thinking" : "speaking");
        } else if (msg.state === "stop") {
          afterPlayback(() => { if (!listening) { setFace("idle"); tone = 0; } });
        }
        break;
      case "alert":
        caption.textContent = msg.message || msg.status || "";
        setFace("alert");
        afterPlayback(() => setFace("idle"));
        break;
      case "listen":
        if (msg.state === "start") {
          listening = true;
          if (padFace) padFace.resetListen();
          setFace("listening");
        }
        break;
      case "mood":                                                     // the user's mood while they talk
        if (padFace && msg.phase === "listen") padFace.listen(msg);
        break;
      default:
        break;
    }
  }

  async function startTalking() {
    if (talk.disabled || listening) return;

    try {
      await openMic();
    } catch (e) {
      setStatus("microphone blocked: allow it for this site (HTTPS needed)", true);

      return;
    }

    stopPlayback();
    listening = true;
    talk.classList.add("on");
    talk.textContent = mode === "hold" ? "Listening… release to send" : "Listening…";
    heard.textContent = "";
    caption.textContent = "";
    setFace("listening");
    sendJson({ type: "listen", state: "start", mode: mode === "hold" ? "manual" : "auto" });
  }

  function stopTalking() {
    if (!listening) return;

    listening = false;
    talk.classList.remove("on");
    talk.textContent = mode === "hold" ? "Hold to talk" : "Tap to talk";
    if (mode === "hold") {
      sendJson({ type: "listen", state: "stop", mode: "manual" });
      setFace("thinking");
    }
  }

  talk.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    if (mode === "tap" && listening) { stopTalking(); sendJson({ type: "listen", state: "stop", mode: "auto" }); return; }

    startTalking();
  });
  for (const ev of ["pointerup", "pointercancel", "pointerleave"]) {
    talk.addEventListener(ev, () => { if (mode === "hold") stopTalking(); });
  }

  for (const b of document.querySelectorAll("#style button")) b.addEventListener("click", () => selectStyle(b.dataset.style));
  for (const b of document.querySelectorAll("#mode button")) {
    b.addEventListener("click", () => {
      mode = b.dataset.mode;
      store.set("mode", mode);
      for (const x of document.querySelectorAll("#mode button")) x.classList.toggle("sel", x === b);
      if (!talk.disabled) talk.textContent = mode === "hold" ? "Hold to talk" : "Tap to talk";
    });
  }
  for (const b of document.querySelectorAll("#mode button")) b.classList.toggle("sel", b.dataset.mode === mode);

  nameEl.addEventListener("change", () => {
    store.set("name", nameEl.value);
    if (ws) ws.close();
  });

  // Smoke-test hook: one utterance from a 16 kHz PCM buffer through the same socket path as the mic (a headless
  // browser has no microphone). Returns once the frames and "listen stop" are sent.
  window.webpodSpeakPcm = function (int16Buffer) {
    stopPlayback();
    audioContext();
    sendJson({ type: "listen", state: "start", mode: "manual" });
    setFace("listening");
    for (let off = 0; off < int16Buffer.byteLength; off += 1920) ws.send(int16Buffer.slice(off, off + 1920));

    sendJson({ type: "listen", state: "stop", mode: "manual" });
    setFace("thinking");
  };

  window.webpodState = () => ({ face, style, title, caption: caption.textContent, heard: heard.textContent,
    status: statusEl.textContent, audioBytes, queuedAudioS: audio ? Math.max(0, playAt - audio.currentTime) : 0,
    barges: barge.count, bargeEcho: barge.echo, bargeFloor: barge.floor });

  // Test hook: feed 16 kHz mic PCM through the barge-in path, as the mic worklet would (headless browsers have no mic).
  window.webpodMicPcm = function (int16Buffer) {
    const all = new Int16Array(int16Buffer);
    for (let off = 0; off < all.length; off += 320) {
      const c = all.slice(off, off + 320);
      if (listening) ws.send(c.buffer);
      else bargeWatch(c);
    }
  };

  selectStyle(style);
  loadFace().then(connect);
})();
