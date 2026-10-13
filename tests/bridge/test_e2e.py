"""Offline end-to-end tests: real bridge + real Opus + fake_pod, with stub STT/LLM/TTS.

No network, no keys. Run:  python -m pytest -q tests  (or: python tests/test_e2e.py)
"""
import asyncio
import math
import os
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.mkdtemp()
os.environ["STATUS_FILE"] = os.path.join(_tmp, "status.json")
os.environ["INBOX_DIR"] = os.path.join(_tmp, "inbox")
os.environ["STATUS_WRITE_KEY"] = "test-only-key"
os.environ["STATUS_READ_KEY"] = "test-read-key"
os.environ["SKWOOP_ROOMS"] = '{"aa:bb:cc:00:00:01":"desk","aa:bb:cc:00:00:02":"kitchen","aa:bb:cc:00:00:03":"office"}'

from lean_http import ClientSession, FormData, web  # noqa: E402

import fake_pod  # noqa: E402
import skwoop_bridge  # noqa: E402
from stubs import StubSTT  # noqa: E402
from providers import TTS_RATE, pcm_to_wav  # noqa: E402

AUTH = {"Authorization": "Bearer test-only-key"}
READ = {"Authorization": "Bearer test-read-key"}


class E2eSTT(StubSTT):
    async def transcribe(self, pcm, rate):
        assert len(pcm) > rate  # got >0.5 s of decoded audio
        return "what is the team doing"


class StubLLM:
    mode = "stub"
    def __init__(self):
        self.last_system = ""
    async def stream_sentences(self, messages, fast=False):
        self.last_system = messages[0]["content"]
        assert "Skwoop: busy" in self.last_system  # team status reached the prompt
        for s in ["Skwoop is deploying the bridge.", "Everything else is quiet."]:
            yield s


class StubTTS:
    provider = "stub"
    library = None        # the providers.TTS / house.AckSpeaker surface: no SkwoopOS clips here
    last_backend = ""

    async def synth(self, text, mood=None, previous_text=None, next_text=None):
        n = int(TTS_RATE * 0.5)
        return b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / TTS_RATE))) for i in range(n))


def tone_wav(path, seconds=1.2, rate=16000):
    pcm = b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 220 * i / rate)))
                   for i in range(int(rate * seconds)))
    open(path, "wb").write(pcm_to_wav(pcm, rate))


async def _server(port):
    app = skwoop_bridge.build_app(port)
    app.on_startup.clear()
    app.on_cleanup.clear()
    app["stt"], app["llm"], app["tts"] = E2eSTT(), StubLLM(), StubTTS()
    app["store"].inbox.synth = app["tts"].synth
    app["store"].upsert_bot("Skwoop", "busy", "deploying")
    app["store"].upsert_bot("helper", "ok")
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return app, runner


def _args(**kw):
    base = dict(server="", wav=None, say=None, mode="manual", out=None, timeout=10, fast=True,
                inbox=None, wrap="mcp", next_after=None, device=None)
    base.update(kw)
    return type("A", (), base)()


# ----------------------------------------------------------------------------- voice turn
async def _run_voice():
    app, runner = await _server(18003)
    try:
        d = tempfile.mkdtemp()
        q, out = os.path.join(d, "q.wav"), os.path.join(d, "reply.wav")
        tone_wav(q)
        results = {}
        for mode in ("manual", "auto"):
            results[mode] = await fake_pod.run(_args(server="http://127.0.0.1:18003", wav=q, mode=mode, out=out))
        return results, os.path.getsize(out), app["store"].feed()
    finally:
        await runner.cleanup()


def test_e2e():
    results, size, feed = asyncio.run(_run_voice())
    assert results == {"manual": 0, "auto": 0}, results
    assert size > 40000  # ~1 s of 24 kHz reply audio made it through Opus both ways
    assert feed["summary"].startswith("Skwoop busy")
    assert feed["inbox"]["unread"] == 0


