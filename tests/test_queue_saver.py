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
    assert calls == {"reseed": 1, "fresh": [False, True, True], "built": 2}


# ---------------------------------------------------------------- review-fix round (PR #1)
def _cluster_with_queue(*queued):
    c = load_cluster("playing.json")
    nt = c["player_state"]["next_tracks"]
    c["player_state"]["next_tracks"] = [*queued, *[t for t in nt if t["provider"] != "queue"]]
    return c


def test_probe_seed_does_not_remove_a_song_another_client_queued():
    class Racing(FakeBackend):  # another client queues a song right after the probe's first add
        async def add_to_queue(self, uri):
            await super().add_to_queue(uri)
            if self.log.count("add_to_queue") == 1:
                self.ps["next_tracks"].insert(0, {"uri": T(777), "uid": "foreign", "provider": "queue"})

    fb = Racing(load_cluster("playing.json"))
    rc, out, _ = run(["probe", "--seed", "4"], fb)
    assert rc == 2 and "seed: remove added failed" in out, out
    assert "set_queue" not in fb.log
    assert "foreign" in [t.uid for t in fb.state.next_tracks]


def test_write_snapshot_reserves_a_unique_name_when_two_parks_race(monkeypatch):
    from queue_saver import state

    monkeypatch.setattr(state.time, "strftime", lambda fmt, *a: "20261120-120000")
    real_exists = Path.exists
    # Both writers pass the existence check before either has written.
    monkeypatch.setattr(Path, "exists", lambda p: False if p.suffix == ".json" else real_exists(p))
    a = snapshot_from_cluster(load_cluster("playing.json"), "2.1.0")
    b = snapshot_from_cluster(load_cluster("playing.json"), "2.1.0")
    b.track.uri = T(2)
    pa, pb = state.write_snapshot(a), state.write_snapshot(b)
    assert pa != pb
    assert Snapshot.from_json(pa.read_text()).track.uri == T(1)
    assert Snapshot.from_json(pb.read_text()).track.uri == T(2)


def test_bare_restore_loads_the_latest_same_second_snapshot(state_dir):
    from queue_saver import state

    d = state_dir / "snapshots"
    d.mkdir(parents=True)
    snap = snapshot_from_cluster(load_cluster("playing.json"), "2.1.0")
    for name, uri in (("20261120-120000", T(1)), ("20261120-120000-1", T(2)),
                      ("20261120-120000-2", T(3)), ("20261120-120000-10", T(4))):
        snap.track.uri = uri
        (d / f"{name}.json").write_text(snap.to_json())
        state.mark_completed(d / f"{name}.json")
    path, got = state.load_snapshot(None)
    assert path.name == "20261120-120000-10.json" and got.track.uri == T(4)


# ---------------------------------------------------------------- restore after a failed park
def test_completed_park_marks_its_snapshot(state_dir):
    from queue_saver import state

    rc, out, _ = run(["park"], FakeBackend(load_cluster("playing.json")))
    assert rc == 0, out
    (snap,) = snapshots(state_dir)
    assert state.is_completed(snap)


def test_snapshot_dir_and_marker_are_private_under_a_loose_umask(state_dir):
    """t_58370087 round 3: the snapshot dir is 0700 and the .parked marker 0600 (umask 022)."""
    import stat

    from queue_saver import state

    (state_dir / "snapshots").mkdir(parents=True, mode=0o755)
    old = os.umask(0o022)
    try:
        rc, out, _ = run(["park"], FakeBackend(load_cluster("playing.json")))
    finally:
        os.umask(old)
    assert rc == 0, out
    (snap,) = snapshots(state_dir)
    assert stat.S_IMODE((state_dir / "snapshots").stat().st_mode) == 0o700
    assert stat.S_IMODE(state.completed_marker(snap).stat().st_mode) == 0o600


def test_park_that_fails_before_readback_leaves_no_marker(state_dir):
    from queue_saver import state

    rc, out, _ = run(["park"], FakeBackend(load_cluster("playing.json"), short_readback=True))
    assert rc != 0 and "playlist read-back" in out
    (snap,) = snapshots(state_dir)
    assert not state.is_completed(snap)


