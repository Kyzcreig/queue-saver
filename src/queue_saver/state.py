"""Player state -> Snapshot (plain dataclass, JSON on disk)."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from spotapi.models.playback import PlaybackTrack, PlayerState

SNAPSHOT_VERSION = 1


class StateError(RuntimeError):
    """The private state lacks something the tool needs. Fails loud, never partial."""


@dataclass
class Item:
    uri: str
    uid: str | None = None
    provider: str | None = None
    name: str = ""
    artist: str = ""

    @classmethod
    def from_track(cls, t: PlaybackTrack) -> "Item":
        return cls(uri=t.uri, uid=t.uid, provider=t.provider, name=t.name, artist=t.artist)


@dataclass
class Snapshot:
    taken_at: str
    device_name: str | None
    context_uri: str | None
    track: Item
    position_ms: int
    shuffle: bool
    repeat: str
    upcoming: list[Item] = field(default_factory=list)
    playlist_url: str | None = None
    spotapi_version: str = ""
    is_playing: bool = False

    @property
    def queued(self) -> list[Item]:
        return [i for i in self.upcoming if i.provider == "queue"]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Snapshot":
        d = json.loads(text)
        d["track"] = Item(**d["track"])
        d["upcoming"] = [Item(**u) for u in d.get("upcoming", [])]
        return cls(**d)


def require_fields(cluster: Mapping[str, Any]) -> None:
    """Fail loud when Spotify's private cluster no longer carries what we read.

    spotapi's parser silently defaults a missing ``next_tracks`` to an empty tuple, which would
    read as "nothing queued". Check the raw cluster instead.
    """
    ps = cluster.get("player_state")
    if not isinstance(ps, Mapping):
        raise StateError("state has no player_state")
    for key in ("next_tracks", "track", "options"):
        if key not in ps:
            raise StateError(f"state lacks player_state.{key}")
    if not isinstance(ps["next_tracks"], list):
        raise StateError("player_state.next_tracks is not a list")


def device_name(cluster: Mapping[str, Any], device_id: str | None) -> str | None:
    if not device_id:
        return None
    dev = (cluster.get("devices") or {}).get(device_id) or {}
    return dev.get("name") or None


def snapshot_from_cluster(cluster: Mapping[str, Any], spotapi_version: str) -> Snapshot:
    require_fields(cluster)
    st = PlayerState.from_cluster(cluster)
    if st.track is None or not st.track.uri:
        raise StateError("nothing is loaded on the player")
    return Snapshot(
        taken_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        device_name=device_name(cluster, st.active_device_id),
        context_uri=st.context_uri,
        track=Item.from_track(st.track),
        position_ms=st.position_ms,
        shuffle=st.shuffle,
        repeat=st.repeat_mode,
        upcoming=[Item.from_track(t) for t in st.next_tracks],
        spotapi_version=spotapi_version,
        is_playing=st.is_playing,
    )


def is_real_track(uri: str) -> bool:
    """Context lists end in 'spotify:delimiter' and similar markers; keep only playable items."""
    return uri.startswith("spotify:track:") or uri.startswith("spotify:episode:")


def snapshot_dir() -> Path:
    base = os.environ.get("QS_STATE_DIR") or os.path.expanduser("~/.hermes/var/queue-saver")
    return Path(base) / "snapshots"


def write_snapshot(snap: Snapshot, path: Path | None = None) -> Path:
    d = snapshot_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fd, tmp = tempfile.mkstemp(dir=d, prefix=(path.stem if path else stamp) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(snap.to_json())
        if path is not None:
            os.replace(tmp, path)
            return path
        n = 0
        while True:  # link fails if the name exists, so only a complete file ever gets a free name
            path = d / (f"{stamp}.json" if n == 0 else f"{stamp}-{n}.json")
            try:
                os.link(tmp, path)
                return path
            except FileExistsError:
                n += 1
    finally:
        Path(tmp).unlink(missing_ok=True)


_NAME = re.compile(r"^(\d{8}-\d{6})(?:-(\d+))?$")


def _order(p: Path) -> tuple[str, int]:
    """Oldest first: by timestamp, then by the same-second suffix (none, -1, -2, ..., -10)."""
    m = _NAME.match(p.stem)
    return (m.group(1), int(m.group(2) or 0)) if m else (p.stem, -1)


def list_snapshots() -> list[Path]:
    d = snapshot_dir()
    if not d.exists():
        return []
    return sorted(d.glob("*.json"), key=_order)


def load_snapshot(name: str | None) -> tuple[Path, Snapshot]:
    files = list_snapshots()
    if name:
        p = Path(name)
        if not p.exists():
            p = snapshot_dir() / (name if name.endswith(".json") else name + ".json")
    else:
        if not files:
            raise StateError("no parked snapshots")
        p = files[-1]
    if not p.exists():
        raise StateError(f"snapshot not found: {name}")
    return p, Snapshot.from_json(p.read_text())
