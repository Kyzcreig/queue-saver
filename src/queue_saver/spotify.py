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

    # Most queued entries a state read is known to show in full. None: unknown (spotapi 2.1.0
    # says nothing about truncation of next_tracks), so park never clears by replacement.
    queue_cap: int | None = None

    def __init__(self, cookie_header: str) -> None:
        self._cookie = cookie_header
        self._stack = AsyncExitStack()
        self.log: list[str] = []
        self.player: Any = None
        self.client: Any = None
        self.raw: Mapping[str, Any] = {}
        self._ph: Any = None

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

    async def pause(self) -> None:
        self.log.append("pause")
        await self.player.pause()

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
    async def _handler(self) -> Any:
        """A PlaylistHandler whose username is known BEFORE any playlist is created.

        spotapi 2.1.0 resolves the username lazily inside create_playlist, after the playlist
        already exists; on Ace's account its Profile model rejects the live response
        ("Profile.avatar: value does not match str | None"), which strands an orphan playlist
        outside the library. So resolve the username first, from the raw query if needed.
        """
        from spotapi import PlaylistHandler
        from spotapi.operations import Operations
        from spotapi.types.exceptions import BaseClientError

        if self._ph is not None:
            return self._ph
        h = PlaylistHandler(self.client)
        try:
            await h._get_username()
        except BaseClientError:
            resp = await h.qc.pathfinder_query({}, Operations.PROFILE_ATTRIBUTES)
            me = resp.get("me") or {}
            prof = me.get("profile") if hasattr(me, "get") else None
            user = prof.get("username") if hasattr(prof, "get") else None
            if not isinstance(user, str) or not user:
                raise
            h._username = user
        self._ph = h
        return h

    async def _playlist(self, uri: str) -> Any:
        from spotapi import Playlist

        return Playlist(self.client, uri, handler=await self._handler())

    async def create_playlist(self, name: str) -> str:
        self.log.append("create_playlist")
        h = await self._handler()
        pl = await h.create_playlist(name)
        return pl.playlist_id

    async def set_private(self, uri: str) -> None:
        self.log.append("set_private")
        await (await self._playlist(uri)).set_published(False)

    async def add_tracks(self, uri: str, uris: Sequence[str]) -> None:
        self.log.append("add_tracks")
        pl = await self._playlist(uri)
        for i in range(0, len(uris), 100):
            await pl.add_tracks(*uris[i : i + 100])

    async def playlist_uris(self, uri: str) -> list[str]:
        self.log.append("playlist_uris")
        out: list[str] = []
        async for entry in (await self._playlist(uri)).items():
            out.append(entry.uri or "")
        return out

    async def delete_playlist(self, uri: str) -> None:
        self.log.append("delete_playlist")
        await (await self._playlist(uri)).delete()


async def settle(seconds: float = 0.6) -> None:
    await asyncio.sleep(seconds)