def test_restore_after_a_failed_park_uses_the_previous_completed_snapshot(state_dir):
    from queue_saver import state

    rc, out, _ = run(["park"], FakeBackend(load_cluster("playing.json")))
    assert rc == 0, out
    (good,) = snapshots(state_dir)
    rc, out, _ = run(["park"], FakeBackend(load_cluster("playing.json"), short_readback=True))
    assert rc != 0
    newer = [p for p in snapshots(state_dir) if p != good]
    assert len(newer) == 1 and state.list_snapshots()[-1] == newer[0]  # the failed one is newest
    path, _snap = state.load_snapshot(None)
    assert path == good
    fb = FakeBackend(load_cluster("playing.json"))
    rc, out, _ = run(["restore"], fb)
    assert f"restoring {good.name}:" in out


def test_restore_with_only_failed_parks_says_there_is_nothing_parked(state_dir):
    rc, out, _ = run(["park"], FakeBackend(load_cluster("playing.json"), short_readback=True))
    assert rc != 0
    rc, out, _ = run(["restore"], FakeBackend(load_cluster("playing.json")))
    assert rc == 2 and "no parked snapshots" in out


def test_park_leaves_queued_local_files_it_cannot_restore():
    local = {"uri": "spotify:local:Artist:Album:Song:180", "uid": "uidlocal", "provider": "queue"}
    fb = FakeBackend(_cluster_with_queue(
        {"uri": T(101), "uid": "uid101q", "provider": "queue"}, local))
    rc, out, _ = run(["park"], fb)
    assert rc == 1, out
    assert "cannot requeue" in out and "spotify:local:" in out
    queued_now = [(t.uri, t.uid) for t in fb.state.next_tracks if t.provider == "queue"]
    assert queued_now == [(local["uri"], "uidlocal")]


def test_restore_of_a_paused_snapshot_leaves_it_paused(state_dir):
    c = load_cluster("playing.json")
    c["player_state"]["is_playing"], c["player_state"]["is_paused"] = False, True
    assert run(["park"], FakeBackend(c))[0] == 0
    fb = FakeBackend(load_cluster("playing.json"), contexts={CTX: [T(0), T(1), T(2), T(3), T(4)]})
    rc, out, _ = run(["restore"], fb)
    assert rc == 0, out
    assert fb.state.is_playing is False and fb.state.is_paused is True


def test_restore_of_an_empty_queue_keeps_songs_queued_since_the_park(state_dir):
    c = load_cluster("playing.json")
    c["player_state"]["next_tracks"] = [t for t in c["player_state"]["next_tracks"] if t["provider"] != "queue"]
    assert run(["park"], FakeBackend(c))[0] == 0

    class KeepsQueue(FakeBackend):  # Spotify keeps manually queued songs across play()
        async def play(self, uri, *, track, position_ms):
            kept = [t for t in self.ps["next_tracks"] if t.get("provider") == "queue"]
            await super().play(uri, track=track, position_ms=position_ms)
            self.ps["next_tracks"][:0] = kept

    fb = KeepsQueue(load_cluster("playing.json"), contexts={CTX: [T(0), T(1), T(2), T(3), T(4)]})
    rc, out, _ = run(["restore"], fb)
    assert rc == 0, out
    assert [t.uri for t in fb.state.next_tracks if t.provider == "queue"] == [T(101), T(102), T(103)]
    assert "kept 3 song(s) queued since the park" in out


def test_restore_fails_when_the_seek_did_not_take_effect():
    class DeafSeek(FakeBackend):  # loads the song at 0 ms and acknowledges seek without moving
        async def play(self, uri, *, track, position_ms):
            await super().play(uri, track=track, position_ms=0)

        async def seek(self, position_ms):
            self.log.append("seek")

    fb = _parked_then_elsewhere()
    deaf = DeafSeek(fb.cluster, contexts=fb.contexts)
    rc, out, _ = run(["restore"], deaf)
    assert rc == 1, out
    assert "in order: yes" in out and "seek: position 0ms" in out


def test_restore_refuses_an_ambiguous_device_name(state_dir):
    assert run(["park"], FakeBackend(load_cluster("playing.json")))[0] == 0
    c = load_cluster("no_active.json")
    c["devices"] = {"dev-a": {"name": "Living Room", "device_type": "SPEAKER"},
                    "dev-b": {"name": "Living Room", "device_type": "TV"},
                    "dev-c": {"name": "Living Room", "device_type": "SPEAKER"}}
    idle = FakeBackend(c, contexts={CTX: [T(1), T(2)]})
    rc, out, _ = run(["restore", "--device", "Living Room"], idle)
    assert rc == 3, out
    assert idle.selected is None and "play" not in idle.log
    assert all(f"id={i}" in out for i in ("dev-a", "dev-b", "dev-c"))


