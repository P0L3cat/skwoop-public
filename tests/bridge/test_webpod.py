"""Web pod sidecar (webpod/server.py): browser messages parsed at the boundary, PCM framing, the relay to a fake
bridge on lean_http (port 0), pages (/ , /static/, /ca.crt, /healthz), and face.js (run under Node) against the
Python sim it ports. No network, no keys, no TLS (plain HTTP here; TLS is only the ssl_context in main())."""
import asyncio
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
from pathlib import Path

import pytest  # noqa: E402
from fake_webpod_bridge import FakeBridge  # noqa: E402
from lean_http import Application, AppRunner, ClientSession, TCPSite  # noqa: E402
from lean_ws import WSMsgType  # noqa: E402

from webpod import server as wp  # noqa: E402

TOOLS = Path(os.environ.get("SKWOOP_TOOLS", ""))
NODE = shutil.which("node")
STATIC = Path(__file__).resolve().parents[2] / "bridge" / "webpod" / "static"


def _cfg(bridge_port: int, key: str = "", token: str = "", tls_dir: Path | None = None) -> wp.WebPodConfig:
    return wp.WebPodConfig(host="127.0.0.1", port=0, tls_dir=tls_dir or (Path(__file__).parent / "no-tls-here"),
                           access_key=key,
                           bridge_url=f"ws://127.0.0.1:{bridge_port}/xiaozhi/v1/", device_token=token)


def _obj(text: str) -> dict[str, object]:
    raw = json.loads(text)
    assert isinstance(raw, dict)

    return {str(k): v for k, v in raw.items()}


# ------------------------------------------------------------------ parsing
def test_parse_hello_and_device_ids():
    h = wp.parse_hello('{"type": "hello", "device": "Alex\'s iPhone 15", "key": "k1"}')
    assert h == wp.BrowserHello("alex-s-iphone-15", "k1") and h.device_id == "web-pod-alex-s-iphone-15"
    assert wp.device_slug("x" * 40) == "x" * 24
    bad_hellos = (
        "[]",
        '{"type": "listen"}',
        '{"type": "hello", "device": 7}',
        '{"type": "hello", "device": "!!"}',
        "nope",
    )
    for bad in bad_hellos:
        with pytest.raises(wp.BadMessage):
            wp.parse_hello(bad)


def test_parse_commands_map_to_the_pod_protocol():
    assert wp.to_bridge(wp.parse_command('{"type": "listen", "state": "start"}')) == {
        "type": "listen", "state": "start", "mode": "manual"}
    stopped = wp.to_bridge(wp.parse_command('{"type": "listen", "state": "stop", "mode": "auto"}'))
    assert stopped["mode"] == "auto"
    assert wp.to_bridge(wp.parse_command('{"type": "abort"}')) == {"type": "abort"}
    assert wp.to_bridge(wp.parse_command('{"type": "inbox"}')) == {"type": "inbox", "action": "play"}
    bad_cmds = (
        '{"type": "listen", "state": "detect"}',
        '{"type": "mcp"}',
        '{"type": "inbox", "action": "rm"}',
        "1",
    )
    for bad in bad_cmds:
        with pytest.raises(wp.BadMessage):
            wp.parse_command(bad)


def test_bridge_events_are_filtered():
    event = wp.bridge_event('{"type": "tts", "state": "start", "session_id": "s"}')
    assert event is not None and json.loads(event)["state"] == "start"
    assert wp.bridge_event('{"type": "mcp", "payload": {}}') is None
    assert wp.bridge_event("not json") is None and wp.bridge_event("[1]") is None


def test_config_from_env():
    cfg = wp.config_from_env({"SKWOOP_PORT": "8003", "WEBPOD_KEY": " abc ", "SKWOOP_DEVICE_TOKEN": "t"})
    assert cfg.port == 8443 and cfg.access_key == "abc" and cfg.bridge_url == "ws://127.0.0.1:8003/xiaozhi/v1/"
    with pytest.raises(SystemExit):
        wp.config_from_env({"WEBPOD_PORT": "eighty"})


def test_pcm_framer():
    f = wp.PcmFramer(frame_bytes=8)
    assert f.push(b"\x01\x00" * 3) == []
    assert f.push(b"\x02\x00" * 3) == [b"\x01\x00" * 3 + b"\x02\x00"]
    assert f.flush() == b"\x02\x00" * 2 + b"\x00" * 4 and f.flush() is None
    with pytest.raises(wp.BadMessage):
        f.push(b"\x01")


