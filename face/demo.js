"use strict";

const STATES = ["idle", "listening", "thinking", "speaking"];
const SERVER_MSG = "Serve with python3 -m http.server";
const EXPR_MS = 2400;

function motionOk() {
  return !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function inlineData() {
  const data = window.FACE_DATA;
  if (data && typeof data === "object") return data;

  return null;
}

// file:// cannot fetch. Served pages prefer face-data.json, then the inline copy.
async function loadFaceData() {
  if (location.protocol === "file:") {
    const data = inlineData();
    if (data) return data;

    throw new Error("face data missing");
  }

  try {
    const res = await fetch("face-data.json");
    if (!res.ok) throw new Error("face-data.json " + res.status);

    return await res.json();
  } catch (err) {
    const data = inlineData();
    if (data) return data;

    throw err;
  }
}

function showNeedServer() {
  const note = document.getElementById("need-server");
  if (note) note.hidden = false;

  const canvas = document.getElementById("face");
  if (!canvas) return;

  canvas.setAttribute("aria-label", SERVER_MSG);
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  ctx.fillStyle = "#111";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#eee";
  ctx.font = "16px sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(SERVER_MSG, canvas.width / 2, canvas.height / 2);
}

function drawSnapshot(ctx, st, useDot) {
  if (useDot && typeof drawDot === "function") drawDot(ctx, st, st.label);
  else SkwoopFace.drawRobot(ctx, st, st.label);
}

async function main() {
  const q = new URLSearchParams(location.search);
  const useDot = q.get("face") === "dot";
  const usePad = q.get("pad") === "1";
  let data;
  try {
    data = await loadFaceData();
  } catch (err) {
    console.error(err);
    showNeedServer();

    return;
  }

  SkwoopFace.init(data);
  const canvas = document.getElementById("face");
  const ctx = canvas.getContext("2d");
  const sim = new SkwoopFace.FaceSim("robot");
  let face = "idle";
  sim.settleOn(face);

  const buttons = [...document.querySelectorAll("[data-state]")];
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const next = btn.getAttribute("data-state");
      if (!STATES.includes(next)) return;

      face = next;
      sim.setFace(face);
      buttons.forEach((b) => b.setAttribute("aria-pressed", b === btn ? "true" : "false"));
    });
  });

  let pad = null;
  let exprNames = [];
  let exprIndex = 0;
  let exprAt = 0;
  const exprLabel = document.getElementById("expr");
  if (usePad && window.PadFace && location.protocol !== "file:") {
    try {
      const res = await fetch("expressions.json");
      if (!res.ok) throw new Error("expressions.json " + res.status);

      const expressions = await res.json();
      PadFace.install(data, expressions);
      pad = new PadFace.PadFace(sim, SkwoopFace.Smoother);
      exprNames = PadFace.names();
      if (exprNames.length) {
        pad.hold(PadFace.weightsFromMix({ [exprNames[0]]: 1 }, 1));
        sim.settleOn(face);
        if (exprLabel) {
          exprLabel.hidden = false;
          exprLabel.textContent = exprNames[0];
        }
      }
    } catch (err) {
      console.error(err);
    }
  }

  let aria = "";
  function applyAria(st) {
    const kind = useDot ? "dot" : "robot";
    const text = `Skwoop ${kind} face, current state: ${st.label || face}`;
    if (text === aria) return;

    aria = text;
    canvas.setAttribute("aria-label", text);
  }

  function paint(now) {
    if (pad && exprNames.length && motionOk() && exprAt !== 0 && now - exprAt >= EXPR_MS) {
      exprIndex = (exprIndex + 1) % exprNames.length;
      exprAt = now;
      const name = exprNames[exprIndex];
      pad.hold(PadFace.weightsFromMix({ [name]: 1 }, 1));
      if (exprLabel) exprLabel.textContent = name;
    } else if (pad && exprAt === 0) {
      exprAt = now;
    }

    const move = motionOk();
    const st = sim.step(now, { blinks: move, bob: move });
    drawSnapshot(ctx, st, useDot);
    applyAria(st);
    requestAnimationFrame(paint);
  }

  paint(performance.now());
}

main().catch((err) => {
  console.error(err);
  showNeedServer();
});
