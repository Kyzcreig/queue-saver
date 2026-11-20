"""The only module that talks to spotapi. Every call is recorded in ``log`` by name.

A test double (tests/fakes.py) implements the same surface, so park/restore/probe
logic is tested without the network.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
from contextlib import AsyncExitStack
from typing import Any, Mapping, Sequence

PINNED_SPOTAPI = "2.1.0"

# Names a park run may log. Anything else (play, seek, next, previous, pause, resume,
# transfer, volume, add_to_queue) during a park breaks the never-skip invariant.
PARK_ALLOWED = {
    "refresh",
    "create_playlist",
    "set_private",
    "add_tracks",
    "playlist_uris",
    "set_queue",
}


def installed_spotapi() -> str:
    return importlib.metadata.version("spotapi")


def assert_pinned() -> str:
    v = installed_spotapi()
    if v != PINNED_SPOTAPI:
        raise RuntimeError(f"spotapi {v} installed, {PINNED_SPOTAPI} pinned; refusing to run")
    return v


class SpotifyBackend:
    """Live backend over spotapi. Use as ``async with SpotifyBackend(cookie) as sp``."""

    def __init__(self, cookie_header: str) -> None:
        self._cookie = cookie_header
        self._stack = AsyncExitStack()
        self.log: list[str] = []
        self.player: Any = None
        self.client: Any = None
        self.raw: Mapping[str, Any] = {}

    async def __aenter__(self) -> "SpotifyBackend":
        from spotapi import AsyncClient, Player

        class _RawPlayer(Player):  # keeps the raw cluster so missing fields can be detected
            def _update_cluster(inner, cluster):  # noqa: N805
                if "player_state" in cluster:
                    self.raw = cluster
                return super()._update_cluster(cluster)

        client = await AsyncClient.from_cookies(self._cookie)
        self._cookie = ""  # drop our copy; the client owns the session now
        self.client = await self._stack.enter_async_context(client)
        self.player = _RawPlayer(self.client, name="queue-saver")
        await self._stack.enter_async_context(self.player.connect())
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    # ---- reads -------------------------------------------------------------
    async def refresh(self) -> Mapping[str, Any]:
        self.log.append("refresh")
        await self.player.refresh()
        return self.raw

    @property
    def state(self):
        return self.player.state

    @property
    def devices(self):
        return self.player.devices

    # ---- queue ---------------------------------------------------------------
    async def add_to_queue(self, uri: str) -> None:
        self.log.append("add_to_queue")
        await self.player.add_to_queue(uri)

    async def set_queue(self, tracks: Sequence[Any]) -> None:
        self.log.append("set_queue")
        await self.player.set_queue(list(tracks))

    # ---- playback (restore only) ----------------------------------------------
    def select(self, device_id: str) -> None:
        self.player.select(device_id)

    async def play(self, uri: str, *, track: str | None, position_ms: int) -> None:
        self.log.append("play")
        await self.player.play(uri, track=track, position_ms=position_ms)

    async def seek(self, position_ms: int) -> None:
        self.log.append("seek")
        await self.player.seek(position_ms)

    async def shuffle(self, enabled: bool) -> None:
        self.log.append("shuffle")
        await self.player.shuffle(enabled)

    async def repeat(self, mode: str) -> None:
        self.log.append("repeat")
        await self.player.repeat(mode)

    async def wait_state(self, timeout: float = 10) -> None:
        try:
            await self.player.wait_for("state_update", timeout=timeout)
        except TimeoutError:
            pass

    # ---- playlists -------------------------------------------------------------
    async def create_playlist(self, name: str) -> str:
        from spotapi import PlaylistHandler

        self.log.append("create_playlist")
        pl = await PlaylistHandler(self.client).create_playlist(name)
        return pl.playlist_id

    async def set_private(self, uri: str) -> None:
        from spotapi import Playlist

        self.log.append("set_private")
        await Playlist(self.client, uri).set_published(False)

    async def add_tracks(self, uri: str, uris: Sequence[str]) -> None:
        from spotapi import Playlist

        self.log.append("add_tracks")
        pl = Playlist(self.client, uri)
        for i in range(0, len(uris), 100):
            await pl.add_tracks(*uris[i : i + 100])

    async def playlist_uris(self, uri: str) -> list[str]:
        from spotapi import Playlist

        self.log.append("playlist_uris")
        out: list[str] = []
        async for entry in Playlist(self.client, uri).items():
            out.append(entry.uri or "")
        return out

    async def delete_playlist(self, uri: str) -> None:
        from spotapi import Playlist

        self.log.append("delete_playlist")
        await Playlist(self.client, uri).delete()


async def settle(seconds: float = 0.6) -> None:
    await asyncio.sleep(seconds)