def test_opus_round_trip():
    codec = wp.OpusCodec()
    tone = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(960))
    packet = codec.encode(tone)
    assert 0 < len(packet) < len(tone)


# ------------------------------------------------------------------ relay against a recording fake bridge
class FakeCodec:
    def encode(self, pcm16k: bytes) -> bytes:
        return b"OPUS" + len(pcm16k).to_bytes(2, "big")

    def decode(self, opus24k: bytes) -> bytes:
        return b"\x07\x00" * 1440


async def _serve(app: Application) -> tuple[AppRunner, int]:
    runner = AppRunner(app)
    await runner.setup()
    await TCPSite(runner, "127.0.0.1", 0).start()

    return runner, runner.addresses[-1][1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))

        return int(sock.getsockname()[1])


async def _browser_turn(sidecar_port: int, hello: dict[str, str], pcm: bytes, until: str = "stop",
                        timeout: float = 10) -> tuple[list[dict[str, object]], list[bytes]]:
    got_text: list[dict[str, object]] = []
    got_audio: list[bytes] = []
    async with ClientSession() as http:
        async with http.ws_connect(f"ws://127.0.0.1:{sidecar_port}/pod") as ws:
            await ws.send_str(json.dumps(hello))
            first_msg = await ws.receive(timeout=timeout)
            if first_msg.type != WSMsgType.TEXT or not isinstance(first_msg.data, str):
                return got_text, got_audio

            first = _obj(first_msg.data)
            got_text.append(first)
            if not first.get("connected"):
                return got_text, got_audio

            await ws.send_str(json.dumps({"type": "listen", "state": "start", "mode": "manual"}))
            for off in range(0, len(pcm), 1000):                         # odd chunk sizes, like a worklet
                await ws.send_bytes(pcm[off:off + 1000])

            await ws.send_str(json.dumps({"type": "listen", "state": "stop", "mode": "manual"}))
            while True:
                msg = await ws.receive(timeout=timeout)
                if msg.type == WSMsgType.BINARY and isinstance(msg.data, bytes):
                    got_audio.append(msg.data)

                    continue

                if msg.type != WSMsgType.TEXT or not isinstance(msg.data, str):
                    break

                obj = _obj(msg.data)
                got_text.append(obj)
                if obj.get("type") == "tts" and obj.get("state") == until:
                    break

    return got_text, got_audio


