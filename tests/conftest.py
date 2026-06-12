"""
Shared fixtures for streamtools tests.

Tests are pure-Python: no network, no real ffmpeg, no GPU. Anything that
touches the queue file is repointed at a tmp_path so the real
output/publish_queue.json is never read or written.
"""

import sys
import os

import pytest

# Make the streamtools root importable when pytest is run from anywhere
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def queue_paths(tmp_path, monkeypatch):
    """Repoint the publish queue (and its lock) at a per-test temp file."""
    from pipeline import publish_queue

    qpath = str(tmp_path / "publish_queue.json")
    monkeypatch.setattr(publish_queue, "_QUEUE_PATH", qpath)
    monkeypatch.setattr(publish_queue, "_LOCK_PATH", qpath + ".lock")
    return qpath