def test_reseed_refuses_when_qs_op_ref_overrides_the_read_item(monkeypatch):
    calls = []
    monkeypatch.setattr(session.subprocess, "run", lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0))
    monkeypatch.setenv("QS_OP_REF", "op://Test/AlternateSpotify/credential")
    with pytest.raises(session.CookieError, match="QS_OP_REF"):
        session.reseed_from_container()
    assert calls == []


def test_reseed_writes_the_item_the_reader_reads(monkeypatch):
    seen = {}
    monkeypatch.setattr(session.subprocess, "run",
                        lambda argv, **k: seen.update(k) or subprocess.CompletedProcess(argv, 0))
    monkeypatch.delenv("QS_OP_REF", raising=False)
    monkeypatch.setenv("QS_OP_ITEM", "someOtherItem")
    monkeypatch.setenv("QS_OP_VAULT", "someOtherVault")
    assert session.reseed_from_container() is True
    vault, item = session.DEFAULT_OP_REF.split("/")[2:4]
    assert seen["env"]["QS_OP_ITEM"] == item and seen["env"]["QS_OP_VAULT"] == vault


def _load_reseed_script(monkeypatch, cookies):
    import importlib.util
    import json
    import sys
    import types

    class WS:
        def send(self, msg):
            pass

        def recv(self, timeout=None):
            return json.dumps({"id": 1, "result": {"cookies": cookies}})

        def close(self):
            pass

    client = types.ModuleType("websockets.sync.client")
    client.connect = lambda *a, **k: WS()
    for name, mod in (("websockets", types.ModuleType("websockets")),
                      ("websockets.sync", types.ModuleType("websockets.sync")),
                      ("websockets.sync.client", client)):
        monkeypatch.setitem(sys.modules, name, mod)
    path = Path(__file__).resolve().parents[1] / "scripts" / "reseed_sp_dc.py"
    spec = importlib.util.spec_from_file_location("reseed_sp_dc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return mod


def _fake_cdp(monkeypatch, mod, hits):
    import io
    import json

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        hits.append(url)
        if "/json/new" in url:
            return io.BytesIO(json.dumps({"id": "TAB1", "webSocketDebuggerUrl": "ws://localhost:1/x"}).encode())
        return io.BytesIO(b"Target is closing")

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)


def test_reseed_closes_the_tab_it_opened(monkeypatch):
    mod = _load_reseed_script(monkeypatch, [{"name": "sp_dc", "value": FAKE_COOKIE}])
    hits = []
    _fake_cdp(monkeypatch, mod, hits)
    assert mod.read_cookie("9222") == FAKE_COOKIE
    assert any(h.endswith("/json/close/TAB1") for h in hits), hits


def test_reseed_edits_only_the_credential_of_the_full_item(monkeypatch, tmp_path):
    import json

    mod = _load_reseed_script(monkeypatch, [{"name": "sp_dc", "value": FAKE_COOKIE}])
    _fake_cdp(monkeypatch, mod, [])
    token = tmp_path / "token"
    token.write_text("svc")
    monkeypatch.setattr(mod, "TOKEN_FILE", str(token))
    monkeypatch.setenv("CDP_PORT", "9222")
    item = {"id": "x", "title": "Spotify web session sp_dc", "fields": [
        {"id": "credential", "type": "CONCEALED", "label": "credential", "value": "old"},
        {"id": "recovery_hint", "type": "STRING", "label": "recovery_hint", "value": "keep me"}]}
    edits = []

    def fake_run(argv, **k):
        if argv[:3] == ["op", "item", "get"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(item), "")
        edits.append(json.loads(k["input"]))
        assert FAKE_COOKIE not in " ".join(argv)
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod.main() == 0
    (sent,) = edits
    by_id = {f["id"]: f["value"] for f in sent["fields"]}
    assert by_id == {"credential": FAKE_COOKIE, "recovery_hint": "keep me"}


def test_fake_playlist_ids_never_repeat_after_a_delete():
    fb = FakeBackend(load_cluster("playing.json"))

    async def go():
        a = await fb.create_playlist("a")
        b = await fb.create_playlist("b")
        await fb.add_tracks(b, [T(5)])
        await fb.delete_playlist(a)
        c = await fb.create_playlist("c")
        return b, c

    b, c = asyncio.run(go())
    assert c != b and fb.playlists[b] == [T(5)]