def test_relay_speaks_the_pod_protocol():
    fake = FakeBridge()

    async def go():
        bridge_app = Application()
        bridge_app.router.add_get("/xiaozhi/v1/", fake.handle)
        bridge, bridge_port = await _serve(bridge_app)
        side_app = wp.make_app(_cfg(bridge_port, key="sesame", token="dev-token"), codec=FakeCodec)
        side, side_port = await _serve(side_app)
        try:
            denied = await _browser_turn(side_port, {"type": "hello", "device": "ipad", "key": "wrong"}, b"")
            ok = await _browser_turn(
                side_port, {"type": "hello", "device": "ipad", "key": "sesame"}, b"\x01\x00" * 2000)
        finally:
            await side.cleanup()
            await bridge.cleanup()

        return denied, ok

    denied, (texts, audio) = asyncio.run(go())
    assert "access key" in str(denied[0][0].get("error"))
    assert fake.headers["Device-Id"] == "web-pod-ipad" and fake.headers["Authorization"] == "Bearer dev-token"
    hello = fake.texts[0]
    assert hello["type"] == "hello" and hello["audio_params"] == {
        "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}
    assert [t["type"] for t in fake.texts[1:]] == ["listen", "listen"]
    assert fake.frames == [b"OPUS\x07\x80"] * 3                     # 2000 samples -> 2 full frames + padded tail
    assert texts[0] == {"type": "webpod", "device": "web-pod-ipad", "connected": True}
    assert [t["type"] for t in texts[1:]] == ["hello", "stt", "tts", "tts"]      # the mcp frame is not forwarded
    assert audio == [b"\x07\x00" * 1440]


def test_bridge_down_and_healthz_and_pages(tmp_path: Path):
    asyncio.run(_pages(tmp_path))


async def _pages(tmp_path: Path) -> None:
    closed = _free_port()
    side, side_port = await _serve(wp.make_app(_cfg(closed)))
    base = f"http://127.0.0.1:{side_port}"
    try:
        texts, _audio = await _browser_turn(side_port, {"type": "hello", "device": "ipad"}, b"")
        async with ClientSession() as http:
            async with http.get(base + "/healthz") as resp:
                assert resp.status == 200 and resp.content_type == "application/json"
                health = await resp.json()
            async with http.get(base + "/") as resp:
                assert resp.status == 200 and resp.content_type == "text/html"
                assert resp.headers.get("Cache-Control") == "no-cache"
                page = await resp.text()
            async with http.get(base + "/static/app.js") as resp:
                assert resp.status == 200 and resp.content_type == "text/javascript"
                script = await resp.read()
            async with http.get(base + "/static/expressions.json") as resp:
                assert resp.status == 200 and resp.content_type == "application/json"
            async with http.get(base + "/static/face.js") as resp:
                assert resp.status == 200 and resp.content_type == "text/javascript"
            async with http.get(base + "/static/Montserrat.ttf") as resp:
                assert resp.status == 200 and resp.content_type == "font/ttf"
            async with http.get(base + "/static/no-such") as resp:
                missing = resp.status
            async with http.get(base + "/static/../server.py") as resp:
                escaped = resp.status
            async with http.get(base + "/ca.crt") as resp:
                absent = resp.status
                absent_body = await resp.text()
    finally:
        await side.cleanup()

    assert "not reachable" in str(texts[0].get("error"))
    assert health == {"ok": True, "bridge": f"ws://127.0.0.1:{closed}/xiaozhi/v1/", "pods": []}
    assert "<html" in page.lower() and len(script) > 0
    assert missing == 404 and escaped == 404
    assert absent == 404 and "no local CA" in absent_body

    ca_dir = tmp_path
    (ca_dir / "ca.crt").write_bytes(b"CERTDATA")
    ca_side, ca_port = await _serve(wp.make_app(_cfg(closed, tls_dir=ca_dir)))
    try:
        async with ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{ca_port}/ca.crt") as resp:
                assert resp.status == 200 and resp.content_type == "application/x-x509-ca-cert"
                disposition = resp.headers.get("Content-Disposition")
                body = await resp.read()
    finally:
        await ca_side.cleanup()

    assert disposition == 'attachment; filename="skwoop-webpod-ca.crt"' and body == b"CERTDATA"


def test_healthz_lists_the_connected_pod():
    asyncio.run(_health_live())


async def _health_live() -> None:
    fake = FakeBridge()
    bridge_app = Application()
    bridge_app.router.add_get("/xiaozhi/v1/", fake.handle)
    bridge, bridge_port = await _serve(bridge_app)
    side, side_port = await _serve(wp.make_app(_cfg(bridge_port)))
    base = f"http://127.0.0.1:{side_port}"
    try:
        async with ClientSession() as http:
            async with http.ws_connect(f"ws://127.0.0.1:{side_port}/pod") as ws:
                await ws.send_str(json.dumps({"type": "hello", "device": "ipad"}))
                hello = await ws.receive(timeout=5)
                assert isinstance(hello.data, str) and "web-pod-ipad" in hello.data
                async with http.get(base + "/healthz") as resp:
                    live = await resp.json()
    finally:
        await side.cleanup()
        await bridge.cleanup()

    assert live == {"ok": True, "bridge": f"ws://127.0.0.1:{bridge_port}/xiaozhi/v1/", "pods": ["web-pod-ipad"]}
    assert fake.headers["Device-Id"] == "web-pod-ipad" and fake.headers["Client-Id"] == "web-pod-ipad"
    assert fake.headers["Protocol-Version"] == "1"


# ------------------------------------------------------------------ face.js == the Python sim
PARITY_JS = r"""
const F = require(process.argv[2]);
F.init(require(process.argv[3]));
const out = { blend: [], smooth: [], sims: {}, robot: [] };
for (let v = -1; v <= 1.0001; v += 0.25) for (let a = -1; a <= 1.0001; a += 0.25) out.blend.push(F.blend(v, a));
const s = new F.Smoother();
for (const [tv, ta, dt] of [[0, 0, 0], [0.8, 0.6, 0.05], [0.8, 0.6, 0.05], [-0.7, -0.45, 0.3], [0.2, 0.4, 0.05]]) {
  s.step(tv, ta, dt); out.smooth.push([s.v, s.a]);
}
{
  const sim = new F.FaceSim("robot", 7);
  sim.settleOn("idle", [0, 0]);
  const snaps = [];
  const plan = [["idle", [0.8, 0.6]], ["listening", null], ["thinking", null], ["alert", null], ["message", null],
                ["sleep", null], ["offline", null], ["idle", [-0.7, -0.45]]];
  let t = 0;
  for (const [face, va] of plan) {
    sim.setFace(face);
    for (let k = 0; k < 12; k++) {
      t += 50; snaps.push(sim.step(t, { moodOverride: va, blinks: false, glances: false }));
    }
  }
  out.sims.robot = snaps;
}
for (const st of out.sims.robot) out.robot.push(F.robotLayout(st.eye_w, st.eye_h, st.eye_dx, st.eye_dy, st.fx, st.bob));
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None or not (TOOLS / "render_va.py").exists(),
                    reason="optional face-sim tools are not in this checkout")
def test_face_js_matches_the_python_sim(tmp_path):
    pytest.importorskip("PIL")                                   # the sim modules draw with Pillow
    sys.path[:0] = [str(TOOLS)]
    import face_layout as fl
    import pod_mood as pm
    import render_va as rv

    static = STATIC
    script = tmp_path / "parity.js"
    script.write_text(PARITY_JS)
    run = subprocess.run([NODE, str(script), str(static / "face.js"), str(static / "face-data.json")],
                         capture_output=True, text=True, check=True, timeout=60)
    js = json.loads(run.stdout)

    def close(a: float, b: float) -> bool:
        return abs(a - b) < 1e-6

    grid = [(v / 4, a / 4) for v in range(-4, 5) for a in range(-4, 5)]
    for (v, a), got in zip(grid, js["blend"], strict=True):
        want = pm.blend(v, a)
        assert all(close(got[f], want[f]) for f in pm.FIELDS), (v, a)

    sm = pm.Smoother()
    steps = [(0, 0, 0), (0.8, 0.6, 0.05), (0.8, 0.6, 0.05), (-0.7, -0.45, 0.3), (0.2, 0.4, 0.05)]
    for (tv, ta, dt), got in zip(steps, js["smooth"], strict=True):
        sm.step(tv, ta, dt)
        assert close(sm.v, got[0]) and close(sm.a, got[1])

    sim = rv.FaceSim("robot", seed=7)
    sim.settle_on("idle", (0.0, 0.0))
    plan = [("idle", (0.8, 0.6)), ("listening", None), ("thinking", None), ("alert", None), ("message", None),
            ("sleep", None), ("offline", None), ("idle", (-0.7, -0.45))]
    t, py = 0, []
    for face, va in plan:
        sim.set_face(face)
        for _ in range(12):
            t += 50
            py.append(sim.step(t, mood_override=va, blinks=False, glances=False))

    for want, got in zip(py, js["sims"]["robot"], strict=True):
        keys = ("eye_w", "eye_h", "eye_dx", "eye_dy", "mouth_w", "mouth_h", "mouth_curve", "blush", "bob",
                "eye", "body", "v", "a")
        for key in keys:
            assert close(float(want[key]), float(got[key])), (want["face"], key, want[key], got[key])

    for st, got in zip(py, js["robot"], strict=True):
        want = fl.robot_layout(st["eye_w"], st["eye_h"], st["eye_dx"], st["eye_dy"], st["fx"], st["bob"])
        assert [list(b) for b in want["eye"]] == got["eye"] and [list(b) for b in want["arc"]] == got["arc"]
        got_bits = (got["brow_opa"], got["glow_opa"], got["dim"])
        assert (want["brow_opa"], want["glow_opa"], want["dim"]) == got_bits


def test_face_data_is_robot_only():
    shipped = json.loads((STATIC / "face-data.json").read_text())
    styles = shipped["styles"]
    assert {"idle", "listening", "thinking", "alert", "message", "sleep", "offline", "speaking"} <= set(styles)
    assert all(isinstance(style, dict) and "body" in style for style in styles.values())
    assert "robot_state_y" in shipped
