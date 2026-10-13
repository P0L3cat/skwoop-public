"""SkwoopOS library lookup order (whole -> b2 overlap join -> live), mood aliases, the no-hum rule, the TTS wiring
(library before live ElevenLabs, previous/next_text for live text bodies, Grokbot stays on xAI), the Apple TV lines
and POST /speak-preview. A 27-clip cut of the sample library (tests/bridge/fixtures/sample-lib); no network, no
keys, no cost. Seams: the library dir, a TTS subclass whose two network legs record instead of calling out, an
injected title resolver (house.ShowContext) and an injected ack cache dir."""
import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from synth_audio import library_root

FIX = library_root()
BUILD = Path(__file__).resolve().parents[2] / "tools" / "build_clip_index.py"

import pytest  # noqa: E402

import tools.ha_service as house  # noqa: E402
import stitch  # noqa: E402
from credit_guard import CreditGuard  # noqa: E402
from inbox_store import InboxStore  # noqa: E402
from providers import TTS, TTS_RATE  # noqa: E402
from skwoop_lib import NONVERBAL, ManifestError, SkwoopLibrary, build_manifest_tables, norm, parse_manifest, speak_mood  # noqa: E402


def _indexed(root: Path) -> None:
    proc = subprocess.run([sys.executable, str(BUILD), str(root)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.fixture(scope="module")
def lib(tmp_path_factory: pytest.TempPathFactory) -> SkwoopLibrary:
    root = tmp_path_factory.mktemp("sample-lib")
    (root / "manifest.json").symlink_to(FIX / "manifest.json")
    (root / "clips").symlink_to(FIX / "clips")
    _indexed(root)
    loaded = SkwoopLibrary(root, enabled=True)
    assert loaded.ready, loaded.error

    return loaded


# ------------------------------------------------------------------ parsing + lookup
def test_manifest_is_parsed_at_the_boundary():
    raw = json.loads((FIX / "manifest.json").read_text())
    m = parse_manifest(raw)
    assert m.templates["weather_now"].mode == "b2" and m.templates["room_on"].mode == "whole"
    assert all(c.file.startswith("clips/") for c in m.clips.values())
    with pytest.raises(ManifestError):
        parse_manifest(dict(raw, clips=[]))
    with pytest.raises(ManifestError):
        parse_manifest(dict(raw, templates={"x": {"sentence": 3, "moods": []}}))


def test_missing_index_is_not_ready(tmp_path):
    (tmp_path / "manifest.json").write_text((FIX / "manifest.json").read_text())
    lib = SkwoopLibrary(tmp_path, enabled=True)
    assert not lib.ready and lib.error == "IndexMissing" and lib.find("Okay.") is None
    assert lib.describe()["ready"] is False and lib.describe()["error"] == "IndexMissing"


def test_stale_index_is_not_ready(tmp_path):
    (tmp_path / "clips").symlink_to(FIX / "clips")
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes((FIX / "manifest.json").read_bytes())
    _indexed(tmp_path)
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    lib = SkwoopLibrary(tmp_path, enabled=True)
    assert not lib.ready and lib.error == "IndexStale" and lib.find("Okay.") is None
    assert lib.describe()["error"] == "IndexStale"


def test_norm_numbers_and_punctuation():
    assert norm("Right now it's 72°F and sunny!") == "right now it's seventy two degrees and sunny"
    assert norm("My battery is at 34%.") == "my battery is at thirty four percent"
    assert norm("Okay, the KITCHEN lights are on") == norm("okay the kitchen lights are on.")


def test_whole_sentence_first(lib):
    r = lib.render("Okay, the kitchen lights are on.", "happy")
    assert r is not None and r.found.how == "whole" and r.found.mood == "happy" and len(r.pcm) > TTS_RATE
    assert lib.render("My battery is at 34%.", "idle").found.how == "whole"       # digits match the spelled render
    assert lib.render("Putting on the blacklist", "happy").found.how == "whole"


def test_b2_overlap_join_when_no_whole(lib):
    rep = []
    r = lib.render("Right now it's 72 degrees and sunny.", "idle", report=rep)
    assert r.found.how == "b2" and r.found.template == "weather_now" and len(r.found.pieces) == 3
    assert r.v3_fallback is False and rep and all("fallback" not in j for j in rep)  # a real overlap join
    assert 1.5 < len(r.pcm) / 2 / TTS_RATE < 5


def test_whole_beats_b2(tmp_path):
    # a manifest where the same words exist as a whole render AND as b2 pieces: the whole render wins
    raw = json.loads((FIX / "manifest.json").read_text())
    any_clip = next(iter(raw["clips"]))
    raw["templates"]["wx_whole"] = {"sentence": "Right now it's {temp} and {wx}.", "moods": ["idle"], "mode": "whole"}
    raw["whole"]['wx_whole|idle|{"temp": "seventy-two degrees", "wx": "sunny"}'] = any_clip
    (tmp_path / "manifest.json").write_text(json.dumps(raw))
    (tmp_path / "clips").symlink_to(FIX / "clips")
    _indexed(tmp_path)
    found = SkwoopLibrary(tmp_path, enabled=True).find("Right now it's 72 degrees and sunny.", "idle")
    assert found.how == "whole" and found.clip_id == any_clip


def test_miss_means_live(lib):
    assert lib.find("The quantum flux capacitor is warm.", "idle") is None
    assert lib.find("Right now it's 73 degrees and sunny.", "idle") is None   # b2 shape, but no 73 piece


def test_moods_sleepy_is_calm_and_fallback(lib):
    assert speak_mood("sleepy") == speak_mood("sleep") == "calm" and speak_mood("neutral") == "idle"
    assert lib.find("Good night!", "sleepy").mood == "calm"
    assert lib.find("Okay, a text to Mel.", "sad").mood in ("idle", "happy")     # no sad take: closest mood


def test_no_hum_or_moan_lines(lib):
    for t in ("Mmm.", "Hmm...", "Mm-hm!", "Hummm", "moans", "Zzz..."):
        assert NONVERBAL.match(t), t
        assert lib.find(t, "calm") is None

    assert not NONVERBAL.match("Hmm, I couldn't find that room.")   # a real sentence that starts with hmm is fine
    lines = build_manifest_tables(FIX).manifest.lines
    assert not any(NONVERBAL.match(it.text) for items in lines.values() for it in items)


def test_pick_line_never_repeats_back_to_back(lib):
    a = lib.pick_line("tv_ok", "idle")
    assert a.pcm and a.kind == "tv_ok"
    assert len({lib.pick_line("tv_ok", "idle").text for _ in range(6)}) == 2


def test_library_off_switch():
    assert SkwoopLibrary(FIX, enabled=False).ready is False
    assert SkwoopLibrary(FIX / "nope", enabled=True).ready is False   # missing dir: everything live, no crash


def test_default_join_is_overlap():
    assert stitch.JOIN == "overlap" or os.environ.get("STITCH_JOIN")


# ------------------------------------------------------------------ TTS wiring
@dataclasses.dataclass
class Call:
    leg: str
    text: str
    previous_text: str | None = None
    next_text: str | None = None


class Recorder(TTS):
    """providers.TTS with its two network legs recording instead of calling out (the real seam: subclass)."""

    def __init__(self, lib):
        super().__init__(None, library=lib,
                         guard=CreditGuard(state_path=Path(tempfile.mkdtemp()) / "c.json", balance=5000, floor=1000))
        self.provider, self.fallback = "elevenlabs", "xai"
        self.calls: list[Call] = []

    async def _elevenlabs(self, text, mood=None, previous_text=None, next_text=None):
        self.calls.append(Call("eleven", text, previous_text, next_text))
        self.last_cost = 2.0

        return b"\x02\x00" * 2400

    async def _xai(self, text, voice=None):
        self.calls.append(Call("xai", text))

        return b"\x03\x00" * 2400


@pytest.fixture
def rec(lib, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-only")       # the key gate; the legs above never send it

    return Recorder(lib)


def test_tts_lookup_order(rec):
    async def go():
        backends = []
        for text, mood in (("Okay, the kitchen lights are on.", "happy"),
                           ("Right now it's 72 degrees and sunny.", None),
                           ("The quantum flux capacitor is warm.", "idle")):
            await rec.synth(text, mood=mood)
            backends.append(rec.last_backend)

        return backends

    assert asyncio.run(go()) == ["clip-whole", "clip-b2", "elevenlabs"]
    assert [c.leg for c in rec.calls] == ["eleven"]                      # only the miss went live
    assert rec.guard.describe()["spent_since_seed"] == 2.0


def test_live_text_body_gets_previous_and_next_text(rec):
    parts = ["Here's your text to Mel.", "Running ten minutes late, save me a seat.", "Should I send it?"]
    pcm = asyncio.run(rec.synth_parts(parts, mood="idle"))
    assert rec.calls == [Call("eleven", parts[1], parts[0], parts[2])]   # context, not spoken
    assert len(pcm) > 3 * 4800


def test_multi_sentence_reply_mixes_clips_and_live(rec):
    asyncio.run(rec.synth("Okay, a text to Mel. What a strange little afternoon this is.", mood="idle"))
    assert rec.calls == [Call("eleven", "What a strange little afternoon this is.", "Okay, a text to Mel.")]


def test_one_voice_has_no_persona_bypass(rec):
    """Grokbot text goes through the same library -> ElevenLabs order as everything else (no xAI-voice persona)."""
    asyncio.run(rec.synth("Okay, the kitchen lights are on."))
    asyncio.run(rec.synth("Crows have three hearts."))
    assert rec.calls == [Call("eleven", "Crows have three hearts.")]


def test_inbox_body_uses_context(rec, tmp_path):
    inbox = InboxStore(path=str(tmp_path / "inbox"), synth=rec.synth)
    inbox.synth_parts = rec.synth_parts
    item = asyncio.run(inbox.add("Mel", "Running about ten minutes late. Save me a seat by the window."))
    assert item["duration_s"] > 0.3
    # the body is one live request (<= 400 chars, ~1 credit) spoken as the continuation of the preface, and the
    # preface "New text from Mel." itself came from the library
    assert rec.calls == [Call("eleven", "Running about ten minutes late. Save me a seat by the window.",
                              "New text from Mel.")]


# ------------------------------------------------------------------ Apple TV lines (house.py)
def _resolve(title):
    t = title.lower()
    if t == "the blacklist":
        return house.ShowLookup("NETFLIX", ("com.netflix.Netflix",))

    if t == "severance":
        return house.ShowLookup("APPLE", ("https://tv.apple.com/show/umc.cmc.1srk2goyh2q2zdxcx605w8vtx",))

    return house.ShowLookup("SEARCH", (f"videos://search?term={t}",))


@pytest.fixture
def show(lib):
    return house.show_context(lib, resolve=_resolve)


def test_parse_show_lookup():
    assert house.parse_show_lookup(("APPLE", ["https://tv.apple.com/x", 3])) == house.ShowLookup(
        "APPLE", ("https://tv.apple.com/x",))
    assert house.parse_show_lookup(("HULU", [])).mode == "SEARCH" and house.parse_show_lookup(None).mode == "SEARCH"


def test_play_show_apple_deeplink(show):
    h = house.play_show_hit("play the show severance", show)
    assert h.spoken == "Putting on Severance." and h.mode == "APPLE" and h.follow is None
    assert h.deeplink.startswith("https://tv.apple.com/show/umc.cmc.")
    assert h.script == "script.play_on_living_room_tv" and h.data == {"query": "severance"}   # HA contract unchanged


def test_play_show_ignores_stt_punctuation(show):
    h = house.play_show_hit("Play Severance on TV.", show)               # how STT hands it over
    assert h.mode == "APPLE" and h.spoken == "Putting on Severance." and h.data == {"query": "Severance"}
    assert house.play_show_hit("Watch the Blacklist!", show).mode == "NETFLIX"


def test_play_show_netflix_follow_up(show, rec, tmp_path):
    h = house.play_show_hit("watch the blacklist", show)
    assert h.spoken == "Putting on The Blacklist." and h.follow == house.NETFLIX_LINE and h.mode == "NETFLIX"
    assert h.text == "Putting on The Blacklist. Netflix is up. You'll have to pick it from there."
    pcm = asyncio.run(house.ack_pcm(rec, h, cache_dir=tmp_path))
    assert rec.calls == [] and len(pcm) / 2 / TTS_RATE > 3              # both lines from the library, no live call


def test_play_show_unknown_title_searches(show):
    h = house.play_show_hit("play some obscure thing on the tv", show)
    assert h.mode == "SEARCH" and h.spoken == "Playing." and h.deeplink.startswith("videos://search")


def test_hit_from_id_passes_the_context(show):
    assert house.hit_from_id("tv_play_show", "watch severance", show).mode == "APPLE"


def test_play_without_tv_or_show_is_music(show):
    assert house.play_show_hit("Play Severance.", show) is None                 # Spotify (router DEEP music)
    assert house.play_show_hit("play show me how", show) is None
    assert house.play_show_hit("play the movie dune on apple tv", show).data == {"query": "dune"}
    assert house.play_show_hit("watch severance on the tv", show).data == {"query": "severance"}


def test_ack_miss_is_cached_per_voice(rec, tmp_path):
    h = house.Hit("Grokbot.", "endpoint_grokbot", None)
    asyncio.run(house.ack_pcm(rec, h, cache_dir=tmp_path))
    asyncio.run(house.ack_pcm(rec, h, cache_dir=tmp_path))
    assert [c.text for c in rec.calls] == ["Grokbot."]                   # live once, then the cached take


# ------------------------------------------------------------------ POST /speak-preview (the deploy smoke test)
def test_parse_preview_request():
    import skwoop_bridge

    r = skwoop_bridge.parse_preview_request({"text": "hi", "simulate": "floor"})
    assert r.simulate_floor and r.parts == () and r.mood is None
    for bad in ([], {"text": 3}, {"parts": "x"}, {"text": ""}, {"text": "a", "simulate": "boom"}):
        with pytest.raises(ValueError):
            skwoop_bridge.parse_preview_request(bad)


def test_speak_preview_endpoint(rec, monkeypatch):
    from lean_http import ClientSession, web

    import skwoop_bridge

    monkeypatch.setenv("STATUS_WRITE_KEY", "test-only-key")

    async def go():
        app = skwoop_bridge.build_app(18150)
        app.on_startup.clear()
        app.on_cleanup.clear()
        app["tts"] = rec
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 18150).start()
        out = {}
        try:
            async with ClientSession() as http:
                url, auth = "http://127.0.0.1:18150/speak-preview", {"Authorization": "Bearer test-only-key"}
                async with http.post(url, json={"text": "hi"}) as r:
                    out["noauth"] = r.status

                async with http.post(url, json={"text": 5}, headers=auth) as r:
                    out["bad"] = r.status

                for name, body in (("whole", {"text": "Okay, the kitchen lights are on.", "mood": "happy"}),
                                   ("b2", {"text": "Right now it's 72 degrees and sunny."}),
                                   ("live", {"text": "A brand new sentence nobody rendered."}),
                                   ("floor", {"text": "A brand new sentence nobody rendered.", "simulate": "floor"}),
                                   ("parts", {"parts": ["Here's your text to Mel.", "See you at six.",
                                                        "Should I send it?"]})):
                    async with http.post(url, json=body, headers=auth) as r:
                        out[name] = (r.status, r.headers.get("X-Skwoop-Backend"), r.headers.get("X-Skwoop-Credits"),
                                     (await r.read())[:4])
        finally:
            await runner.cleanup()

        return out

    out = asyncio.run(go())
    assert out["noauth"] == 401 and out["bad"] == 400
    assert out["whole"][:2] == (200, "clip-whole") and out["whole"][3] == b"RIFF"
    assert out["b2"][:2] == (200, "clip-b2")
    assert out["live"][:2] == (200, "elevenlabs") and "calls=1" in out["live"][2]
    assert out["floor"][:2] == (200, "xai-fallback") and "calls=1" in out["floor"][2]   # nothing spent
    assert out["parts"][0] == 200
    assert [c.leg for c in rec.calls] == ["eleven", "xai", "eleven"]
    assert rec.calls[2] == Call("eleven", "See you at six.", "Here's your text to Mel.", "Should I send it?")


# ------------------------------------------------------------------ the full library, where it is installed
_FULL = [Path(p) for p in (os.environ.get("SKWOOP_FULL_LIB"), os.path.expanduser("~/.skwoop/skwoop-lib"),
                           "",
                           "")
         if p and os.path.exists(os.path.join(p, "clips.idx"))
         and os.path.exists(os.path.join(p, "manifest.json"))]


@pytest.mark.skipif(not _FULL, reason="the full SkwoopOS library (clips.idx) isn't on this machine")
def test_full_library_lookup_order():
    # Needs clips.idx (an earlier index); a bare manifest.json is not enough.
    lib = SkwoopLibrary(_FULL[0], enabled=True)
    described = lib.describe()
    assert described["clips"] == 15953 and (described["voice"] or "").startswith("")
    assert lib.find("Okay, the bedroom lights are at 70%.", "idle").how == "whole"      # tier-6 gold render
    r = lib.render("Okay, the office lights are at 45%.", "idle")                       # no gold: b2 overlap join
    assert r.found.how == "b2" and r.v3_fallback is False and r.ms < 250
    assert lib.find("Putting on Severance.", "happy").how == "whole"
    assert lib.find("Netflix is up. You'll have to pick it from there.", "idle").how == "whole"
    assert lib.find("Tell me something I don't know about crows.", "idle") is None