def _login_retry(monkeypatch, first):
    calls = {"reseed": 0, "built": 0}
    monkeypatch.setattr(cli, "reseed_from_container", lambda: calls.__setitem__("reseed", calls["reseed"] + 1) or True)
    good = FakeBackend(load_cluster("no_active.json"))

    def factory(header):
        calls["built"] += 1
        return first if calls["built"] == 1 else good

    return calls, factory


def test_login_failure_on_the_first_state_read_is_retried(monkeypatch):
    from spotapi.types.exceptions import HTTPError

    class Expired(FakeBackend):
        async def refresh(self):
            self.log.append("refresh")
            raise HTTPError("401", status_code=401)

    calls, factory = _login_retry(monkeypatch, Expired(load_cluster("playing.json")))
    rc = cli.main(["probe"], factory=factory, out=[].append)
    assert rc == 3 and calls == {"reseed": 1, "built": 2}


def test_login_failure_after_a_mutation_is_not_replayed(monkeypatch):
    from spotapi.types.exceptions import HTTPError

    class ExpiresMidPark(FakeBackend):
        async def set_private(self, uri):
            self.log.append("set_private")
            raise HTTPError("401", status_code=401)

    first = ExpiresMidPark(load_cluster("playing.json"))
    calls, factory = _login_retry(monkeypatch, first)
    lines = []
    rc = cli.main(["park"], factory=factory, out=lines.append)
    assert rc == 2 and "create playlist failed" in "\n".join(lines)
    assert calls == {"reseed": 0, "built": 1}


# ---------------------------------------------------------------- review-fix round 2 (PR #1)
LOCAL = "spotify:local:Artist:Album:Song:180"


class KeepsQueue(FakeBackend):  # Spotify keeps manually queued songs across play()
    async def play(self, uri, *, track, position_ms):
        kept = [t for t in self.ps["next_tracks"] if t.get("provider") == "queue"]
        await super().play(uri, track=track, position_ms=position_ms)
        self.ps["next_tracks"][:0] = kept


def test_restore_keeps_queued_local_files_it_cannot_requeue(state_dir):
    c = _cluster_with_queue({"uri": LOCAL, "uid": "uidlocal", "provider": "queue"})
    assert run(["park"], FakeBackend(c))[0] == 1  # the local file stays queued
    fb = KeepsQueue(load_cluster("playing.json"), contexts={CTX: [T(0), T(1), T(2), T(3), T(4)]})
    fb.ps["next_tracks"] = [{"uri": LOCAL, "uid": "uidlocal", "provider": "queue"}]
    rc, out, _ = run(["restore"], fb)
    assert rc == 0, out
    assert [(t.uri, t.uid) for t in fb.state.next_tracks if t.provider == "queue"] == [(LOCAL, "uidlocal")]


def test_park_reports_a_queued_track_without_uid_as_left_in_place():
    fb = FakeBackend(_cluster_with_queue(
        {"uri": T(101), "uid": "uid101q", "provider": "queue"},
        {"uri": T(102), "uid": "", "provider": "queue"}))
    rc, out, _ = run(["park", "--queued-only"], fb)
    assert rc == 1, out
    assert "left 1 queued item(s)" in out and T(102) in out
    assert [t.uri for t in fb.state.next_tracks if t.provider == "queue"] == [T(102)]