# ----------------------------------------------------------------------------- voice inbox
async def _run_inbox():
    port = 18005
    base = f"http://127.0.0.1:{port}"
    app, runner = await _server(port)
    inbox = app["store"].inbox
    log = []
    try:
        async with ClientSession() as http:
            async def status():
                async with http.get(f"{base}/status", headers=READ) as r:
                    return (await r.json())["inbox"]

            # 1. queue a message (key required)
            async with http.post(f"{base}/inbox", json={"from": "Skwoop", "text": "x"}) as r:
                assert r.status == 401
            async with http.post(f"{base}/inbox", headers=AUTH,
                                 json={"from": "Skwoop", "text": "Build is green. Nothing urgent."}) as r:
                assert r.status == 200, await r.text()
                first = (await r.json())["item"]
            assert first["duration_s"] > 1.0  # "Message from Skwoop." + gap + message

            # 2. status shows unread=1; GET /inbox is metadata-only without the key
            st = await status()
            assert st["unread"] == 1 and st["latest_from"] == "Skwoop", st
            log.append(f"after queue: unread={st['unread']} latest_from={st['latest_from']}")
            async with http.get(f"{base}/inbox") as r:
                meta = await r.json()
            assert meta["count"] == 1 and "text" not in meta["items"][0]
            async with http.get(f"{base}/inbox", headers=AUTH) as r:
                assert (await r.json())["items"][0]["text"].startswith("Build is green")

            # 3. play (MCP-wrapped command, exactly like the firmware)
            res = await fake_pod.run_inbox(_args(server=base, inbox="play", wrap="mcp"))
            secs = len(res["pcm"]) / 2 / res["rate"]
            assert secs > 1.0, secs
            assert res["texts"][0].startswith("Skwoop: Build is green"), res["texts"]
            assert res["events"][0] == "tts:start" and res["events"][-1] == "tts:stop", res["events"]
            log.append(f"played {secs:.2f}s: {res['texts'][0]!r}")

            # 4. unread=0 and the item is marked played
            st = await status()
            assert st["unread"] == 0, st
            assert inbox.items[first["id"]]["played"] is True
            log.append(f"after play: unread={st['unread']}")

            # 5. a voice turn right after = reply, with context for Deep
            d = tempfile.mkdtemp()
            q = os.path.join(d, "q.wav")
            tone_wav(q)
            assert await fake_pod.run(_args(server=base, wav=q, out=os.path.join(d, "r.wav"))) == 0
            assert "replying to a message from Skwoop" in app["llm"].last_system
            log.append("reply turn carried the inbox reply context")

            # 6. two messages, tap during playback = next (plain {"type":"inbox"} form)
            for t in ("First of two.", "Second of two."):
                async with http.post(f"{base}/inbox", headers=AUTH, json={"from": "helper", "text": t}) as r:
                    assert r.status == 200
            assert (await status())["unread"] == 2
            res = await fake_pod.run_inbox(_args(server=base, inbox="play", wrap="plain", next_after=0.3))
            assert [t.split(": ", 1)[1] for t in res["texts"]] == ["First of two.", "Second of two."], res["texts"]
            assert res["events"].count("tts:start") == 1 and res["events"][-1] == "tts:stop", res["events"]
            assert (await status())["unread"] == 0
            log.append("next during playback skipped to message 2; unread=0")

            # 7. nothing left: play answers "No new messages." and stops (device returns to idle)
            res = await fake_pod.run_inbox(_args(server=base, inbox="play"))
            assert res["texts"] == ["No new messages."] and res["events"][-1] == "tts:stop"

            # 8. pre-recorded upload, skip without playing, delete
            wav = os.path.join(d, "note.wav")
            tone_wav(wav, seconds=2.0, rate=16000)
            form = FormData()
            form.add_field("from", "Skwoop")
            form.add_field("audio", open(wav, "rb").read(), filename="note.wav", content_type="audio/wav")
            async with http.post(f"{base}/inbox", headers=AUTH, data=form) as r:
                assert r.status == 200, await r.text()
                up = (await r.json())["item"]
            assert up["source"] == "upload" and up["duration_s"] > 2.0
            res = await fake_pod.run_inbox(_args(server=base, inbox="skip"))  # not playing: marks played
            assert (await status())["unread"] == 0
            async with http.delete(f"{base}/inbox/{up['id']}", headers=AUTH) as r:
                assert (await r.json())["removed"] is True
            assert not inbox.audio_path(up["id"]).exists()

            # 9. played items expire after 24 h
            before = len(inbox.items)
            assert inbox.expire(now=time.time() + 25 * 3600) == before
            assert len(inbox.items) == 0
            log.append("upload/skip/delete/24h-expiry ok")
        return log
    finally:
        await runner.cleanup()


