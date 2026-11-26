"""Songs queued from a phone read provider "context" + metadata.is_queued; they must count as queued."""
from spotapi.models.playback import PlayerState

from queue_saver.ops import queued
from queue_saver.spotify import mark_queued


def _cluster():
    # Shape recorded live 2026-11-26 (iPhone active): 1st queued entry provider "queue", the rest "context".
    q = {"is_queued": "true", "queued_by": "kyzcreig"}
    return {"player_state": {
        "track": {"uri": "spotify:track:cur", "uid": "c1", "provider": "context", "metadata": {}},
        "next_tracks": [
            {"uri": "spotify:track:a", "uid": "qbcb79575a8933a0c", "provider": "queue", "metadata": dict(q)},
            {"uri": "spotify:track:b", "uid": "q7e42bc5c6972365d", "provider": "context", "metadata": dict(q)},
            {"uri": "spotify:track:c", "uid": "q3336c82612a20565", "provider": "context", "metadata": dict(q)},
            {"uri": "spotify:track:b", "uid": "a5573b7355e052833b12", "provider": "context", "metadata": {}},
            {"uri": "spotify:track:d", "uid": "cd637a63605f3e9ab625", "provider": "context"},
        ],
    }}


def test_phone_queued_entries_count_as_queued():
    st = PlayerState.from_cluster(mark_queued(_cluster()))
    assert [t.uri[-1] for t in queued(st)] == ["a", "b", "c"]
    assert [t.provider for t in st.next_tracks] == ["queue", "queue", "queue", "context", "context"]


def test_without_the_fix_only_the_first_is_seen():
    st = PlayerState.from_cluster(_cluster())
    assert len(queued(st)) == 1  # the 2026-11-26 failure: 1 of 3 parked


def test_tolerates_odd_shapes():
    assert mark_queued({}) == {}
    assert mark_queued({"player_state": None}) == {"player_state": None}
    assert mark_queued({"player_state": {"next_tracks": [None, {"uri": "x", "metadata": "?"}]}})
