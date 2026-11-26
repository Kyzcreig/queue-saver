"""Queue writes on SpotifyBackend: sent without spotapi's ACK wait, confirmed by reading state back.

Live 2026-11-25 (Mac desktop app and the owner's iPhone): add_to_queue got HTTP 200 + an ack_id,
then no ACK and no queue change reached this connection; spotapi's wait=True raised a bare
TimeoutError. These tests drive SpotifyBackend against a fake spotapi Player.
"""

import asyncio
import copy
import inspect
import itertools

import pytest
from spotapi.models.playback import Device, PlayerState

from fakes import FakeBackend, load_cluster
from queue_saver import cli
from queue_saver.state import parse_state
from queue_saver.spotify import QueueWriteError, SpotifyBackend, route_from_controller


def T(i):
    return "spotify:track:" + "T%021d" % i


class FakePlayer:
    """The slice of spotapi's Player that SpotifyBackend's queue writes use.

    mode: "apply" (lands at once), "lag" (lands after ``lag`` refreshes), "drop" (never lands),
    "stale" (the first write is dropped, as a stale queue_revision would be; later ones land).
    """

    def __init__(self, cluster, *, mode="apply", lag=0):
        self.cluster = copy.deepcopy(cluster)
        self.mode, self.lag = mode, lag
        self.controller_id = "c" * 40
        self.source_device_id = None
        self.target_device_id = None
        self.sent: list[tuple[str, dict]] = []
        self.waits: list[bool] = []
        self.refreshes = 0
        self._pending = None
        self._uid = itertools.count(5000)
        self.state = parse_state(self.cluster)
        active = self.cluster["active_device_id"]
        self.devices = tuple(Device(i, d.get("name", ""), d.get("device_type", "SMARTPHONE"), None, True,
                                    i == active) for i, d in self.cluster["devices"].items())

    async def command(self, endpoint, *, wait=True, **args):
        self.waits.append(wait)
        self.sent.append((endpoint, args))
        if self.mode == "drop" or (self.mode == "stale" and len(self.sent) == 1):
            return
        self._pending = (endpoint, args, self.lag)
        if self.lag == 0:
            self._apply()

    def _apply(self):
        endpoint, args, _ = self._pending
        self._pending = None
        ps = self.cluster["player_state"]
        if endpoint == "add_to_queue":
            nt = ps["next_tracks"]
            n = sum(1 for t in nt if t.get("provider") == "queue")
            nt.insert(n, {**args["track"], "uid": f"u{next(self._uid)}"})
        elif endpoint == "set_queue":
            ps["next_tracks"] = [{**e, "uid": e.get("uid") or f"u{next(self._uid)}"}
                                 for e in args["next_tracks"]]
            ps["queue_revision"] = f"rev{next(self._uid)}"
        self.cluster["timestamp"] += 1
        self.state = parse_state(self.cluster)

    async def refresh(self):
        self.refreshes += 1
        if self._pending is not None:
            endpoint, args, left = self._pending
            if left <= 1:
                self._apply()
            else:
                self._pending = (endpoint, args, left - 1)
        return self.state

    async def wait_for(self, event, *, timeout=None):
        await asyncio.sleep(0)
        raise TimeoutError

    @property
    def raw_cluster(self):  # what _RawPlayer keeps: the cluster exactly as the device sent it
        return self.cluster


def backend(player, timeout=0.5):
    sp = SpotifyBackend("sp_dc=unused")
    sp.player = player
    sp.confirm_timeout = timeout
    return sp


def queued_uris(player):
    return [t.uri for t in player.state.next_tracks if t.provider == "queue"]


def test_add_to_queue_sends_without_ack_wait_and_confirms_by_state():
    p = FakePlayer(load_cluster("playing.json"))
    asyncio.run(backend(p).add_to_queue(T(7)))
    assert p.waits == [False]
    assert p.sent[0][0] == "add_to_queue" and p.sent[0][1]["track"]["uri"] == T(7)
    assert queued_uris(p)[-1] == T(7)


def test_add_to_queue_waits_for_a_late_state():
    p = FakePlayer(load_cluster("playing.json"), lag=3)
    asyncio.run(backend(p, timeout=5).add_to_queue(T(7)))
    assert p.refreshes >= 3 and queued_uris(p)[-1] == T(7)


def test_add_to_queue_that_never_lands_names_the_device():
    p = FakePlayer(load_cluster("playing.json"), mode="drop")
    with pytest.raises(QueueWriteError) as e:
        asyncio.run(backend(p, timeout=0.3).add_to_queue(T(7)))
    msg = str(e.value)
    assert "did not land" in msg and "Living Room" in msg and "id=dev-livi" in msg
    assert len(p.sent) == 1  # never resent: a second add would queue the song twice


def test_add_to_queue_is_not_fooled_by_an_unrelated_state_update():
    p = FakePlayer(load_cluster("playing.json"), mode="drop")
    sp = backend(p, timeout=0.3)

    async def go():
        async def bump():  # position moves, queue does not
            await asyncio.sleep(0.05)
            p.cluster["timestamp"] += 1
            p.cluster["player_state"]["position_as_of_timestamp"] += 1000
            p.state = PlayerState.from_cluster(p.cluster)
        await asyncio.gather(sp.add_to_queue(T(7)), bump())

    with pytest.raises(QueueWriteError):
        asyncio.run(go())


