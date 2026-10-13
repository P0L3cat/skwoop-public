"""The 3-tier router: rules only. The cases are the ones the old 2B router got wrong on the bridge host (Oct 2 eval)."""
from __future__ import annotations

import asyncio

import pytest

import tools.ha_service as house
import router.quick as quick
import router


def _route(text: str) -> router.Route:
    answers = quick.QuickAnswers(quick.WeatherSource(lambda: asyncio.sleep(0)),
                                 quick.TimerBook(ring=lambda d, r, s: asyncio.sleep(0)))
    return router.route(text, lambda t: answers.match(t, "dev"), lambda t: house.match(t))


@pytest.mark.parametrize(("said", "tier", "why"), [
    ("What's the weather like right now?", "instant", "quick-WeatherAsk"),
    ("Set a timer for ten minutes.", "instant", "quick-TimerSet"),
    ("Turn on the lights.", "instant", "script-living_on"),
    ("Turn off the living room light.", "instant", "script-living_off"),
    ("Can you turn off the bedroom light?", "instant", "script-bedroom_off"),      # a polite command is a command
    ("Pause the TV.", "instant", "script-tv_pause"),
    # questions, "don't", two asks, later, a level, the other room: never the script
    ("Is the bedroom light on?", "fast", "fast-not-a-command-question"),
    ("Are the lights on?", "fast", "fast-not-a-command-question"),
    ("Don't turn on the lights.", "fast", "fast-not-a-command-negated"),
    ("Turn on the lights and set a timer for five minutes.", "fast", "fast-not-a-command-compound"),
    ("Turn off the lights in ten minutes.", "fast", "fast-not-a-command-later"),
    ("Turn on the lights to thirty percent.", "fast", "fast-not-a-command-modifier"),
    ("Turn on the lights in the bedroom.", "fast", "fast-not-a-command-other-room"),
    ("Turn off all the lights.", "fast", "fast"),                                 # both lights: the tool
    ("Don't set a timer.", "fast", "fast"),
    # Oct 4: "play X" is Spotify unless Alex says TV or show
    ("Play some music.", "deep", "deep-music"),
    ("Play Hurry Up and Wait by Ben Miller Band.", "deep", "deep-music"),
    ("Play Severance.", "deep", "deep-music"),
    ("Play Show Me How.", "deep", "deep-music"),                                   # a song, not "the show"
    ("Can you play some jazz?", "deep", "deep-music"),
    ("Pause.", "deep", "deep-music"),
    ("Pause the music.", "deep", "deep-music"),
    ("Skip this song.", "deep", "deep-music"),
    ("Next song.", "deep", "deep-music"),
    ("What's playing?", "deep", "deep-music"),
    ("Switch to my metal and house fusion playlist.", "deep", "deep-music"),     # was fast, then escalated
    ("Put on some music for dinner.", "deep", "deep-music"),
    ("Play Severance on TV.", "instant", "script-tv_play_show"),
    ("Play the show Severance.", "instant", "script-tv_play_show"),
    ("Play the TV show Severance.", "instant", "script-tv_play_show"),           # not tv_play ("play the tv")
    ("Watch The Office.", "instant", "script-tv_play_show"),
    ("Play the TV.", "instant", "script-tv_play"),
    ("Could you play Severance on the TV?", "fast", "fast"),                       # apple_tv tool
    ("Stop.", "fast", "fast"),
    ("Next.", "fast", "fast"),
    ("What time is it in Tokyo?", "fast", "fast"),                                 # not the local clock
    # live data and status: the fast tier with tools, never plain chat
    ("Can you check what the bots are working on", "fast", "fast"),
    ("Is Mel home?", "fast", "fast"),
    ("What's the price of bitcoin?", "fast", "fast"),
    ("Add milk to the shopping list.", "fast", "fast"),
    # deep
    ("What's in my notes about the dishwasher?", "deep", "deep-docs"),
    ("Do you remember what I said about the trip?", "deep", "deep-memory"),
    ("Compare the two best robot vacuums for pet hair.", "deep", "deep-research"),
    ("Has the Starlink ping been unusual today?", "deep", "deep-analysis"),
    ("Write a note about the dishwasher.", "deep", "deep-docs"),
    ("Summarize the latest Home Assistant release for me.", "deep", "deep-research"),
    ("Compare the prices of the two cheapest flights to Denver.", "deep", "deep-research"),
    ("Compare my notes on the two contractors.", "deep", "deep-docs"),
    ("Remember that my wife's birthday is in May.", "deep", "deep-memory"),
    ("What did I tell you about my wife?", "deep", "deep-memory"),
    # short facts and small talk: the fast tier (Oct 3)
    ("Can you compare cats and dogs as pets in one sentence?", "fast", "fast-soft-compare"),
    ("What are the pros and cons of electric cars?", "fast", "fast-soft-compare"),
    ("Do you remember who my wife is?", "fast", "fast-memory-fact"),
    ("Who's my wife?", "fast", "fast-memory-fact"),
    ("What's my dog's name?", "fast", "fast-memory-fact"),
])
def test_route(said, tier, why):
    r = _route(said)
    assert (r.tier, r.why) == (tier, why)


def test_routing_never_fires_anything(monkeypatch):
    async def boom(http, hit):
        raise AssertionError("route() must only look things up")

    monkeypatch.setattr(house, "_fire", boom)
    assert _route("Turn on the lights.").hit is not None


@pytest.mark.parametrize("said", ["", "   ", "?!"])
def test_empty_goes_to_fast(said):
    assert _route(said).tier == "fast"


@pytest.mark.parametrize(("said", "hold"), [
    ("Is the TV on?", "only asked a question"),
    ("What's the bedroom light at?", "only asked a question"),
    ("Don't turn on the lights.", "said not to do it"),
    ("Please don't text Mom.", "said not to do it"),
    ("Can you text Mom that I'm late?", None),
    ("Could you dim the lamp?", None),
    ("Turn off the TV.", None),
    ("Don't forget to add milk.", None),        # "don't forget" is a request
    ("Turn the lights off in ten minutes.",
     "asked for it to happen later, and the pod can't schedule anything; say so"),
    ("Set a timer for ten minutes.", None),
])
def test_hold_actions(said, hold):
    assert router.hold_actions(said) == hold


@pytest.mark.parametrize("heard,want", [
    ("What's on my calendar today?", ("calendar", {"day_offset": 0, "days": 1})),
    ("What do I have tomorrow?", ("calendar", {"day_offset": 1, "days": 1})),
    ("Can you check what the bots are working on?", ("bot_status", {})),
    ("What are the bots doing?", ("bot_status", {})),
    ("Will it rain this weekend?", ("forecast", {})),
    ("Do I need an umbrella tomorrow?", ("forecast", {})),
    ("What's on my calendar this week?", None),
    ("What's on my calendar on Monday?", None),
    ("Tell the bots to check the status", None),
    ("Will it rain in Austin tomorrow?", None),
    ("Is it raining right now?", None),
    ("Don't check my calendar", None),
    ("Check my calendar and turn on the lights", None),
    ("What's the latest SpaceX news?", None),
    ("Text Mel that I'm running late", None),
    ("Find the dishwasher warranty", None),
    ("Do you remember who my wife is?", ("recall_memory", {"query": "Do you remember who my wife is?"})),
    ("What's my dog's name?", ("recall_memory", {"query": "What's my dog's name?"})),
    ("Do you remember what I said about the trip?", None),
    ("Who's home?", None),
])
def test_prepick_only_takes_certain_single_reads(heard, want):
    assert router.prepick(heard) == want
