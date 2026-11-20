"""An in-memory stand-in for SpotifyBackend, driven by recorded-shape cluster fixtures."""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path
from typing import Any

from spotapi.models.playback import Device, PlaybackTrack, PlayerState
from spotapi.types.exceptions import HTTPError

FIXTURES = Path(__file__).parent / "fixtures"


def load_cluster(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeBackend:
    def __init__(self, cluster: dict, *, contexts: dict[str, list[str]] | None = None,
                 queue_cap: int | None = None, short_readback: bool = False,
                 missing_contexts: set[str] = frozenset(), error: Exception | None = None) -> None:
        self.cluster = copy.deepcopy(cluster)
        self.contexts = contexts or {}
        self.queue_cap = queue_cap
        self.short_readback = short_readback
        self.missing_contexts = set(missing_contexts)
        self.error = error
        self.log: list[str] = []
        self.playlists: dict[str, list[str]] = {}
        self.private: set[str] = set()
        self.selected: str | None = None
        self._uid = itertools.count(1000)

    async def __aenter__(self) -> "FakeBackend":
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    # ---- helpers -----------------------------------------------------------
    @property
    def ps(self) -> dict:
        return self.cluster["player_state"]

    def _visible(self) -> dict:
        c = copy.deepcopy(self.cluster)
        if self.queue_cap is not None and "next_tracks" in c["player_state"]:
            nt = c["player_state"]["next_tracks"]
            q = [t for t in nt if t.get("provider") == "queue"][: self.queue_cap]
            c["player_state"]["next_tracks"] = q + [t for t in nt if t.get("provider") != "queue"]
        return c

    def _entry(self, t: Any) -> dict:
        if isinstance(t, PlaybackTrack):
            return {"uri": t.uri, "uid": t.uid or f"u{next(self._uid)}", "provider": t.provider or "context"}
        return {"uri": t, "uid": f"u{next(self._uid)}", "provider": "queue"}

    # ---- backend surface ---------------------------------------------------
    async def refresh(self) -> dict:
        self.log.append("refresh")
        return self._visible()

    @property
    def state(self) -> PlayerState:
        return PlayerState.from_cluster(self._visible())

    @property
    def devices(self) -> tuple[Device, ...]:
        active = self.cluster.get("active_device_id")
        return tuple(Device(i, d.get("name", ""), d.get("device_type", ""), None, True, i == active)
                     for i, d in self.cluster.get("devices", {}).items())

    async def add_to_queue(self, uri: str) -> None:
        self.log.append("add_to_queue")
        nt = self.ps["next_tracks"]
        n = sum(1 for t in nt if t.get("provider") == "queue")
        nt.insert(n, self._entry(uri))

    async def set_queue(self, tracks: list[Any]) -> None:
        self.log.append("set_queue")
        self.ps["next_tracks"] = [self._entry(t) for t in tracks]

    def select(self, device_id: str) -> None:
        self.selected = device_id
        self.cluster["active_device_id"] = device_id

    async def play(self, uri: str, *, track: str | None, position_ms: int) -> None:
        self.log.append("play")
        if uri in self.missing_contexts:
            raise HTTPError("context not found", status_code=404)
        if not self.cluster.get("active_device_id"):
            raise RuntimeError("no device")
        items = self.contexts.get(uri, [uri] if uri.startswith("spotify:track:") else [])
        start = items.index(track) if track in items else 0
        cur = items[start] if items else (track or uri)
        self.ps["track"] = {"uri": cur, "uid": f"u{next(self._uid)}", "provider": "context"}
        self.ps["context_uri"] = uri
        self.ps["position_as_of_timestamp"] = position_ms
        self.ps["is_paused"] = False
        self.ps["is_playing"] = True
        self.ps["next_tracks"] = [{"uri": u, "uid": f"u{next(self._uid)}", "provider": "context"}
                                  for u in items[start + 1:]]

    async def seek(self, position_ms: int) -> None:
        self.log.append("seek")
        self.ps["position_as_of_timestamp"] = position_ms

    async def shuffle(self, enabled: bool) -> None:
        self.log.append("shuffle")
        self.ps.setdefault("options", {})["shuffling_context"] = enabled

    async def repeat(self, mode: str) -> None:
        self.log.append("repeat")
        o = self.ps.setdefault("options", {})
        o["repeating_context"] = mode == "context"
        o["repeating_track"] = mode == "track"

    async def create_playlist(self, name: str) -> str:
        self.log.append("create_playlist")
        uri = f"spotify:playlist:fake{len(self.playlists)}"
        self.playlists[uri] = []
        return uri

    async def set_private(self, uri: str) -> None:
        self.log.append("set_private")
        self.private.add(uri)

    async def add_tracks(self, uri: str, uris: list[str]) -> None:
        self.log.append("add_tracks")
        self.playlists[uri].extend(uris)

    async def playlist_uris(self, uri: str) -> list[str]:
        self.log.append("playlist_uris")
        got = list(self.playlists[uri])
        return got[:-1] if self.short_readback else got

    async def delete_playlist(self, uri: str) -> None:
        self.log.append("delete_playlist")
        self.playlists.pop(uri, None)
