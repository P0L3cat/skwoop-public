"""Skwoop - device id (MAC) -> room name, for logs, Grok context and room targeting.

SKWOOP_ROOMS can be any of:
  a JSON object      SKWOOP_ROOMS='{"a0:b7:65:12:34:56":"kitchen","a0:b7:65:ab:cd:ef":"office"}'
  a JSON file path   SKWOOP_ROOMS=~/.skwoop/rooms.json   (re-read when it changes; no restart)
  a short list       SKWOOP_ROOMS='a0:b7:65:12:34:56=kitchen,a0:b7:65:ab:cd:ef=office'
Unknown devices are named SKWOOP_DEFAULT_ROOM if set, else "pod-<last 4 hex of the MAC>".
The bridge logs "OTA check-in from <mac> (room ...)" so new pods are easy to add.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("skwoop.rooms")
_ROOM_RE = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,23}$")


def norm_device(device_id: str) -> str:
    return (device_id or "").strip().lower()


def norm_room(room: str | None) -> str | None:
    if room is None:
        return None
    r = str(room).strip().lower()
    return r or None


class Rooms:
    def __init__(self, spec: str | None = None):
        self.spec = (spec if spec is not None else os.environ.get("SKWOOP_ROOMS", "")).strip()
        self.default = norm_room(os.environ.get("SKWOOP_DEFAULT_ROOM", ""))
        self._map: dict[str, str] = {}
        self._mtime = None
        self._load()

    def _path(self) -> Path | None:
        if not self.spec or self.spec.startswith("{") or "=" in self.spec:
            return None
        return Path(self.spec).expanduser()

    def _load(self) -> None:
        raw: dict = {}
        try:
            p = self._path()
            if p is not None:
                self._mtime = p.stat().st_mtime
                raw = json.loads(p.read_text())
            elif self.spec.startswith("{"):
                raw = json.loads(self.spec)
            elif self.spec:
                raw = dict(kv.split("=", 1) for kv in self.spec.split(",") if "=" in kv)
        except FileNotFoundError:
            log.warning("SKWOOP_ROOMS file not found; every pod gets a default name")
        except (ValueError, OSError) as e:
            log.warning("SKWOOP_ROOMS unreadable (%s); every pod gets a default name", e)
        m = {}
        for dev, room in raw.items():
            r = norm_room(room)
            if r and _ROOM_RE.match(r):
                m[norm_device(dev)] = r
            else:
                log.warning("ignoring room name %r (use 1-24 chars: a-z 0-9 space _ -)", room)
        self._map = m

    def _maybe_reload(self) -> None:
        p = self._path()
        if p is None:
            return
        try:
            if p.stat().st_mtime != self._mtime:
                self._load()
        except FileNotFoundError:
            pass

    def name(self, device_id: str) -> str:
        self._maybe_reload()
        dev = norm_device(device_id)
        if dev in self._map:
            return self._map[dev]
        if dev.startswith("web-pod-"):          # a browser pod (webpod/) is its own room unless SKWOOP_ROOMS says else
            return dev
        if self.default:
            return self.default
        tail = re.sub(r"[^0-9a-f]", "", dev)[-4:] or "0000"
        return f"pod-{tail}"

    def known(self) -> set[str]:
        self._maybe_reload()
        return set(self._map.values())

    def valid_target(self, room: str | None) -> bool:
        """With a configured map, a target room must be one of its names (catches typos)."""
        r = norm_room(room)
        return r is None or not self._map or r in self._map.values()
