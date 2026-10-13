"""Test isolation: the suite must never read or write a live bridge's state.
Set before any bridge module is imported: no clip library unless a test points at one, a throwaway credit
counter with plenty of credits, throwaway session/inbox/status dirs. Importing dsp builds bridge/_dsp.so."""
import os
import tempfile

import dsp  # noqa: F401  # builds _dsp.so when it is missing or older than dsp.c

_tmp = tempfile.mkdtemp(prefix="pod-tests-")
os.environ["SKWOOP_LIBRARY"] = "0"
os.environ.pop("SKWOOP_LIBRARY_DIR", None)
os.environ["ELEVEN_CREDIT_STATE"] = os.path.join(_tmp, "eleven-credits.json")
os.environ["ELEVEN_CREDIT_BALANCE"] = "100000"
os.environ.pop("ELEVEN_CREDIT_FLOOR", None)
os.environ.setdefault("SKWOOP_SESSION_DIR", os.path.join(_tmp, "sessions"))
os.environ.setdefault("STATUS_FILE", os.path.join(_tmp, "status.json"))
os.environ.setdefault("INBOX_DIR", os.path.join(_tmp, "inbox"))
