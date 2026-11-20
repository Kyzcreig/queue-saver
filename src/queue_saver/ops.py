"""probe, park, restore, list. Backend-agnostic: takes anything shaped like SpotifyBackend."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from spotapi.models.playback import PlaybackTrack, PlayerState

from .state import (
    Item,
    Snapshot,
    StateError,
    is_real_track,
    list_snapshots,
    load_snapshot,
    require_fields,
    snapshot_from_cluster,
    write_snapshot,
)

Out = Callable[[str], None]

# Base wait between playlist read-back attempts (grows linearly). Tests set it to 0.
READBACK_BASE_S = 1.0


class StageError(RuntimeError):
    """A named stage failed. Exit code 2."""

    def __init__(self, stage: str, cause: BaseException) -> None:
        super().__init__(f"{stage} failed: {type(cause).__name__}: {cause}")
        self.stage = stage


class NoActiveDevice(RuntimeError):
    """Nothing is playing anywhere. Exit code 3."""


@contextmanager
def stage(name: str) -> Iterator[None]:
    try:
        yield
    except (StageError, NoActiveDevice):
        raise
    except Exception as e:  # SpotAPIError, StateError, TimeoutError, ...
        raise StageError(name, e) from e


def device_lines(sp: Any) -> list[str]:
    devs = list(sp.devices or ())
    if not devs:
        return ["  (no devices visible)"]
    return [f"  {d.name} [{d.type}]{' (active)' if d.is_active else ''}" for d in devs]


async def read_state(sp: Any, *, need_active: bool = True) -> tuple[dict, PlayerState]:
    with stage("read state"):
        raw = await sp.refresh()
        require_fields(raw)
        st = PlayerState.from_cluster(raw)
    if need_active and not st.active_device_id:
        raise NoActiveDevice("no active Spotify device")
    return raw, st


def queued(st: PlayerState) -> list[PlaybackTrack]:
    return [t for t in st.next_tracks if t.provider == "queue"]


def describe(st: PlayerState, raw: dict, out: Out) -> None:
    from .state import device_name

    counts = Counter(t.provider or "?" for t in st.next_tracks)
    out(f"device:  {device_name(raw, st.active_device_id) or '-'}")
    out(f"context: {st.context_uri or '-'}")
    out(f"track:   {st.track.uri if st.track else '-'}  playing={st.is_playing}  pos={st.position_ms}ms")
    out("upcoming: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"))


# ---------------------------------------------------------------------------- probe
async def probe(sp: Any, out: Out, *, seed: int = 0) -> int:
    try:
        raw, st = await read_state(sp)
    except NoActiveDevice:
        out("no active device; nothing changed. devices:")
        for line in device_lines(sp):
            out(line)
        return 3
    describe(st, raw, out)
    if seed <= 0:
        return 0

    start = [(t.uri, t.uid) for t in queued(st)]
    pool = [t.uri for t in (*st.next_tracks, *st.prev_tracks) if t.uri.startswith("spotify:track:")]
    if st.track and st.track.uri.startswith("spotify:track:"):
        pool.append(st.track.uri)
    if not pool:
        raise StageError("seed", StateError("no track URIs available to seed with"))
    need = max(0, seed - len(start))
    added = 0
    with stage("seed: add_to_queue"):
        for i in range(need):
            await sp.add_to_queue(pool[i % len(pool)])
            added += 1
    raw, st = await read_state(sp)
    visible = len(queued(st))
    start_uids = {uid for _, uid in start}
    mine = [t for t in queued(st) if t.uid not in start_uids]
    out(f"queued visible after seeding: {visible} (added {added}, identified {len(mine)} by uid)")

    with stage("seed: remove added"):
        if mine:
            mine_uids = {t.uid for t in mine}
            await sp.set_queue([t for t in st.next_tracks if t.uid not in mine_uids])
    restored = False
    for _ in range(5):
        raw, st = await read_state(sp)
        if [(t.uri, t.uid) for t in queued(st)] == start:
            restored = True
            break
        await asyncio.sleep(1)
    ok = visible >= seed
    out(f"seeded {len(start) + added}, visible {'>=' if ok else '<'}{seed} ({visible}), "
        f"restored: {'yes' if restored else 'no'}")
    return 0 if (ok and restored) else 1


# ---------------------------------------------------------------------------- park
async def park(sp: Any, out: Out, *, version: str, queued_only: bool = False,
               name_prefix: str = "Queue Saver", readback_tries: int = 6) -> int:
    try:
        raw, _st = await read_state(sp)
    except NoActiveDevice:
        out("no active device; nothing parked. devices:")
        for line in device_lines(sp):
            out(line)
        return 3
    with stage("snapshot"):
        snap = snapshot_from_cluster(raw, version)
        path = write_snapshot(snap)
    out(f"snapshot: {path}")

    items = snap.queued if queued_only else [snap.track, *snap.upcoming]
    uris = [i.uri for i in items if i.uri.startswith("spotify:track:")]
    skipped = len(items) - len(uris)
    if skipped:
        out(f"note: {skipped} non-track items (episodes, markers) are in the snapshot only")

    with stage("create playlist"):
        name = f"{name_prefix} {time.strftime('%Y-%m-%d %H:%M')}"
        pl = await sp.create_playlist(name)
        await sp.set_private(pl)
        if uris:
            await sp.add_tracks(pl, uris)
    pl_url = "https://open.spotify.com/playlist/" + pl.rsplit(":", 1)[-1]
    snap.playlist_url = pl_url
    with stage("snapshot"):
        write_snapshot(snap, path)

    with stage("playlist read-back"):
        got: list[str] = []
        for i in range(readback_tries):
            got = await sp.playlist_uris(pl)
            if len(got) >= len(uris):
                break
            await asyncio.sleep(READBACK_BASE_S * (1 + i))
        if got != uris:
            raise StateError(f"playlist holds {len(got)} items, expected {len(uris)}; queue left as is")
    out(f"playlist: {pl_url} ({len(got)} tracks)")

    # Only now remove queued songs, and only the occurrences that are in the snapshot.
    parked_uids = {i.uid for i in snap.queued if i.uid}
    raw, st = await read_state(sp)
    keep = [t for t in st.next_tracks if not (t.provider == "queue" and t.uid in parked_uids)]
    if len(keep) != len(st.next_tracks):
        with stage("clear queued"):
            await sp.set_queue(keep)
    raw, st = await read_state(sp)
    left = [t for t in queued(st) if t.uid in parked_uids]
    out(f"parked {len(snap.queued)} queued + {len(snap.upcoming) - len(snap.queued)} context; "
        f"queued visible now: {len(queued(st))} (parked ones left: {len(left)})")
    out(f"current: {st.track.uri if st.track else '-'} (was {snap.track.uri})")
    return 0 if not left else 1


# ---------------------------------------------------------------------------- restore
async def restore(sp: Any, out: Out, *, name: str | None = None, device: str | None = None,
                  position_tolerance_ms: int = 5000) -> int:
    with stage("load snapshot"):
        path, snap = load_snapshot(name)
    out(f"restoring {path.name}: {snap.track.uri} @ {snap.position_ms}ms, {len(snap.queued)} queued")

    try:
        raw, st = await read_state(sp, need_active=device is None)
    except NoActiveDevice:
        out("no active device; pass --device NAME. devices:")
        for line in device_lines(sp):
            out(line)
        return 3
    if device is not None:
        match = [d for d in (sp.devices or ()) if d.name == device]
        if not match:
            out(f"no device named {device!r}. devices:")
            for line in device_lines(sp):
                out(line)
            return 3
        sp.select(match[0].id)

    context_ok = True
    with stage("play saved song"):
        try:
            if snap.context_uri and not snap.context_uri.startswith("spotify:track:"):
                await sp.play(snap.context_uri, track=snap.track.uri, position_ms=snap.position_ms)
            else:
                await sp.play(snap.track.uri, track=None, position_ms=snap.position_ms)
        except Exception as e:  # context deleted or unplayable: fall back to the song itself
            context_ok = False
            out(f"context {snap.context_uri} could not be played ({type(e).__name__}); "
                "restoring the song and the queued songs only")
            await sp.play(snap.track.uri, track=None, position_ms=snap.position_ms)

    st = None
    for _ in range(10):
        raw, st = await read_state(sp, need_active=False)
        if st.track and st.track.uri == snap.track.uri:
            break
        await asyncio.sleep(1)
    if not st or not st.track or st.track.uri != snap.track.uri:
        raise StageError("play saved song", StateError("player did not load the saved song"))
    if abs(st.position_ms - snap.position_ms) > position_tolerance_ms:
        with stage("seek"):
            await sp.seek(snap.position_ms)

    with stage("options"):
        if st.shuffle != snap.shuffle:
            await sp.shuffle(snap.shuffle)
        if st.repeat_mode != snap.repeat:
            await sp.repeat(snap.repeat)

    want = [i.uri for i in snap.queued if is_real_track(i.uri)]
    with stage("set queue"):
        raw, st = await read_state(sp, need_active=False)
        context_rest = [t for t in st.next_tracks if t.provider != "queue"]
        if want:
            await sp.set_queue([*want, *context_rest])

    got: list[str] = []
    for _ in range(5):
        raw, st = await read_state(sp, need_active=False)
        got = [t.uri for t in queued(st)]
        if got == want:
            break
        await asyncio.sleep(1)
    pos = st.position_ms if st else -1
    out(f"track: {st.track.uri if st and st.track else '-'} (saved {snap.track.uri}); "
        f"position {pos}ms (saved {snap.position_ms}ms); queued {len(got)}/{len(want)} in order: "
        f"{'yes' if got == want else 'no'}"
        + ("" if context_ok else "; context NOT restored"))
    return 0 if got == want else 1


# ---------------------------------------------------------------------------- list
def list_parked(out: Out) -> int:
    files = list_snapshots()
    if not files:
        out("no parked states")
        return 0
    for p in files:
        try:
            s = Snapshot.from_json(p.read_text())
        except Exception as e:
            out(f"{p.name}: unreadable ({type(e).__name__})")
            continue
        out(f"{p.stem}  {s.track.name or s.track.uri} - {s.track.artist}  "
            f"queued={len(s.queued)} context={len(s.upcoming) - len(s.queued)}  "
            f"{s.playlist_url or '(no playlist)'}")
    return 0
