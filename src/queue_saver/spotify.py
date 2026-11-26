"""The only module that talks to spotapi. Every call is recorded in ``log`` by name.

A test double (tests/fakes.py) implements the same surface, so park/restore/probe
logic is tested without the network.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
from contextlib import AsyncExitStack
from typing import Any, Callable, Mapping, Sequence

PINNED_SPOTAPI = "2.1.0"


class QueueWriteError(RuntimeError):
    """A queue write was accepted over HTTP but never showed up in the player state."""


def _queued(st: Any) -> list[Any]:
    return [t for t in st.next_tracks if t.provider == "queue"]


def queued_count(st: Any, uri: str) -> int:
    return sum(1 for t in _queued(st) if t.uri == uri)


def _advanced(before: Any, st: Any) -> bool:
    a, b = before.track, st.track
    return bool(a and b and (a.uri, a.uid) != (b.uri, b.uid))


def add_landed(before: Any, count_before: int, uri: str, st: Any) -> bool:
    """One more queued occurrence of ``uri`` than before (or it became the current song)."""
    if st is before:
        return False
    now = queued_count(st, uri)
    if _advanced(before, st) and st.track.uri == uri and st.track.provider == "queue":
        now += 1
    return now > count_before


def set_queue_landed(before: Any, want: Sequence[str], st: Any) -> bool:
    """The queued part of the state equals ``want`` in order (the context part may be read capped).

    If playback moved on to the first wanted song meanwhile, the rest of ``want`` is enough.
    """
    if st is before:
        return False
    got = [t.uri for t in _queued(st)]
    if got == list(want):
        return True
    return bool(want) and _advanced(before, st) and st.track.uri == want[0] and got == list(want[1:])


def route_from_controller(player: Any) -> None:
    """Send every Connect command FROM this tool's own registered controller.

    spotapi 2.1.0 builds /player/command/from/<source>/to/<target> with
    ``source = source_device_id or <active device> or controller_id``, so whenever a device is
    active the command claims to come from the target device itself. Measured live 2026-11-25 on
    the Mac desktop app (and the same failure on the owner's iPhone): from=<active> got HTTP 200
    and an ack_id, then the command was never applied and no ACK reached this connection;
    from=<controller> landed in ~0.3 s with the ACK in the next cluster push.
    """
    player.source_device_id = player.controller_id


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

    # Most queued entries a state read is known to show in full. Measured live 2026-11-25 on the Mac
    # desktop app (card t_96945aa0): a state read's next_tracks holds at most 80 entries, queued
    # songs first, then the context. `probe --seed 30` and `--seed 60` showed 30 and 60 queued; at
    # `--seed 120` the read showed `queue=80` and no context, and the next add landed unseen. A
    # set_queue drops the unseen ones too, so park clears only while fewer than 80 are queued.
    queue_cap: int | None = 80

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
        route_from_controller(self.player)
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
    # Queue writes are sent with wait=False and confirmed by reading state back, not by spotapi's
    # ACK wait, so a write that does not land fails with a message naming the device instead of a
    # bare TimeoutError (what wait=True raised on 2026-11-25; root cause: route_from_controller).
    confirm_timeout: float = 8.0

    async def add_to_queue(self, uri: str) -> None:
        from spotapi.player import queue_entry

        self.log.append("add_to_queue")
        before = self._fresh_state()
        n = queued_count(before, uri)
        await self._write_queue(
            "add_to_queue", {"track": queue_entry(uri)},
            lambda st: add_landed(before, n, uri, st), what=f"add_to_queue {uri}")

    async def set_queue(self, tracks: Sequence[Any]) -> None:
        """Replace the upcoming list; one retry with a fresh queue_revision (the write is idempotent)."""
        from spotapi.player import queue_entry

        self.log.append("set_queue")
        entries = [queue_entry(t) for t in tracks]
        want = [e["uri"] for e in entries if e.get("provider") == "queue"]
        cur = self._fresh_state()
        if [t.uri for t in cur.next_tracks] == [e["uri"] for e in entries] and \
                [t.uri for t in _queued(cur)] == want:
            return  # already so: sending it would change nothing a read could confirm
        last: QueueWriteError | None = None
        for _attempt in range(2):
            before = self._fresh_state()
            args: dict[str, Any] = {"next_tracks": entries,
                                    "prev_tracks": [queue_entry(t) for t in before.prev_tracks]}
            if before.queue_revision is not None:
                args["queue_revision"] = before.queue_revision
            try:
                await self._write_queue("set_queue", args,
                                        lambda st, b=before: set_queue_landed(b, want, st),
                                        what=f"set_queue ({len(want)} queued)")
                return
            except QueueWriteError as e:
                last = e
                await self.player.refresh()
        assert last is not None
        raise last

    def _fresh_state(self) -> Any:
        st = self.player.state
        if st is None:
            raise QueueWriteError("no player state read yet")
        return st

    def device_label(self, device_id: str | None = None) -> str:
        """'<name> [<type>] id=<first 8>' for the target (default: the active device)."""
        st = self.player.state
        dev_id = device_id or self.player.target_device_id or (st.active_device_id if st else None)
        for d in self.player.devices or ():
            if d.id == dev_id:
                return f"{d.name} [{d.type}] id={dev_id[:8]}"
        return f"device id={dev_id[:8] if dev_id else '-'}"

    async def _write_queue(self, endpoint: str, args: Mapping[str, Any],
                           landed: Callable[[Any], bool], *, what: str) -> None:
        """Send without waiting for spotapi's ACK, then poll state until ``landed`` or timeout."""
        await self.player.command(endpoint, wait=False, **args)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.confirm_timeout
        delay = 0.3
        while True:
            st = self.player.state
            if st is not None and landed(st):
                return
            left = deadline - loop.time()
            if left <= 0:
                break
            try:  # a dealer push usually arrives first; else read the state ourselves
                await self.player.wait_for("state_update", timeout=min(delay, left))
            except TimeoutError:
                await self.player.refresh()
            delay = min(delay * 2, 2.0)
        raise QueueWriteError(
            f"{what}: Spotify accepted the command (HTTP 200) but no state read within "
            f"{self.confirm_timeout:g} s showed it on {self.device_label()}; the write did not land")

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
