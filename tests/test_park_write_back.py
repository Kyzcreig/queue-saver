"""park's set_queue writes back exactly what the device sent, minus the parked entries.

Live 2026-11-26 04:32 (owner's iPhone, 7 queued songs with duplicates): park paused playback.
That set_queue re-labelled phone-queued entries (provider "context" + is_queued) as "queue" in
next_tracks AND prev_tracks, and rebuilt every context entry through spotapi's queue_entry,
dropping the metadata the phone had. Whatever the device does with that, park must not change
anything in the command except removing the parked entries, and must say so loudly (without
resuming anything itself) when playback was playing before the clear and is paused after it.
"""

import asyncio
import copy

import pytest

from queue_saver import ops
from queue_saver.spotify import PARK_ALLOWED, SpotifyBackend
from queue_saver.state import mark_queued, parse_state
from fakes import FakeBackend, load_cluster
from test_queue_writes import FakePlayer

CTX = "spotify:playlist:37i9dQZF1F5p3rmiWPIYgZ"


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("QS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(ops, "READBACK_BASE_S", 0.0)
    return tmp_path / "state"


def T(c):
    return "spotify:track:" + c * 22


def ctx_md(i):  # the kind of metadata a phone sends on a context entry
    return {"context_uri": CTX, "entity_uri": CTX, "iteration": "0", "decision_id": f"d{i}",
            "interaction_id": f"i{i}", "page_instance_id": f"p{i}", "requested_uri": CTX}


QMD = {"is_queued": "true", "queued_by": "kyzcreig"}


def phone_cluster():
    """Recorded shape (iPhone, 2026-11-26): 1st queued entry provider "queue", the rest
    "context" + metadata.is_queued; duplicates among the queued and of the context songs."""
    q = [("a", "queue", "q139a1cd6d32f3ade"), ("b", "context", "q150c624742e453fb"),
         ("c", "context", "q6185d3b7d1cba9c9"), ("b", "context", "q3e1a29f0aa837289"),
         ("a", "context", "q36f9f705e27a9631")]
    nt = [{"uri": T(u), "uid": uid, "provider": prov, "metadata": dict(QMD)} for u, prov, uid in q]
    nt += [{"uri": T(u), "uid": f"ctx{i:017d}", "provider": "context", "metadata": ctx_md(i)}
           for i, u in enumerate("abdefg")]
    prev = [{"uri": T("h"), "uid": "qaaaaaaaaaaaaaaaa", "provider": "context",
             "metadata": {**QMD, "page_instance_id": "x"}},  # played earlier; queued from the phone
            {"uri": T("i"), "uid": "ctxprev", "provider": "context", "metadata": ctx_md(99)}]
    return {
        "timestamp": 1795700000000,
        "active_device_id": "dev-phone",
        "devices": {"dev-phone": {"name": "iPhone", "device_type": "SMARTPHONE", "can_play": True}},
        "player_state": {
            "timestamp": 1795700000000, "context_uri": CTX, "is_playing": True, "is_paused": False,
            "position_as_of_timestamp": 243266, "duration": 267520, "playback_speed": 1.0,
            "track": {"uri": T("z"), "uid": "ctxcur", "provider": "context", "metadata": ctx_md(50)},
            "options": {"shuffling_context": True, "repeating_context": False,
                        "repeating_track": False},
            "queue_revision": "rev-phone",
            "prev_tracks": prev,
            "next_tracks": nt,
        },
    }


class LiveShapedBackend(SpotifyBackend):
    """The real SpotifyBackend queue path over a fake spotapi Player; playlists in memory."""

    def __init__(self, player):
        super().__init__("sp_dc=unused")
        self.player = player
        self.confirm_timeout = 0.5
        self.pl: dict[str, list[str]] = {}

    async def create_playlist(self, name):
        self.log.append("create_playlist")
        self.pl["spotify:playlist:p1"] = []
        return "spotify:playlist:p1"

    async def set_private(self, uri):
        self.log.append("set_private")

    async def add_tracks(self, uri, uris):
        self.log.append("add_tracks")
        self.pl[uri].extend(uris)

    async def playlist_uris(self, uri):
        self.log.append("playlist_uris")
        return list(self.pl[uri])


def park(sp):
    lines = []
    rc = asyncio.run(ops.park(sp, lines.append, version="2.1.0"))
    return rc, "\n".join(lines)


def test_mark_queued_classifies_a_copy_and_never_changes_the_cluster():
    c = phone_cluster()
    before = copy.deepcopy(c)
    marked = mark_queued(c)
    assert c == before  # the raw cluster keeps the phone's providers
    assert [e["provider"] for e in marked["player_state"]["next_tracks"][:5]] == ["queue"] * 5
    assert [t.provider for t in parse_state(c).next_tracks[:5]] == ["queue"] * 5


def test_park_set_queue_sends_the_raw_entries_minus_the_parked_ones(state_dir):
    p = FakePlayer(phone_cluster())
    raw = copy.deepcopy(p.cluster["player_state"])
    sp = LiveShapedBackend(p)
    rc, out = park(sp)
    assert rc == 0, out
    assert "queued visible now: 0" in out and "parked 5 queued + 6 context" in out
    sets = [a for e, a in p.sent if e == "set_queue"]
    assert len(sets) == 1
    args = sets[0]
    # next_tracks: the 6 context entries exactly as read (provider, uid, metadata), in order
    assert args["next_tracks"] == raw["next_tracks"][5:]
    # prev_tracks: unchanged from the raw cluster (no "queue" re-label, metadata kept)
    assert args["prev_tracks"] == raw["prev_tracks"]
    assert args["prev_tracks"][0]["provider"] == "context"
    assert args["queue_revision"] == "rev-phone"
    assert set(args) == {"next_tracks", "prev_tracks", "queue_revision"}
    assert set(sp.log) <= PARK_ALLOWED


def test_park_keeps_an_unparked_queued_entry_as_the_phone_sent_it(state_dir):
    c = phone_cluster()
    nt = c["player_state"]["next_tracks"]
    nt.insert(2, {"uri": "spotify:local:a:b:c:1", "uid": "qlocal0000000000", "provider": "context",
                  "metadata": dict(QMD)})  # a queued local file: restore cannot requeue it
    p = FakePlayer(c)
    raw_local = copy.deepcopy(nt[2])
    rc, out = park(LiveShapedBackend(p))
    assert rc == 1 and "left 1 queued item(s)" in out
    sent = [a for e, a in p.sent if e == "set_queue"][0]["next_tracks"]
    assert sent[0] == raw_local  # still provider "context" + is_queued, as read


def test_restore_puts_new_songs_first_and_keeps_context_entries_raw(state_dir):
    p = FakePlayer(phone_cluster())
    sp = LiveShapedBackend(p)
    want = [T("q"), T("r")]
    ctx = [t for t in parse_state(p.cluster).next_tracks if t.provider != "queue"]
    raw_ctx = copy.deepcopy(p.cluster["player_state"]["next_tracks"][5:])
    asyncio.run(sp.set_queue([*want, *ctx]))
    sent = p.sent[0][1]["next_tracks"]
    assert [e["uri"] for e in sent[:2]] == want and all(e["provider"] == "queue" for e in sent[:2])
    assert sent[2:] == raw_ctx


class PausesOnSetQueue(FakeBackend):
    """A device that stops playback when its queue is replaced (what the iPhone did at 04:32)."""

    async def set_queue(self, tracks):
        await super().set_queue(tracks)
        self.ps["is_playing"] = False
        self.ps["is_paused"] = True


def test_park_reports_a_pause_loudly_and_sends_no_play(state_dir):
    fb = PausesOnSetQueue(load_cluster("playing.json"))
    lines = []
    rc = asyncio.run(ops.park(fb, lines.append, version="2.1.0"))
    out = "\n".join(lines)
    assert rc == 1
    assert "!!! PLAYBACK STOPPED" in out and "is_paused=True" in out
    assert not {"play", "resume", "seek", "pause"} & set(fb.log)
    assert set(fb.log) <= PARK_ALLOWED


def test_park_that_keeps_playing_reports_no_pause(state_dir):
    fb = FakeBackend(load_cluster("playing.json"))
    lines = []
    rc = asyncio.run(ops.park(fb, lines.append, version="2.1.0"))
    assert rc == 0 and "PLAYBACK STOPPED" not in "\n".join(lines)


def test_park_of_a_paused_player_is_not_reported_as_stopped(state_dir):
    c = load_cluster("playing.json")
    c["player_state"]["is_playing"] = False
    c["player_state"]["is_paused"] = True
    fb = PausesOnSetQueue(c)
    lines = []
    rc = asyncio.run(ops.park(fb, lines.append, version="2.1.0"))
    assert rc == 0 and "PLAYBACK STOPPED" not in "\n".join(lines)
