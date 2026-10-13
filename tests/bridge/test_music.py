"""Music fast path (music.py): parsing, matching, the Deep-owned token, and whole turns against a local stand-in
for the Spotify API and the pick model proxy. No network."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest
from lean_http import ClientSession, web

import tools.music as music
from tools.music import Control, Music, PlayClear, PlayLoose, Spotify, TokenSource, parse_ask, parse_picks, same

# ---------------------------------------------------------------- what Alex asked for

@pytest.mark.parametrize("said,ask", [
    ("Play Mr. Brightside.", PlayClear("mr brightside")),
    ("Skwoop, play Bohemian Rhapsody by Queen.", PlayClear("bohemian rhapsody", "any", "queen")),
    ("Can you play some Taylor Swift please?", PlayClear("taylor swift", "artist")),
    ("Play the album Abbey Road by the Beatles", PlayClear("abbey road", "album", "the beatles")),
    ("Play my metal and house fusion playlist.", PlayClear("metal and house fusion", "mine")),
    ("Switch to my metal and house fusion playlist.", PlayClear("metal and house fusion", "mine")),
    ("Play something to go with my morning coffee.", PlayLoose("play something to go with my morning coffee")),
    ("Play some 90s hip hop.", PlayLoose("play some 90s hip hop")),
    ("Play jazz", PlayLoose("play jazz")),
    ("Play something new I haven't heard.", PlayLoose("play something new i haven't heard")),
    ("Change it to my metal and house fusion playlist", PlayClear("metal and house fusion", "mine")),
    ("Play something by Hoobastank", PlayClear("hoobastank", "artist")),
    ("Pause the music.", Control("pause")),
    ("Skip this song.", Control("next")),
    ("Next song", Control("next")),
    ("Play the previous song", Control("previous")),
    ("Resume.", Control("resume")),
    ("What's playing right now?", Control("now_playing")),
])
def test_parse_ask(said, ask):
    assert parse_ask(said) == ask


@pytest.mark.parametrize("said", ["what time is it", "Queue up Hey Jude", "Shuffle my liked songs"])
def test_left_to_deep(said):
    assert parse_ask(said) is None


def test_names_match_the_way_people_say_them():
    assert same("Mr. Brightside", "mr brightside")
    assert same("Bohemian Rhapsody - Remastered 2011", "Bohemian Rhapsody")
    assert same("Beyoncé", "beyonce")
    assert not same("Mr. Blue Sky", "Mr. Brightside")


def test_model_picks_are_parsed_strictly():
    picks = parse_picks('Sure! {"say": "Mellow jazz for the coffee", "songs": [["So What", "Miles Davis"], '
                        '["Blue in Green", "Miles Davis"], ["Take Five", "Dave Brubeck"], ["bad"]]}')
    assert picks.say == "Mellow jazz for the coffee"
    assert picks.songs == (("So What", "Miles Davis"), ("Blue in Green", "Miles Davis"), ("Take Five", "Dave Brubeck"))
    with pytest.raises(music.MusicError):
        parse_picks('{"say": "x", "songs": [["a", "b"]]}')


# ---------------------------------------------------------------- the token Deep owns

def _auth(path: Path, token: str, expires_in: float) -> None:
    at = datetime.now(UTC) + timedelta(seconds=expires_in)
    path.write_text(json.dumps({"providers": {"spotify": {"access_token": token, "expires_at": at.isoformat()}}}))


def test_fresh_token_is_read_from_deep_store_without_refreshing(tmp_path):
    auth = tmp_path / "auth.json"
    _auth(auth, "fresh", 3000)
    calls: list[bool] = []

    async def resolve(force: bool) -> None:
        calls.append(force)

    assert asyncio.run(TokenSource(auth, resolve).get()) == "fresh"
    assert calls == []


def test_expiring_token_is_refreshed_by_deep_and_reread(tmp_path):
    auth = tmp_path / "auth.json"
    _auth(auth, "old", 30)
    calls: list[bool] = []

    async def resolve(force: bool) -> None:     # stands in for Deep's resolver: it rewrites auth.json
        calls.append(force)
        _auth(auth, "new", 3600)

    assert asyncio.run(TokenSource(auth, resolve).get()) == "new"
    assert calls == [False]


# ---------------------------------------------------------------- whole asks against a stand-in Spotify

COMPUTER = {"id": "pc1", "name": "PC", "type": "Computer", "is_active": True}


def _track(uri: str, name: str, artist: str, pop: int = 60) -> dict:
    return {"uri": uri, "name": name, "artists": [{"name": artist}], "popularity": pop}


class FakeSpotify:
    def __init__(self, picks: str = "", fail_play: int = 0) -> None:
        self.played: list[dict] = []
        self.calls: list[str] = []
        self.picks = picks
        self.fail_play = fail_play
        self.auth: list[str] = []
        self.pick_auth: list[str | None] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/v1/me/player/devices", self.devices)
        app.router.add_get("/v1/search", self.search)
        app.router.add_put("/v1/me/player/play", self.play)
        app.router.add_put("/v1/me/player/pause", self.ok)
        app.router.add_post("/v1/me/player/next", self.ok)
        app.router.add_get("/v1/me/player/recently-played", self.recent)
        app.router.add_get("/v1/me/playlists", self.playlists)
        app.router.add_post("/pick/v1/chat/completions", self.pick)
        return app

    async def devices(self, request: web.Request) -> web.Response:
        self.auth.append(request.headers.get("Authorization", ""))
        return web.json_response({"devices": [{"id": "phone", "type": "Smartphone"}, COMPUTER]})

    async def search(self, request: web.Request) -> web.Response:
        if "market" in request.query:      # live Spotify: market=from_token needs a scope Deep's login lacks
            return web.json_response({"error": {"status": 403, "message": "Insufficient client scope"}}, status=403)
        q, types = request.query["q"], request.query["type"]
        self.calls.append(f"search {types} {q}")
        tracks = {
            "mr brightside": [_track("t:mb", "Mr. Brightside", "The Killers")],
            "track:bohemian rhapsody artist:queen": [_track("t:br", "Bohemian Rhapsody - Remastered 2011", "Queen")],
            "track:So What artist:Miles Davis": [_track("t:sw", "So What", "Miles Davis")],
            "track:Take Five artist:Dave Brubeck": [_track("t:t5", "Take Five", "The Dave Brubeck Quartet")],
            "track:Blue in Green artist:Miles Davis": [_track("t:big", "Blue in Green", "Miles Davis")],
        }.get(q, [])
        artists = [{"uri": "a:ts", "name": "Taylor Swift", "popularity": 100}] if q == "taylor swift" else []
        return web.json_response({"tracks": {"items": tracks}, "artists": {"items": artists}})

    async def play(self, request: web.Request) -> web.Response:
        if self.fail_play:
            self.fail_play -= 1
            return web.json_response({"error": {"status": 401}}, status=401)
        self.played.append({"device": request.query.get("device_id"), **(await request.json())})
        return web.Response(status=204)

    async def ok(self, request: web.Request) -> web.Response:
        self.calls.append(f"{request.method} {request.path}")
        return web.Response(status=204)

    async def recent(self, request: web.Request) -> web.Response:
        return web.json_response({"items": [{"track": _track("t:x", "X", "Hoobastank")}]})

    async def playlists(self, request: web.Request) -> web.Response:
        return web.json_response({"items": [None, {"uri": "p:mhf", "name": "Metal and House Fusion",
                                                   "owner": {"display_name": "Alex"}, "tracks": {"total": 40}}]})

    async def pick(self, request: web.Request) -> web.Response:
        self.pick_auth.append(request.headers.get("Authorization"))
        return web.json_response({"choices": [{"message": {"content": self.picks}}]})


async def _ask(tmp: Path, fake: FakeSpotify, said: str, port: int):
    auth = tmp / "auth.json"
    _auth(auth, "tok", 3000)
    refreshed: list[bool] = []

    async def resolve(force: bool) -> None:
        refreshed.append(force)
        _auth(auth, "tok2", 3600)

    runner = web.AppRunner(fake.app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    port = runner.addresses[0][1]
    try:
        async with ClientSession() as http:
            spotify = Spotify(http, TokenSource(auth, resolve), base=f"http://127.0.0.1:{port}/v1")
            m = Music(http, spotify, pick_url=f"http://127.0.0.1:{port}/pick/v1/chat/completions",
                      off_flag=tmp / "music-fast.off")
            started = time.monotonic()
            played = await m.handle(said)
            return played, refreshed, time.monotonic() - started
    finally:
        await runner.cleanup()


def test_clear_song_plays_the_top_match_on_the_computer(tmp_path):
    fake = FakeSpotify()
    played, _, _ = asyncio.run(_ask(tmp_path, fake, "Play Mr. Brightside.", 18301))
    assert played is not None and played.say == "Playing Mr. Brightside by The Killers."
    assert fake.played == [{"device": "pc1", "uris": ["t:mb"]}]
    assert fake.auth == ["Bearer tok"]


def test_song_by_artist_uses_a_fielded_search(tmp_path):
    fake = FakeSpotify()
    played, _, _ = asyncio.run(_ask(tmp_path, fake, "Play Bohemian Rhapsody by Queen", 18302))
    assert played is not None and played.say == "Playing Bohemian Rhapsody - Remastered 2011 by Queen."
    assert fake.played[0]["uris"] == ["t:br"]


def test_some_artist_plays_the_artist(tmp_path):
    fake = FakeSpotify()
    played, _, _ = asyncio.run(_ask(tmp_path, fake, "Play some Taylor Swift.", 18303))
    assert played is not None and played.say == "Playing Taylor Swift." and played.how == "artist"
    assert fake.played[0]["context_uri"] == "a:ts"


def test_my_playlist_by_name(tmp_path):
    fake = FakeSpotify()
    played, _, _ = asyncio.run(_ask(tmp_path, fake, "Switch to my metal and house fusion playlist", 18304))
    assert played is not None and played.say == "Playing your Metal and House Fusion playlist."
    assert fake.played[0]["context_uri"] == "p:mhf"


def test_loose_ask_plays_the_models_picks_that_exist(tmp_path):
    picks = json.dumps({"say": "Mellow jazz for the coffee", "songs": [
        ["So What", "Miles Davis"], ["Made Up Song", "Nobody"], ["Take Five", "Dave Brubeck"],
        ["Blue in Green", "Miles Davis"]]})
    fake = FakeSpotify(picks)
    played, _, _ = asyncio.run(_ask(tmp_path, fake, "Play something to go with my morning coffee", 18305))
    assert played is not None and played.say == "Mellow jazz for the coffee." and played.how == "picks"
    assert fake.played[0]["uris"] == ["t:sw", "t:t5", "t:big"]         # the made-up one is dropped, order kept
    assert fake.pick_auth == [None]                                      # never an API key: the proxy signs


def test_no_strong_match_lets_the_model_pick(tmp_path):
    picks = json.dumps({"say": "Here you go", "songs": [["So What", "Miles Davis"], ["Take Five", "Dave Brubeck"],
                                                        ["Blue in Green", "Miles Davis"]]})
    fake = FakeSpotify(picks)
    played, _, _ = asyncio.run(_ask(tmp_path, fake, "Play that one jazzy thing", 18306))
    assert played is not None and played.how == "picks"


def test_controls_are_one_call(tmp_path):
    fake = FakeSpotify()
    played, _, _ = asyncio.run(_ask(tmp_path, fake, "Skip this song.", 18307))
    assert played is not None and played.say == "Skipping."
    assert "POST /v1/me/player/next" in fake.calls


def test_a_401_refreshes_through_deep_once_and_retries(tmp_path):
    fake = FakeSpotify(fail_play=1)
    played, refreshed, _ = asyncio.run(_ask(tmp_path, fake, "Play Mr. Brightside.", 18308))
    assert played is not None and refreshed == [True]
    assert fake.played == [{"device": "pc1", "uris": ["t:mb"]}]


def test_failure_hands_the_turn_to_deep(tmp_path):
    fake = FakeSpotify(picks="no json here")
    played, _, _ = asyncio.run(_ask(tmp_path, fake, "Play some 90s hip hop", 18309))
    assert played is None and fake.played == []


def test_off_flag_hands_every_turn_to_deep(tmp_path):
    (tmp_path / "music-fast.off").touch()
    fake = FakeSpotify()
    played, _, _ = asyncio.run(_ask(tmp_path, fake, "Play Mr. Brightside.", 18310))
    assert played is None and fake.played == []
