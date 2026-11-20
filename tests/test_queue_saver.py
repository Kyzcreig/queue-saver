import asyncio
import os
import stat
import subprocess
from pathlib import Path

import pytest

from queue_saver import cli, ops, session
from queue_saver.spotify import PARK_ALLOWED, PINNED_SPOTAPI, installed_spotapi
from queue_saver.state import Item, Snapshot, snapshot_from_cluster
from fakes import FakeBackend, load_cluster

FAKE_COOKIE = "AQfakeSessionCookieValue_0123456789abcdefghijklmnopqrstuvwxyz_DO_NOT_LEAK"
CTX = "spotify:album:ALBUM0000000000000000001"


def T(i):
    return "spotify:track:" + "T%021d" % i


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("QS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "read_sp_dc", lambda fresh=False: FAKE_COOKIE)
    monkeypatch.setattr(ops, "READBACK_BASE_S", 0.0)
    return tmp_path / "state"


def run(argv, backend):
    lines = []
    seen = {}

    def factory(cookie_header):
        seen["cookie"] = cookie_header
        return backend

    rc = cli.main(argv, factory=factory, out=lines.append)
    return rc, "\n".join(lines), seen


def snapshots(state_dir):
    return sorted((state_dir / "snapshots").glob("*.json"))


# ---------------------------------------------------------------- Phase 2 unit checks
def test_snapshot_round_trips_through_json():
    snap = snapshot_from_cluster(load_cluster("playing.json"), "2.1.0")
    again = Snapshot.from_json(snap.to_json())
    assert again == snap
    assert again.track.uri == T(1)
    assert [i.provider for i in again.upcoming][:4] == ["queue", "queue", "queue", "context"]
    assert isinstance(again.upcoming[0], Item)


def test_playlist_is_current_song_then_upcoming_in_order(state_dir):
    fb = FakeBackend(load_cluster("playing.json"))
    rc, out, _ = run(["park"], fb)
    assert rc == 0, out
    (pl,) = fb.playlists.values()
    assert pl == [T(1), T(101), T(102), T(103), T(2), T(3), T(4)]  # delimiter dropped
    assert fb.private == set(fb.playlists)
    assert "https://open.spotify.com/playlist/fake0" in out


def test_queued_only_keeps_only_queue_provider():
    fb = FakeBackend(load_cluster("playing.json"))
    rc, out, _ = run(["park", "--queued-only"], fb)
    assert rc == 0, out
    (pl,) = fb.playlists.values()
    assert pl == [T(101), T(102), T(103)]


# ---------------------------------------------------------------- section 3 invariants
def test_never_skip_park_only_reads_playlist_writes_and_one_set_queue():
    fb = FakeBackend(load_cluster("playing.json"))
    before = fb.state
    rc, out, _ = run(["park"], fb)
    assert rc == 0, out
    assert set(fb.log) <= PARK_ALLOWED, fb.log
    assert fb.log.count("set_queue") == 1
    after = fb.state
    assert after.track.uri == before.track.uri and after.is_playing == before.is_playing
    assert after.context_uri == before.context_uri
    assert [t.provider for t in after.next_tracks].count("queue") == 0
    assert [t.uri for t in after.next_tracks] == [T(2), T(3), T(4), "spotify:delimiter"]


def test_never_lose_a_queue_short_readback_leaves_queue_and_fails(state_dir):
    fb = FakeBackend(load_cluster("playing.json"), short_readback=True)
    before = [(t.uri, t.uid) for t in fb.state.next_tracks]
    rc, out, _ = run(["park"], fb)
    assert rc != 0
    assert "playlist read-back" in out
    assert "set_queue" not in fb.log
    assert [(t.uri, t.uid) for t in fb.state.next_tracks] == before
    assert snapshots(state_dir), "snapshot must be on disk even when the park aborts"


def test_queue_not_cleared_before_snapshot_is_on_disk(state_dir, monkeypatch):
    fb = FakeBackend(load_cluster("playing.json"))

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(ops, "write_snapshot", boom)
    rc, out, _ = run(["park"], fb)
    assert rc == 2 and "snapshot failed" in out
    assert "set_queue" not in fb.log and "create_playlist" not in fb.log


def test_cookie_never_in_output_snapshots_or_argv(state_dir, tmp_path, monkeypatch):
    # A backend error that echoes the cookie must be scrubbed from output.
    fb = FakeBackend(load_cluster("playing.json"), error=RuntimeError("bad cookie " + FAKE_COOKIE))
    rc, out, seen = run(["park"], fb)
    assert rc == 2
    assert FAKE_COOKIE not in out and "<redacted>" in out
    assert seen["cookie"] == "sp_dc=" + FAKE_COOKIE  # reached the client in memory only

    fb = FakeBackend(load_cluster("playing.json"))
    rc, out, _ = run(["park"], fb)
    assert rc == 0 and FAKE_COOKIE not in out
    for p in snapshots(state_dir):
        assert FAKE_COOKIE not in p.read_text()

    # The 1Password reader gets only the op:// reference on its command line.
    argv_log = tmp_path / "argv.txt"
    reader = tmp_path / "fake-reader"
    reader.write_text(f'#!/bin/sh\necho "$@" >> {argv_log}\nprintf %s {FAKE_COOKIE}\n')
    reader.chmod(reader.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("QS_OP_READER", str(reader))
    assert session.read_sp_dc() == FAKE_COOKIE
    assert session.read_sp_dc(fresh=True) == FAKE_COOKIE
    logged = argv_log.read_text()
    assert FAKE_COOKIE not in logged and session.DEFAULT_OP_REF in logged and "--fresh" in logged


def test_repo_holds_no_cookie_value():
    root = Path(__file__).resolve().parents[1]
    files = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True).stdout.split()
    import re

    pat = re.compile(r"sp_dc=[A-Za-z0-9_\-]{40,}")
    for f in files:
        p = root / f
        if p.is_file() and p.suffix in {".py", ".md", ".json", ".toml", ".txt", ".lock"}:
            text = p.read_text(errors="ignore").replace("sp_dc=\" + FAKE_COOKIE", "")
            assert not pat.search(text), f


def test_private_api_change_fails_loud_missing_next_tracks():
    fb = FakeBackend(load_cluster("missing_next_tracks.json"))
    rc, out, _ = run(["park"], fb)
    assert rc == 2
    assert "read state failed" in out and "next_tracks" in out
    assert fb.log == ["refresh"]  # no partial clear, nothing written


def test_restore_never_starts_sound_without_a_picked_device(state_dir):
    fb = FakeBackend(load_cluster("playing.json"))
    assert run(["park"], fb)[0] == 0
    idle = FakeBackend(load_cluster("no_active.json"))
    rc, out, _ = run(["restore"], idle)
    assert rc == 3
    assert "Living Room" in out and "dell-rugged" in out
    assert "play" not in idle.log and "set_queue" not in idle.log


# ---------------------------------------------------------------- Phase 0 probe
def test_probe_no_active_device_exits_3_and_changes_nothing():
    fb = FakeBackend(load_cluster("no_active.json"))
    rc, out, _ = run(["probe", "--seed", "25"], fb)
    assert rc == 3 and "Living Room" in out
    assert fb.log == ["refresh"]


def test_probe_seed_25_visible_and_restored():
    fb = FakeBackend(load_cluster("playing.json"))
    start = [(t.uri, t.uid) for t in fb.state.next_tracks]
    rc, out, _ = run(["probe", "--seed", "25"], fb)
    assert rc == 0, out
    assert out.splitlines()[-1] == "seeded 25, visible >=25 (25), restored: yes"
    assert fb.log.count("add_to_queue") == 22
    assert [(t.uri, t.uid) for t in fb.state.next_tracks] == start


def test_probe_reports_a_capped_queue():
    fb = FakeBackend(load_cluster("playing.json"), queue_cap=20)
    rc, out, _ = run(["probe", "--seed", "25"], fb)
    assert rc == 1
    assert "visible <25 (20)" in out


# ---------------------------------------------------------------- Phase 3 restore
def _parked_then_elsewhere():
    fb = FakeBackend(load_cluster("playing.json"),
                     contexts={CTX: [T(0), T(1), T(2), T(3), T(4)],
                               "spotify:playlist:PARTY": [T(900), T(901)]})
    rc, out, _ = run(["park"], fb)
    assert rc == 0, out
    asyncio.run(fb.play("spotify:playlist:PARTY", track=None, position_ms=0))
    fb.log.clear()
    return fb


def test_restore_returns_song_position_and_queued_in_order():
    fb = _parked_then_elsewhere()
    rc, out, _ = run(["restore"], fb)
    assert rc == 0, out
    st = fb.state
    assert st.track.uri == T(1) and st.context_uri == CTX
    assert abs(st.position_ms - 61000) <= 5000
    assert [t.uri for t in st.next_tracks if t.provider == "queue"] == [T(101), T(102), T(103)]
    assert [t.uri for t in st.next_tracks if t.provider != "queue"] == [T(2), T(3), T(4)]
    assert "in order: yes" in out


def test_restore_with_device_flag_targets_that_device(state_dir):
    assert run(["park"], FakeBackend(load_cluster("playing.json")))[0] == 0
    idle = FakeBackend(load_cluster("no_active.json"), contexts={CTX: [T(1), T(2)]})
    rc, out, _ = run(["restore", "--device", "dell-rugged"], idle)
    assert rc == 0, out
    assert idle.selected == "dev-mac"


def test_restore_when_context_is_gone_restores_queued_and_says_so():
    fb = _parked_then_elsewhere()
    fb.missing_contexts.add(CTX)
    rc, out, _ = run(["restore"], fb)
    assert rc == 0, out
    assert "could not be played" in out and "context NOT restored" in out
    st = fb.state
    assert st.track.uri == T(1)
    assert [t.uri for t in st.next_tracks if t.provider == "queue"] == [T(101), T(102), T(103)]


def test_list_shows_parked_states():
    assert run(["park"], FakeBackend(load_cluster("playing.json")))[0] == 0
    lines = []
    assert cli.main(["list"], out=lines.append) == 0
    assert "Now Playing" in lines[0] and "queued=3" in lines[0] and "playlist/fake0" in lines[0]


def test_spotapi_version_is_pinned():
    assert installed_spotapi() == PINNED_SPOTAPI == "2.1.0"


def test_login_failure_reseeds_from_container_and_retries_once(monkeypatch):
    from spotapi.types.exceptions import HTTPError

    calls = {"reseed": 0, "fresh": [], "built": 0}
    monkeypatch.setattr(cli, "reseed_from_container", lambda: calls.__setitem__("reseed", calls["reseed"] + 1) or True)
    monkeypatch.setattr(cli, "read_sp_dc", lambda fresh=False: calls["fresh"].append(fresh) or FAKE_COOKIE)
    good = FakeBackend(load_cluster("no_active.json"))

    def factory(header):
        calls["built"] += 1
        if calls["built"] == 1:
            return FakeBackend(load_cluster("no_active.json"), error=HTTPError("401", status_code=401))
        return good

    lines = []
    rc = cli.main(["probe"], factory=factory, out=lines.append)
    assert rc == 3  # second attempt reached the account (nothing active)
    assert calls == {"reseed": 1, "fresh": [False, True], "built": 2}
