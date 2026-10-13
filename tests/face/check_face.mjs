// Headless Chrome check for face/. Invoked by check_face.py.
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
const faceDir = path.resolve(process.argv[2] || "");
const SERVER_MSG = "Serve with python3 -m http.server";

const FACTS = `(() => {
  const canvas = document.getElementById("face");
  const ctx = canvas.getContext("2d");
  const img = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
  let n = 0;
  for (let i = 0; i < img.length; i += 4) {
    if (img[i] > 12 || img[i + 1] > 12 || img[i + 2] > 12) n += 1;
  }
  const note = document.getElementById("need-server");
  const text = note ? note.textContent : "";
  const shown = !!(note && !note.hidden && text.includes(${JSON.stringify(SERVER_MSG)}));
  const h1 = document.querySelector("h1");
  const expr = document.getElementById("expr");
  return {
    n: n,
    shown: shown,
    aria: canvas.getAttribute("aria-label") || "",
    h1: h1 ? h1.textContent : "",
    expr: expr ? expr.textContent : "",
  };
})()`;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function walkForChrome(root) {
  if (!root || !existsSync(root)) return "";

  const stack = [root];
  while (stack.length) {
    const dir = stack.pop();
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const ent of entries) {
      const full = path.join(dir, ent.name);
      if (ent.isDirectory()) stack.push(full);
      else if (ent.name === "chrome-headless-shell") return full;
    }
  }

  return "";
}

function findChrome() {
  const fromEnv = process.env.CHROME_PATH || "";
  if (fromEnv && existsSync(fromEnv)) return fromEnv;

  const walked = walkForChrome(process.env.CHROME_HEADLESS_ROOT || "");
  if (walked) return walked;

  for (const bin of [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ]) {
    if (existsSync(bin)) return bin;
  }

  throw new Error("chrome not found (set CHROME_PATH)");
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = require("node:net").createServer();
    srv.listen(0, "127.0.0.1", () => {
      const port = srv.address().port;
      srv.close((err) => (err ? reject(err) : resolve(port)));
    });
  });
}

function startServer(dir) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      "python3",
      ["-u", "-m", "http.server", "0", "--bind", "127.0.0.1"],
      { cwd: dir, stdio: ["ignore", "pipe", "pipe"] },
    );
    let buf = "";
    let settled = false;
    const take = (chunk) => {
      buf += chunk.toString();
      const match = buf.match(/port\s+(\d+)/i);
      if (!match || settled) return;

      settled = true;
      resolve({ child, port: Number(match[1]) });
    };
    child.stdout.on("data", take);
    child.stderr.on("data", take);
    child.on("error", (err) => {
      if (!settled) reject(err);
    });
    child.on("exit", (code) => {
      if (!settled) reject(new Error("http.server exited " + code));
    });
  });
}

function openWs(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    const timer = setTimeout(() => reject(new Error("websocket timeout")), 5000);
    ws.addEventListener("open", () => {
      clearTimeout(timer);
      resolve(ws);
    });
    ws.addEventListener("error", () => {
      clearTimeout(timer);
      reject(new Error("websocket failed"));
    });
  });
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.next = 0;
    this.pending = new Map();
    this.listeners = [];
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const pending = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) rejectMsg(pending.reject, msg.error);
        else pending.resolve(msg.result || {});
      } else if (msg.method) {
        for (const listener of this.listeners) {
          if (listener.sessionId && listener.sessionId !== msg.sessionId) continue;

          listener.fn(msg);
        }
      }
    });
  }

  on(sessionId, fn) {
    this.listeners.push({ sessionId, fn });
  }

  send(method, params = {}, sessionId) {
    const id = ++this.next;
    const msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;

    this.ws.send(JSON.stringify(msg));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
  }
}

function rejectMsg(reject, error) {
  reject(new Error(error.message || JSON.stringify(error)));
}

function waitEvent(cdp, sessionId, method, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timeout " + method)), ms);
    cdp.on(sessionId, (msg) => {
      if (msg.method !== method) return;

      clearTimeout(timer);
      resolve(msg.params || {});
    });
  });
}

function smokeSim() {
  const SkwoopFace = require(path.join(faceDir, "face.js"));
  const data = JSON.parse(require("node:fs").readFileSync(
    path.join(faceDir, "face-data.json"),
    "utf8",
  ));
  SkwoopFace.init(JSON.parse(JSON.stringify(data)));
  const sim = new SkwoopFace.FaceSim("robot");
  for (const name of ["idle", "listening", "thinking", "speaking"]) {
    sim.settleOn(name);
    const st = sim.step(1000);
    if (!st || typeof st.eye_w !== "number") throw new Error("bad snapshot " + name);
  }

  const PadFace = require(path.join(faceDir, "pad-face.js"));
  const expr = JSON.parse(require("node:fs").readFileSync(
    path.join(faceDir, "expressions.json"),
    "utf8",
  ));
  const copy = JSON.parse(JSON.stringify(data));
  SkwoopFace.init(copy);
  PadFace.install(copy, expr);
  const sim2 = new SkwoopFace.FaceSim("robot");
  const pad = new PadFace.PadFace(sim2, SkwoopFace.Smoother);
  const names = PadFace.names();
  if (!names.length) throw new Error("no expressions");

  pad.hold(PadFace.weightsFromMix({ [names[0]]: 1 }, 1));
  sim2.settleOn("idle");
  const st = sim2.step(50);
  if (typeof st.mouth_w !== "number") throw new Error("pad snapshot");

  console.log("sim ok", names.join(","));
}