def test_set_queue_sends_without_ack_wait_and_confirms():
    p = FakePlayer(load_cluster("playing.json"))
    keep = [t for t in p.state.next_tracks if t.provider != "queue"]
    asyncio.run(backend(p).set_queue(keep))
    assert p.waits == [False]
    args = p.sent[0][1]
    assert args["queue_revision"] == "rev1" and "prev_tracks" in args
    assert queued_uris(p) == []


def test_set_queue_retries_once_with_a_fresh_revision():
    p = FakePlayer(load_cluster("playing.json"), mode="stale")
    keep = [t for t in p.state.next_tracks if t.provider != "queue"]
    asyncio.run(backend(p, timeout=0.3).set_queue(keep))
    assert len(p.sent) == 2 and queued_uris(p) == []


def test_set_queue_that_never_lands_fails_loud_after_one_retry():
    p = FakePlayer(load_cluster("playing.json"), mode="drop")
    keep = [t for t in p.state.next_tracks if t.provider != "queue"]
    with pytest.raises(QueueWriteError) as e:
        asyncio.run(backend(p, timeout=0.2).set_queue(keep))
    assert len(p.sent) == 2 and "set_queue (0 queued)" in str(e.value) and "Living Room" in str(e.value)


def test_set_queue_equal_to_the_current_queue_sends_nothing():
    p = FakePlayer(load_cluster("playing.json"))
    asyncio.run(backend(p).set_queue(list(p.state.next_tracks)))
    assert p.sent == []


def test_set_queue_accepts_a_song_change_that_consumed_the_first_queued():
    p = FakePlayer(load_cluster("playing.json"), mode="drop")
    sp = backend(p, timeout=1)
    want = [T(201), T(202)]

    async def go():
        async def lands_then_advances():
            await asyncio.sleep(0.05)
            ps = p.cluster["player_state"]
            ps["track"] = {"uri": T(201), "uid": "uX", "provider": "queue"}
            ps["next_tracks"] = [{"uri": T(202), "uid": "uY", "provider": "queue"}]
            p.cluster["timestamp"] += 1
            p.state = PlayerState.from_cluster(p.cluster)
        await asyncio.gather(sp.set_queue(want), lands_then_advances())

    asyncio.run(go())
    assert len(p.sent) == 1


# ---------------------------------------------------------------- routing and probe on top of it
def test_commands_are_routed_from_our_own_controller():
    p = FakePlayer(load_cluster("playing.json"))
    route_from_controller(p)
    assert p.source_device_id == p.controller_id
    src = inspect.getsource(SpotifyBackend.__aenter__)
    assert src.index("route_from_controller(self.player)") < src.index("self.player.connect()")


def test_live_read_cap_is_the_measured_80():
    assert SpotifyBackend.queue_cap == 80


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("QS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "read_sp_dc", lambda fresh=False: "fake")


def run(argv, fb):
    lines = []
    rc = cli.main(argv, factory=lambda _c: fb, out=lines.append)
    return rc, "\n".join(lines)


def test_probe_seed_stops_at_the_read_cap_and_restores(cli_env):
    fb = FakeBackend(load_cluster("playing.json"), queue_cap=10)
    start = [(t.uri, t.uid) for t in fb.state.next_tracks]
    rc, out = run(["probe", "--seed", "25"], fb)
    assert rc == 1
    assert "seeding stops at the read cap (10 queued visible), not 25" in out
    assert fb.log.count("add_to_queue") == 7  # 3 already queued
    assert out.splitlines()[-1] == "seeded 10, visible <25 (10), restored: yes"
    assert [(t.uri, t.uid) for t in fb.state.next_tracks] == start


class DropsOneAdd(FakeBackend):
    """The 3rd add is accepted but never lands, like the one dropped live at seed 41 of 120."""

    async def add_to_queue(self, uri):
        if self.log.count("add_to_queue") == 2:
            self.log.append("add_to_queue")
            raise QueueWriteError("add_to_queue x: ... did not land on Living Room")
        await super().add_to_queue(uri)


def test_probe_removes_the_songs_it_added_when_an_add_fails(cli_env):
    fb = DropsOneAdd(load_cluster("playing.json"))
    start = [(t.uri, t.uid) for t in fb.state.next_tracks]
    rc, out = run(["probe", "--seed", "10"], fb)
    assert rc == 2
    assert "restored: yes" in out and "did not land on Living Room" in out.splitlines()[-1]
    assert [(t.uri, t.uid) for t in fb.state.next_tracks] == start


def test_probe_seed_keep_leaves_the_seeded_songs(cli_env):
    fb = FakeBackend(load_cluster("playing.json"))
    rc, out = run(["probe", "--seed", "5", "--keep"], fb)
    assert rc == 0 and "kept in the queue (--keep)" in out
    assert sum(t.provider == "queue" for t in fb.state.next_tracks) == 5
    assert "set_queue" not in fb.log
