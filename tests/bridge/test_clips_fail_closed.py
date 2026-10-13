"""clips.idx missing/stale/corrupt must not spend ElevenLabs."""
from __future__ import annotations

from pathlib import Path

import pytest

from providers import TTS
from skwoop_lib import SkwoopLibrary


def test_healthz_ok_false_when_library_not_ready(tmp_path: Path) -> None:
    from lean_http import Application
    import skwoop_bridge as pb

    lib = SkwoopLibrary(tmp_path, enabled=True)  # no clips.idx
    assert not lib.ready and lib.error
    # Build a minimal app bag and call healthz's library branch logic.
    class FakeTTS:
        provider = "elevenlabs"
        library = lib
        def describe(self):  # noqa: ANN201
            return {"model": "x", "fallback": None, "last_backend": None, "fallbacks": 0,
                    "last_error": None, "voice": "v", "fallback_voice": None, "credits": {}}

    app = Application()
    app["stt"] = type("S", (), {"provider": "xai"})()
    app["llm"] = type("L", (), {"mode": "xai"})()
    app["tts"] = FakeTTS()
    app["http"] = None

    async def go() -> None:
        # Call healthz through the real handler with a stub request.
        # SAFETY: healthz only reads request.app; the stub supplies that attribute.
        r = await pb.healthz(type("Req", (), {"app": app})())  # type: ignore[arg-type]
        import json
        body = json.loads(r.body.decode() if isinstance(r.body, (bytes, bytearray)) else r.body)
        assert body["ok"] is False
        assert body["library"]["ready"] is False

    import asyncio
    asyncio.run(go())


def test_synth_refuses_live_when_library_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTS_PROVIDER", "say")
    monkeypatch.setenv("XAI_API_KEY", "test")
    lib = SkwoopLibrary(tmp_path, enabled=True)
    # Avoid constructing a full TTS (needs provider keys); exercise the fail-closed branch directly.
    class T:
        library = lib
        last_backend = ""
        last_error = ""
        def _from_library(self, text, mood=None):
            return TTS._from_library(self, text, mood)  # type: ignore[arg-type]
    t = T()
    out = TTS._from_library(t, "Hello there friend.", None)  # type: ignore[arg-type]
    assert out == b""
    assert t.last_backend == "library-down"


def test_clip_texts_does_not_cache_empty_on_error(tmp_path: Path) -> None:
    lib = SkwoopLibrary(tmp_path, enabled=False)
    lib.idx = object()  # pretend loaded
    with pytest.raises(LookupError):
        lib._clip_texts()
    assert lib._texts is None
