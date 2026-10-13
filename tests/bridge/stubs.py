"""The STT surface skwoop_bridge uses (providers.STT), for tests that don't transcribe real audio: a cloud-style STT
with no local model and no streaming. Subclass it and override transcribe()."""


class StubSTT:
    provider = "stub"
    fallback = ""
    local = None
    last_backend = ""

    async def start(self) -> None:
        return None

    def streaming_enabled(self) -> bool:
        return False

    async def transcribe(self, pcm: bytes, rate: int) -> str:
        return ""

    def describe(self) -> dict[str, str | None]:
        return {"provider": self.provider, "fallback": None, "last_backend": None}


class StubLLM:
    """The LLM surface skwoop_bridge uses (providers.LLM) for tests that never reach a reply."""

    mode = "stub"

    async def stream_sentences(self, messages: list[dict[str, str]], fast: bool = False):
        return
        yield ""      # an async generator that yields nothing


class StubTTS:
    """The TTS surface (providers.TTS / house.AckSpeaker): silence, no library."""

    provider = "stub"
    library = None
    last_backend = ""

    async def synth(self, text: str, mood: str | None = None, previous_text: str | None = None,
                    next_text: str | None = None) -> bytes:
        return b"\x00\x00" * 2400