def test_inbox():
    for line in asyncio.run(_run_inbox()):
        print("  inbox:", line)


# ----------------------------------------------------------------------------- several pods
KITCHEN, OFFICE = fake_pod.device_mac(2), fake_pod.device_mac(3)


async def _run_multi():
    port = 18009
    base = f"http://127.0.0.1:{port}"
    app, runner = await _server(port)
    log = []
    try:
        async with ClientSession() as http:
            async def status(dev=None, **q):
                headers = {"Device-Id": dev, **READ} if dev else dict(READ)
                async with http.get(f"{base}/status", headers=headers, params=q) as r:
                    return await r.json()

            async def queue(text, room=None):
                body = {"from": "Skwoop", "text": text}
                if room:
                    body["room"] = room
                async with http.post(f"{base}/inbox", headers=AUTH, json=body) as r:
                    assert r.status == 200, await r.text()
                    return (await r.json())["item"]

            for dev in (KITCHEN, OFFICE):   # both pods are on the network
                await status(dev)

            # 1. both hear "Hi ESP"; the office's wake reaches the bridge host first but the kitchen heard it
            #    louder -> only the kitchen answers, the office closes quietly
            office, kitchen = await asyncio.gather(
                fake_pod.run_wake(base, OFFICE, level=0.08, delay=0.0),
                fake_pod.run_wake(base, KITCHEN, level=0.5, delay=0.12))
            assert kitchen["answered"] and kitchen["audio_s"] > 0.5, kitchen
            assert not office["answered"] and office["stood_down"], office
            assert "asked from the kitchen pod (hands-free, by wake word)" in app["llm"].last_system
            log.append("wake together: kitchen answered, office stood down (office arrived first)")

            # 2. equal pickup -> tie -> first to arrive wins
            await asyncio.sleep(1.6)  # past the late-arrival guard of the previous round
            office, kitchen = await asyncio.gather(
                fake_pod.run_wake(base, OFFICE, level=0.3, delay=0.0),
                fake_pod.run_wake(base, KITCHEN, level=0.3, delay=0.15))
            assert office["answered"] and kitchen["stood_down"], (office, kitchen)
            log.append("equal pickup: first arrival (office) answered")

            # 3. shared inbox: play on the kitchen pod clears the office badge (long-poll wakes it)
            await queue("Deploy finished.")
            k, o = await status(KITCHEN), await status(OFFICE)
            assert k["inbox"]["unread"] == 1 and o["inbox"]["unread"] == 1
            t0 = time.monotonic()
            poll = asyncio.create_task(status(OFFICE, since=o["rev"], wait=10))
            await asyncio.sleep(0.2)
            assert not poll.done()  # nothing changed yet: the office pod is waiting
            res = await fake_pod.run_inbox(_args(server=base, inbox="play", device=KITCHEN))
            assert res["texts"][0] == "Skwoop: Deploy finished."
            o2 = await poll
            assert o2["inbox"]["unread"] == 0 and time.monotonic() - t0 < 6, o2["inbox"]
            log.append(f"played on kitchen -> office badge 0 after {time.monotonic() - t0:.1f}s (long-poll)")

            # 4. room-targeted alert and message
            async with http.post(f"{base}/alert", headers=AUTH, json={"text": "Oven timer", "room": "kitchen"}) as r:
                assert r.status == 200
            async with http.post(f"{base}/alert", headers=AUTH, json={"text": "x", "room": "garage"}) as r:
                assert r.status == 400  # typo protection when SKWOOP_ROOMS is set
            k, o, w = await status(KITCHEN), await status(OFFICE), await status()
            assert k["alert"]["text"] == "Oven timer" and o["alert"] is None, (k["alert"], o["alert"])
            assert w["alert"]["room"] == "kitchen"  # a widget (no Device-Id) sees everything
            only_office = await queue("Your 3 PM moved.", room="office")
            assert (await status(KITCHEN))["inbox"]["unread"] == 0
            assert (await status(OFFICE))["inbox"]["unread"] == 1
            async with http.delete(f"{base}/inbox/{only_office['id']}", headers=AUTH) as r:
                assert r.status == 200
            async with http.delete(f"{base}/alert", headers=AUTH, params={"room": "kitchen"}) as r:
                assert r.status == 200
            assert (await status(KITCHEN))["alert"] is None
            log.append("room-targeted alert + message showed on one pod only")

            # 5. concurrent play: one message, two taps at once -> it plays once
            await queue("Only one of you hears this.")
            a, b = await asyncio.gather(
                fake_pod.run_inbox(_args(server=base, inbox="play", device=KITCHEN)),
                fake_pod.run_inbox(_args(server=base, inbox="play", device=OFFICE)))
            texts = sorted([a["texts"][0], b["texts"][0]])
            assert sum(t == "Skwoop: Only one of you hears this." for t in texts) == 1, texts
            other = [t for t in texts if not t.startswith("Skwoop:")][0]
            assert other.startswith("That message is playing on the") or other == "No new messages.", other
            assert (await status(KITCHEN))["inbox"]["unread"] == 0
            log.append(f"concurrent play, 1 message: one played it, the other heard {other!r}")

            # ... and with two messages each pod gets a different one
            await queue("Message A.")
            await queue("Message B.")
            a, b = await asyncio.gather(
                fake_pod.run_inbox(_args(server=base, inbox="play", device=KITCHEN)),
                fake_pod.run_inbox(_args(server=base, inbox="play", device=OFFICE)))
            assert sorted([a["texts"][0], b["texts"][0]]) == ["Skwoop: Message A.", "Skwoop: Message B."]
            log.append("concurrent play, 2 messages: each pod got a different one")

            # every pod shows up in the ring as "<room> pod"
            names = {b["name"] for b in (await status())["bots"]}
            assert {"kitchen pod", "office pod"} <= names, names
        return log
    finally:
        await runner.cleanup()


