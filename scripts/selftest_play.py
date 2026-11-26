#!/usr/bin/env python3
"""Live self-test helper: start or pause playback on a fleet-controlled test device, volume 0.

    uv run python scripts/selftest_play.py play "Mac Studio M3U" spotify:playlist:<id>
    uv run python scripts/selftest_play.py pause "Mac Studio M3U"

Refuses (exit 9) when any OTHER device is active and playing: never transfer away from it.
Refuses names of house speakers/TVs. The cookie comes from the repo's own 1Password reader.
"""
import asyncio
import sys

from queue_saver.session import cookie_header, read_sp_dc
from queue_saver.spotify import SpotifyBackend

NEVER = ("JBL", "Theater", "FireTV", "TV", "dell-rugged", "iPhone")


async def main(verb: str, name: str, context: str | None) -> int:
    if any(w.lower() in name.lower() for w in NEVER):
        print(f"refusing: {name!r} is not a test device")
        return 9
    async with SpotifyBackend(cookie_header(read_sp_dc())) as sp:
        await sp.refresh()
        st = sp.state
        active = next((d for d in sp.devices if d.id == st.active_device_id), None)
        if active and active.name != name and st.is_playing:
            print(f"refusing: {active.name} is playing; not touching it")
            return 9
        match = [d for d in sp.devices if d.name == name]
        if len(match) != 1:
            print(f"{len(match)} devices named {name!r}: " + ", ".join(d.name for d in sp.devices))
            return 3
        sp.select(match[0].id)
        if verb == "pause":
            await sp.pause()
        else:
            await sp.player.volume(0)
            await sp.play(context, track=None, position_ms=0)
            await asyncio.sleep(2)
            await sp.player.volume(0)
        await sp.refresh()
        print(f"{verb}: {name} playing={sp.state.is_playing} track={sp.state.track.uri if sp.state.track else '-'}")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) < 2 or a[0] not in ("play", "pause") or (a[0] == "play" and len(a) < 3):
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(a[0], a[1], a[2] if len(a) > 2 else None)))