def test_restore_falls_back_to_the_song_when_the_context_loads_another(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(ops.asyncio, "sleep", lambda s: real_sleep(0))
    c = load_cluster("playing.json")
    ps = c["player_state"]
    ps["track"] = {"uri": T(101), "uid": "uid101q", "provider": "queue"}
    ps["next_tracks"] = [t for t in ps["next_tracks"] if t["uri"] != T(101)]
    fb = FakeBackend(c, contexts={CTX: [T(1), T(2)]})
    assert run(["park"], fb)[0] == 0
    rc, out, _ = run(["restore"], fb)
    assert rc == 0, out
    assert "context NOT restored" in out
    st = fb.state
    assert st.track.uri == T(101)
    assert [t.uri for t in st.next_tracks if t.provider == "queue"] == [T(102), T(103)]


def test_restore_seeks_when_the_position_moves_while_pausing(state_dir):
    c = load_cluster("playing.json")
    c["player_state"]["is_playing"], c["player_state"]["is_paused"] = False, True
    assert run(["park"], FakeBackend(c))[0] == 0

    class LatePause(FakeBackend):  # playback runs on 10 s before the pause takes effect
        async def pause(self):
            self.ps["position_as_of_timestamp"] += 10000
            await super().pause()

    fb = LatePause(load_cluster("playing.json"), contexts={CTX: [T(0), T(1), T(2), T(3), T(4)]})
    rc, out, _ = run(["restore"], fb)
    assert rc == 0, out
    assert "seek" in fb.log


def test_write_snapshot_never_shows_an_incomplete_snapshot(state_dir, monkeypatch):
    from queue_saver import state

    snap = snapshot_from_cluster(load_cluster("playing.json"), "2.1.0")
    seen = []
    real = snap.to_json
    snap.to_json = lambda: seen.append([p.stat().st_size for p in state.list_snapshots()]) or real()
    path = state.write_snapshot(snap)
    assert seen == [[]], seen
    assert [p.name for p in state.list_snapshots()] == [path.name]
    assert not list(path.parent.glob("*.tmp"))

    def boom(*a, **k):
        raise OSError("no space")

    monkeypatch.setattr(state.tempfile, "mkstemp", boom)
    with pytest.raises(OSError):
        state.write_snapshot(snapshot_from_cluster(load_cluster("playing.json"), "2.1.0"))
    assert [p.name for p in state.list_snapshots()] == [path.name]


# ---------------------------------------------------------------- review-fix round 3 (PR #1)
@pytest.mark.parametrize("cap", [25, None])
def test_park_never_clears_a_queue_that_may_be_longer_than_shown(state_dir, cap):
    queue = [{"uri": T(200 + i), "uid": f"q{i}", "provider": "queue"} for i in range(26)]
    fb = FakeBackend(_cluster_with_queue(*queue), queue_cap=cap)
    rc, out, _ = run(["park", "--queued-only"], fb)
    assert rc == 1, out
    assert "may be longer than what Spotify showed" in out
    assert "set_queue" not in fb.log
    assert [t["uid"] for t in fb.ps["next_tracks"] if t["provider"] == "queue"] == [t["uid"] for t in queue]
    (pl,) = fb.playlists.values()
    assert len(pl) == (25 if cap else 26) and snapshots(state_dir)


def test_restore_puts_saved_songs_first_and_keeps_songs_queued_since_the_park():
    fb = _parked_then_elsewhere()
    fb = KeepsQueue(fb.cluster, contexts=fb.contexts)
    asyncio.run(fb.add_to_queue(T(500)))
    rc, out, _ = run(["restore"], fb)
    assert rc == 0, out
    assert [t.uri for t in fb.state.next_tracks if t.provider == "queue"] == [T(101), T(102), T(103), T(500)]
    assert "in order: yes" in out and f"kept 1 song(s) queued since the park, after the restored ones: {T(500)}" in out


def _rejected_then(monkeypatch, fresh_value):
    from spotapi.types.exceptions import HTTPError

    calls = {"reseed": 0, "fresh": [], "headers": []}
    monkeypatch.setattr(cli, "reseed_from_container", lambda: calls.__setitem__("reseed", calls["reseed"] + 1) or True)
    monkeypatch.setattr(cli, "read_sp_dc", lambda fresh=False: calls["fresh"].append(fresh) or (fresh_value if fresh else FAKE_COOKIE))

    def factory(header):
        calls["headers"].append(header)
        if len(calls["headers"]) == 1:
            return FakeBackend(load_cluster("no_active.json"), error=HTTPError("401", status_code=401))
        return FakeBackend(load_cluster("no_active.json"))

    rc = cli.main(["probe"], factory=factory, out=[].append)
    return rc, calls


def test_login_rejection_retries_a_newer_vault_value_without_reseeding(monkeypatch):
    newer = FAKE_COOKIE[::-1]
    rc, calls = _rejected_then(monkeypatch, newer)
    assert rc == 3
    assert calls["reseed"] == 0 and calls["fresh"] == [False, True]
    assert calls["headers"][1] == cli.cookie_header(newer)


def test_login_rejection_reseeds_only_when_the_fresh_value_is_the_rejected_one(monkeypatch):
    rc, calls = _rejected_then(monkeypatch, FAKE_COOKIE)
    assert rc == 3
    assert calls["reseed"] == 1 and calls["fresh"] == [False, True, True]