def test_multi_pod():
    for line in asyncio.run(_run_multi()):
        print("  multi:", line)


if __name__ == "__main__":
    test_e2e()
    print("E2E OK")
    test_inbox()
    print("INBOX OK")
    test_multi_pod()
    print("MULTI OK")


# ----------------------------------------------------------------------------- read key (Oct 3 2026)
async def _run_read_key():
    port = 18011
    base = f"http://127.0.0.1:{port}"
    app, runner = await _server(port)
    try:
        async with ClientSession() as http:
            for path in ("/status", "/face.png"):
                async with http.get(base + path) as r:
                    assert r.status == 401, path
                async with http.get(base + path, headers={"Authorization": "Bearer wrong"}) as r:
                    assert r.status == 401, path
                async with http.get(base + path, headers=READ) as r:
                    assert r.status == 200, path
            async with http.post(f"{base}/xiaozhi/ota/", headers={"Device-Id": KITCHEN},
                                 json={"application": {"version": "0.0.0"}}) as r:
                status_url = (await r.json())["websocket"]["status_url"]
            assert status_url == f"http://127.0.0.1:{port}/status?key=test-read-key"
            async with http.get(status_url + "&since=-1&wait=0", headers={"Device-Id": KITCHEN}) as r:
                assert r.status == 200                      # what the pod firmware does with the OTA url
                assert (await r.json())["room"] == "kitchen"
    finally:
        await runner.cleanup()


def test_read_key_guards_status_and_face_and_reaches_pods_via_ota():
    asyncio.run(_run_read_key())
