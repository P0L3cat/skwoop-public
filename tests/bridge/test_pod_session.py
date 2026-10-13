"""Pod Deep session rotates after idle timeout. No network."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SKWOOP_SESSION_DIR"] = tempfile.mkdtemp()
os.environ["SKWOOP_SESSION_IDLE_S"] = "3600"

import harness.skwoop_turn as skwoop_turn  # noqa: E402
from pathlib import Path  # noqa: E402

# another test may have imported skwoop_turn first (env read at import): pin the dir so we never
# touch the real ~/.skwoop/sessions
skwoop_turn.SESSION_DIR = Path(os.environ["SKWOOP_SESSION_DIR"])
skwoop_turn.SESSION_IDLE_S = 3600.0


def test_new_then_continue():
    now = time.time()
    a = skwoop_turn.load_session("desk", now=now)
    assert a["turns"] == []
    assert skwoop_turn.context_blob(a) == "(new pod session)"
    a["last_at"] = now
    a["turns"] = [{"heard": "lights?", "reply": "They're on."}]
    a["rotated"] = False
    skwoop_turn.save_session("desk", a)
    b = skwoop_turn.load_session("desk", now=now + 10)
    assert b.get("rotated") is False
    assert "They're on." in skwoop_turn.context_blob(b)


def test_idle_hour_rotates():
    now = time.time()
    skwoop_turn.save_session(
        "kitchen",
        {
            "started_at": now - 4000,
            "last_at": now - 4000,
            "turns": [{"heard": "old", "reply": "stale"}],
        },
    )
    c = skwoop_turn.load_session("kitchen", now=now)
    assert c["rotated"] is True
    assert c["turns"] == []
    assert skwoop_turn.context_blob(c) == "(new pod session)"


if __name__ == "__main__":
    test_new_then_continue()
    test_idle_hour_rotates()
    print("SESSION OK")
