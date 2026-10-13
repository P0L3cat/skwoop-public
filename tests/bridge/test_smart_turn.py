"""Smart Turn event parser. No network."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from providers import stt_event_text  # noqa: E402


def test_speech_final():
    text, done = stt_event_text({
        "type": "transcript.partial",
        "text": "turn on the trumpet",
        "is_final": True,
        "speech_final": True,
        "end_of_turn_confidence": 0.95,
    })
    assert text == "turn on the trumpet"
    assert done is True


def test_chunk_final_not_done():
    text, done = stt_event_text({
        "type": "transcript.partial",
        "text": "the number is 956",
        "is_final": True,
        "speech_final": False,
    })
    assert "956" in text
    assert done is False


def test_interim_not_done():
    text, done = stt_event_text({
        "type": "transcript.partial",
        "text": "hello",
        "is_final": False,
        "speech_final": False,
    })
    assert text == "hello"
    assert done is False


def test_done_event():
    text, done = stt_event_text({"type": "transcript.done", "text": "full"})
    assert text == "full" and done is True


def test_created_ignored():
    text, done = stt_event_text({"type": "transcript.created"})
    assert text == "" and done is False


if __name__ == "__main__":
    test_speech_final()
    test_chunk_final_not_done()
    test_interim_not_done()
    test_done_event()
    test_created_ignored()
    print("SMART TURN OK")
