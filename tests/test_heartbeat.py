"""Daemon heartbeat: proof-of-life tracking that surfaces a dead/disabled
Task Scheduler task (the failure mode that once caused a multi-day silent gap)."""
from datetime import datetime, timedelta, timezone

import pytest

import publisher_daemon as daemon


@pytest.fixture
def hb_path(tmp_path, monkeypatch):
    p = tmp_path / ".last_daemon_heartbeat"
    monkeypatch.setattr(daemon, "HEARTBEAT_PATH", str(p))
    return p


def test_no_heartbeat_yet(hb_path):
    assert daemon.heartbeat_age_seconds() is None


def test_write_then_age(hb_path):
    now = datetime.now(tz=timezone.utc)
    daemon.write_heartbeat(now)
    assert hb_path.exists()
    assert daemon.heartbeat_age_seconds(now) == 0.0
    later = now + timedelta(hours=2)
    assert daemon.heartbeat_age_seconds(later) == pytest.approx(7200.0)


def test_stale_threshold_meaningful(hb_path):
    """>1h stale (>=3 missed 15-min runs) must exceed the warning threshold."""
    now = datetime.now(tz=timezone.utc)
    daemon.write_heartbeat(now - timedelta(minutes=90))
    assert daemon.heartbeat_age_seconds(now) > daemon.HEARTBEAT_STALE_SECS


def test_corrupt_heartbeat_reads_as_none(hb_path):
    hb_path.write_text("not-a-timestamp")
    assert daemon.heartbeat_age_seconds() is None


def test_write_never_raises(hb_path, monkeypatch):
    """A heartbeat failure must not break a publishing run."""
    # Point the heartbeat at a path whose parent is a FILE — open() will fail.
    blocker = hb_path.parent / "blocker"
    blocker.write_text("")
    monkeypatch.setattr(daemon, "HEARTBEAT_PATH", str(blocker / "hb"))
    daemon.write_heartbeat()  # swallows the OSError

    # Clock going backwards (or a future-stamped file) must clamp to 0, not go negative.
    monkeypatch.setattr(daemon, "HEARTBEAT_PATH", str(hb_path))
    now = datetime.now(tz=timezone.utc)
    daemon.write_heartbeat(now)
    assert daemon.heartbeat_age_seconds(now - timedelta(minutes=5)) == 0.0
