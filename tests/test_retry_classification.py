"""Tests for fatal-vs-retryable error classification and retry pool exclusion."""

from datetime import timedelta

from pipeline import publish_queue as pq


# ── is_fatal_error ──────────────────────────────────────────────────────────────

def test_missing_credentials_error_is_fatal():
    # Mirrors publish.py's actual missing-credential message
    exc = EnvironmentError(
        "TIKTOK_CLIENT_KEY not set for channel 'ilb'. "
        "Run: python setup_credentials.py --platform tiktok --channel ilb"
    )
    assert pq.is_fatal_error(exc) is True


def test_value_error_is_fatal():
    assert pq.is_fatal_error(ValueError("Unknown TikTok post_mode 'bogus'")) is True


def test_invalid_grant_text_is_fatal():
    assert pq.is_fatal_error(Exception("oauth failure: invalid_grant")) is True


def test_403_text_is_fatal():
    assert pq.is_fatal_error(Exception("HTTP 403 Forbidden")) is True


def test_timeout_is_retryable():
    assert pq.is_fatal_error(TimeoutError("read timed out")) is False


def test_500_is_retryable():
    assert pq.is_fatal_error(Exception("HTTP 500 internal server error")) is False


# ── Retry pool behavior with fatal results ──────────────────────────────────────

def _enqueue_past(queue_paths, tmp_path, platforms=("youtube", "tiktok")):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x" * 200_000)
    past = (pq._now_utc() - timedelta(hours=2)).isoformat()
    return pq.enqueue(
        clip_path=str(clip),
        platforms=list(platforms),
        title="t",
        description="d",
        scheduled_time_iso=past,
    )


def test_fatal_failure_excluded_from_retry_pool(queue_paths, tmp_path):
    post_id = _enqueue_past(queue_paths, tmp_path, platforms=("tiktok",))
    pq.mark_failed(post_id, "tiktok", "EnvironmentError: creds missing", fatal=True)
    assert pq.get_retryable() == []


def test_nonfatal_failure_enters_retry_pool(queue_paths, tmp_path):
    post_id = _enqueue_past(queue_paths, tmp_path, platforms=("tiktok",))
    pq.mark_failed(post_id, "tiktok", "TimeoutError: read timed out")
    retryable = pq.get_retryable()
    assert [e["post_id"] for e in retryable] == [post_id]


def test_mixed_fatal_and_ok_not_retryable(queue_paths, tmp_path):
    post_id = _enqueue_past(queue_paths, tmp_path)
    pq.mark_complete(post_id, "youtube", {"video_id": "abc"})
    pq.mark_failed(post_id, "tiktok", "401 unauthorized", fatal=True)
    assert pq.get_retryable() == []


def test_retry_failed_clears_fatal_and_rearms(queue_paths, tmp_path):
    post_id = _enqueue_past(queue_paths, tmp_path)
    pq.mark_complete(post_id, "youtube", {"video_id": "abc"})
    pq.mark_failed(post_id, "tiktok", "401 unauthorized", fatal=True)

    ok, to_retry = pq.retry_failed(post_id)
    assert ok is True
    assert to_retry == ["tiktok"]

    entry = pq.get_entry(post_id)
    assert entry["status"] == "pending"
    # The ok result must be preserved (idempotency), the fatal one cleared.
    assert entry["results"]["youtube"]["status"] == "ok"
    assert "tiktok" not in entry["results"]
