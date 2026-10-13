"""Texting from the pod: the house's own contact list and send path, and never without a spoken "yes".

Contacts: ~/.skwoop/contacts.yaml (SKWOOP_CONTACTS_FILE).
Send:     python3 ~/.skwoop/send_text.py +E164 FILE (SKWOOP_TEXT_SENDER). Exit 0 = landed, 1 = failed,
          2 = the sender is waiting on an Allow dialog (stop, don't retry).

A text is only proposed by the fast tier. The bridge reads it back ("Text Mom: ... Send it?") and sends on the next
turn's "yes"; anything else drops it. Nobody outside the contact list can be texted, and no number is ever guessed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger("skwoop.messaging")

DEFAULT_CONTACTS = Path.home() / ".skwoop" / "contacts.yaml"
DEFAULT_SENDER = Path.home() / ".skwoop" / "send_text.py"
CONFIRM_S = 60.0
ALIASES = {
    "mom": ("mom", "mother", "mum", "mama"),
    "dad": ("dad", "father", "papa", "pop"),
    "alex": ("alex", "me", "myself"),
    "mel": ("mel", "wife", "the wife"),
}
_YES = re.compile(r"^(yes|yeah|yep|yup|sure|send( it)?|do it|go ahead|confirm|ok(ay)?( send( it)?)?|please do)\b")
_NO = re.compile(r"^(no|nope|don'?t|cancel|stop|never ?mind|forget it|wait)\b")


class ContactsError(ValueError):
    pass


@dataclass(frozen=True)
class Contact:
    key: str            # mom | dad | alex (the file's top-level keys)
    name: str           # "Alex Example"
    phone: str          # +E164

    @property
    def spoken(self) -> str:
        return self.key.capitalize() if self.key in ALIASES else self.name


def parse_contacts(text: str) -> dict[str, Contact]:
    """family_contacts.yaml -> contacts. The file is two levels deep (key: / name: / phone: / note:); anything else
    is an error, not a guess."""
    out: dict[str, Contact] = {}
    key: str | None = None
    fields: dict[str, str] = {}

    def close() -> None:
        if key is None:
            return

        phone = fields.get("phone", "")
        if not re.fullmatch(r"\+\d{10,15}", phone):
            raise ContactsError(f"{key}: phone {phone!r} is not +E164")

        out[key] = Contact(key, fields.get("name") or key, phone)

    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue

        if not line.startswith(" "):
            m = re.fullmatch(r"([a-z0-9_]+):", line)
            if m is None:
                raise ContactsError(f"line {n}: expected 'name:'")
            close()
            key, fields = m.group(1), {}
            continue

        m = re.fullmatch(r"\s+([a-z_]+):\s*(.*)", line)
        if m is None or key is None:
            raise ContactsError(f"line {n}: expected '  field: value'")
        fields[m.group(1)] = m.group(2).strip().strip('"').strip("'")

    close()

    return out


def load_contacts(path: Path | None = None) -> dict[str, Contact]:
    path = path or Path(os.environ.get("SKWOOP_CONTACTS_FILE") or DEFAULT_CONTACTS).expanduser()
    try:
        contacts = parse_contacts(path.read_text())
    except (OSError, ContactsError) as e:
        log.warning("texting off: contacts %s unreadable (%s)", path, e)

        return {}

    log.info("texting: %d contacts from %s (%s)", len(contacts), path, ", ".join(c.spoken for c in contacts.values()))

    return contacts


def find_contact(contacts: dict[str, Contact], said: str) -> Contact | None:
    """'Mom', 'my mom', 'Alex Example', 'my wife' (Mel) -> the contact; an unknown name -> None (never a guess)."""
    s = " ".join(re.sub(r"[^a-z ]", " ", said.lower()).split())
    s = re.sub(r"^((my|to) )+", "", s)                        # "to my wife" -> "wife"
    for key, c in contacts.items():
        names = {key, c.name.lower(), *ALIASES.get(key, ())}
        if s in names:
            return c

    return None


@dataclass(frozen=True)
class PendingText:
    to: Contact
    body: str
    at: float

    @property
    def readback(self) -> str:
        return f"Text {self.to.spoken}: {self.body} Send it?"


SendResult = Literal["sent", "failed", "blocked"]
Sender = Callable[[str, str], Awaitable[SendResult]]


def reliable_sender(script: Path | None = None, timeout_s: float = 45.0) -> Sender:
    script = script or Path(os.environ.get("SKWOOP_TEXT_SENDER") or DEFAULT_SENDER).expanduser()

    async def send(phone: str, body: str) -> SendResult:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(body)
        try:
            p = await asyncio.create_subprocess_exec("/usr/bin/python3", str(script), phone, f.name,
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, _ = await asyncio.wait_for(p.communicate(), timeout_s)
            except TimeoutError:
                p.kill()
                log.error("text to %s: sender timed out", phone[:-4] + "****")

                return "failed"
        finally:
            Path(f.name).unlink(missing_ok=True)

        ok = p.returncode == 0
        try:
            ok = ok and bool(json.loads(out.decode() or "{}").get("ok", True))
        except json.JSONDecodeError:
            pass
        log.info("text to %s: exit %s", phone[:-4] + "****", p.returncode)
        if p.returncode == 2:
            return "blocked"

        return "sent" if ok else "failed"

    return send


class Texts:
    """Pending texts per pod, and the confirm turn."""

    def __init__(self, contacts: dict[str, Contact], sender: Sender, clock: Callable[[], float] = time.monotonic):
        self.contacts = contacts
        self.sender = sender
        self.clock = clock
        self.pending: dict[str, PendingText] = {}

    def propose(self, device: str, to: str, body: str) -> tuple[PendingText | None, str]:
        """(pending, what the model is told). Unknown recipient -> no pending, and the model hears who it can text."""
        contact = find_contact(self.contacts, to)
        if contact is None:
            names = ", ".join(c.spoken for c in self.contacts.values()) or "nobody"
            return None, f"Not sent: {to!r} is not in the family contacts. I can text: {names}."

        body = " ".join(body.split())[:300]
        if body and body[-1] not in ".!?":
            body += "."
        pt = PendingText(contact, body, self.clock())
        self.pending[device] = pt

        return pt, f"Waiting for Alex to confirm: {pt.readback}"

    def waiting(self, device: str) -> PendingText | None:
        pt = self.pending.get(device)
        if pt is not None and self.clock() - pt.at > CONFIRM_S:
            self.pending.pop(device, None)
            return None

        return pt

    async def answer(self, device: str, heard: str) -> str | None:
        """The turn after a readback: "yes" sends, "no" drops; None = not an answer (dropped, route it normally)."""
        pt = self.waiting(device)
        if pt is None:
            return None

        self.pending.pop(device, None)
        t = " ".join(re.sub(r"[^a-z' ]", " ", heard.lower()).split())
        if _NO.search(t):
            return "Okay, I won't send it."

        if not _YES.search(t):
            return None

        result = await self.sender(pt.to.phone, pt.body)
        if result == "sent":
            return f"Sent to {pt.to.spoken}."

        if result == "blocked":
            return "Messages needs a tap before it can send. I stopped there."

        return "That text didn't go through."
