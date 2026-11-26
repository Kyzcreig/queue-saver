import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import pytest


@pytest.fixture(autouse=True)
def _fast_playback_watch(monkeypatch):
    """park watches playback for a few seconds after its set_queue; offline that is one read."""
    from queue_saver import ops

    monkeypatch.setattr(ops, "WATCH_S", 0.0)
    monkeypatch.setattr(ops, "WATCH_EVERY_S", 0.0)