async function waitDevtools(port) {
  for (let i = 0; i < 50; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (res.ok) return await res.json();
    } catch {
      // Chrome is still starting.
    }
    await sleep(100);
  }

  throw new Error("chrome devtools did not start");
}

async function readPage(cdp, sessionId) {
  await cdp.send(
    "Runtime.evaluate",
    { expression: "new Promise((resolve) => setTimeout(resolve, 400))", awaitPromise: true },
    sessionId,
  );
  const ev = await cdp.send(
    "Runtime.evaluate",
    { expression: FACTS, returnByValue: true },
    sessionId,
  );
  if (ev.exceptionDetails) {
    throw new Error(ev.exceptionDetails.text || "page evaluate failed");
  }

  return ev.result.value;
}

async function openPage(browser, url) {
  const { targetId } = await browser.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await browser.send("Target.attachToTarget", {
    targetId,
    flatten: true,
  });
  const errors = [];
  browser.on(sessionId, (msg) => {
    if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
      const args = msg.params.args || [];
      errors.push(args.map((arg) => arg.value || arg.description || arg.type).join(" "));
    }
    if (msg.method === "Runtime.exceptionThrown") {
      const details = msg.params.exceptionDetails || {};
      const ex = details.exception || {};
      errors.push((details.text || "exception") + " " + (ex.description || ""));
    }
  });
  await browser.send("Runtime.enable", {}, sessionId);
  await browser.send("Page.enable", {}, sessionId);
  const loaded = waitEvent(browser, sessionId, "Page.loadEventFired", 8000);
  await browser.send("Page.navigate", { url }, sessionId);
  await loaded;

  return { targetId, sessionId, errors };
}

function fail(errors, label) {
  console.error(label);
  for (const err of errors) console.error("  " + err);

  return false;
}

async function main() {
  if (!faceDir || !existsSync(path.join(faceDir, "index.html"))) {
    throw new Error("face directory missing");
  }

  smokeSim();
  const bin = findChrome();
  const server = await startServer(faceDir);
  const devPort = await freePort();
  const userData = mkdtempSync(path.join(tmpdir(), "skwoop-face-"));
  const chrome = spawn(bin, [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--disable-extensions",
    "--disable-background-timer-throttling",
    "--allow-file-access-from-files",
    `--remote-debugging-port=${devPort}`,
    `--user-data-dir=${userData}`,
    "about:blank",
  ], { stdio: "ignore" });

  let ok = true;
  try {
    const version = await waitDevtools(devPort);
    const ws = await openWs(version.webSocketDebuggerUrl);
    const browser = new Cdp(ws);
    const base = `http://127.0.0.1:${server.port}/`;

    const idle = await openPage(browser, base);
    const idleFacts = await readPage(browser, idle.sessionId);
    console.log("http", idleFacts.n, idleFacts.aria);
    if (idle.errors.length) ok = fail(idle.errors, "console errors on http");
    if (!(idleFacts.n > 100)) ok = fail([], "canvas looks black");
    if (idleFacts.shown) ok = fail([], "server hint visible while served");
    if (!idleFacts.h1.includes("Skwoop")) ok = fail([], "missing h1");
    if (!idleFacts.aria.includes("current state: Ready")) ok = fail([], "aria not Ready");

    await browser.send(
      "Runtime.evaluate",
      { expression: "document.querySelector('[data-state=\"speaking\"]').click()" },
      idle.sessionId,
    );
    const speaking = await readPage(browser, idle.sessionId);
    console.log("speaking", speaking.aria);
    if (idle.errors.length) ok = fail(idle.errors, "console errors after speaking");
    if (!speaking.aria.includes("Speaking")) ok = fail([], "state button did not switch");
    if (!(speaking.n > 100)) ok = fail([], "speaking canvas looks black");
    await browser.send("Target.closeTarget", { targetId: idle.targetId });

    const dot = await openPage(browser, base + "?face=dot");
    const dotFacts = await readPage(browser, dot.sessionId);
    console.log("dot", dotFacts.n, dotFacts.aria);
    if (dot.errors.length) ok = fail(dot.errors, "console errors on ?face=dot");
    if (!(dotFacts.n > 100)) ok = fail([], "dot canvas looks black");
    if (!dotFacts.aria.includes("dot")) ok = fail([], "dot aria missing");
    await browser.send("Target.closeTarget", { targetId: dot.targetId });

    const pad = await openPage(browser, base + "?pad=1");
    const padFacts = await readPage(browser, pad.sessionId);
    console.log("pad", padFacts.expr);
    if (pad.errors.length) ok = fail(pad.errors, "console errors on ?pad=1");
    if (!padFacts.expr) ok = fail([], "pad mode did not show an expression");
    if (!(padFacts.n > 100)) ok = fail([], "pad canvas looks black");
    await browser.send("Target.closeTarget", { targetId: pad.targetId });

    const fileUrl = pathToFileURL(path.join(faceDir, "index.html")).href;
    const filePage = await openPage(browser, fileUrl);
    const fileFacts = await readPage(browser, filePage.sessionId);
    console.log("file", fileFacts.n, fileFacts.shown, fileFacts.aria);
    if (filePage.errors.length) ok = fail(filePage.errors, "console errors on file://");
    if (!(fileFacts.n > 100 || fileFacts.shown)) {
      ok = fail([], "file:// neither drew nor showed the server hint");
    }
    await browser.send("Target.closeTarget", { targetId: filePage.targetId });
    ws.close();
  } finally {
    chrome.kill("SIGTERM");
    server.child.kill("SIGTERM");
    rmSync(userData, { recursive: true, force: true });
  }

  if (!ok) process.exit(1);

  console.log("face check ok");
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
