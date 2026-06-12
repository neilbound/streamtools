"""Tests for publish queue state transitions, warnings persistence, dedupe."""

from datetime import timedelta

from pipeline import publish_queue as pq


def _future_iso(hours=24):
    return (pq._now_utc() + timedelta(hours=hours)).isoformat()


def _clip(tmp_path, name="clip.mp4"):
    p = tmp_path / name
    p.write_bytes(b"x" * 200_000)
    return str(p)


# ── Warnings persistence ────────────────────────────────────────────────────────

def test_enqueue_persists_title_truncation_warning(queue_paths, tmp_path):
    post_id = pq.enqueue(
        clip_path=_clip(tmp_path),
        platforms=["youtube"],
        title="x" * 150,
        description="d",
        scheduled_time_iso=_future_iso(),
    )
    entry = pq.get_entry(post_id)
    assert any("truncated" in w for w in entry["warnings"])
    assert len(entry["title"]) == 100


def test_enqueue_persists_tiktok_caption_warning(queue_paths, tmp_path):
    post_id = pq.enqueue(
        clip_path=_clip(tmp_path),
        platforms=["tiktok"],
        title="t",
        description="d",
        scheduled_time_iso=_future_iso(),
        extra={"tiktok_caption": "y" * 200},
    )
    entry = pq.get_entry(post_id)
    assert any("TikTok caption" in w for w in entry["warnings"])


def test_enqueue_clean_entry_has_no_warnings(queue_paths, tmp_path):
    post_id = pq.enqueue(
        clip_path=_clip(tmp_path),
        platforms=["youtube"],
        title="t",
        description="d",
        scheduled_time_iso=_future_iso(),
    )
    assert pq.get_entry(post_id)["warnings"] == []


# ── Duplicate handling ──────────────────────────────────────────────────────────

def test_one_hour_dedupe_returns_existing_post_id(queue_paths, tmp_path):
    clip = _clip(tmp_path)
    when = _future_iso()
    a = pq.enqueue(clip_path=clip, platforms=["youtube"], title="t",
                   description="d", scheduled_time_iso=when)
    b = pq.enqueue(clip_path=clip, platforms=["youtube"], title="t",
                   description="d", scheduled_time_iso=when)
    assert a == b


def test_duplicate_risk_warning_same_channel_shared_platform(queue_paths, tmp_path):
    clip = _clip(tmp_path)
    pq.enqueue(clip_path=clip, platforms=["youtube"], title="t",
               description="d", scheduled_time_iso=_future_iso(24))
    # >1h apart so the hard dedupe doesn't kick in
    b = pq.enqueue(clip_path=clip, platforms=["youtube", "tiktok"], title="t",
                   description="d", scheduled_time_iso=_future_iso(72))
    entry = pq.get_entry(b)
    assert any("DUPLICATE RISK" in w for w in entry["warnings"])


def test_no_duplicate_risk_across_channels(queue_paths, tmp_path):
    clip = _clip(tmp_path)
    pq.enqueue(clip_path=clip, platforms=["youtube"], title="t",
               description="d", scheduled_time_iso=_future_iso(24),
               channel="neilbound")
    b = pq.enqueue(clip_path=clip, platforms=["youtube"], title="t",
                   description="d", scheduled_time_iso=_future_iso(72),
                   channel="ilb")
    entry = pq.get_entry(b)
    assert not any("DUPLICATE RISK" in w for w in entry["warnings"])


# ── Status transitions ──────────────────────────────────────────────────────────

def test_all_ok_marks_complete(queue_paths, tmp_path):
    post_id = pq.enqueue(clip_path=_clip(tmp_path), platforms=["youtube", "tiktok"],
                         title="t", description="d", scheduled_time_iso=_future_iso())
    pq.mark_complete(post_id, "youtube", {})
    assert pq.get_entry(post_id)["status"] == "partial"
    pq.mark_complete(post_id, "tiktok", {})
    assert pq.get_entry(post_id)["status"] == "complete"


def test_mixed_ok_and_error_marks_partial(queue_paths, tmp_path):
    post_id = pq.enqueue(clip_path=_clip(tmp_path), platforms=["youtube", "tiktok"],
                         title="t", description="d", scheduled_time_iso=_future_iso())
    pq.mark_complete(post_id, "youtube", {})
    pq.mark_failed(post_id, "tiktok", "boom")
    assert pq.get_entry(post_id)["status"] == "partial"


def test_only_errors_marks_failed(queue_paths, tmp_path):
    post_id = pq.enqueue(clip_path=_clip(tmp_path), platforms=["tiktok"],
                         title="t", description="d", scheduled_time_iso=_future_iso())
    pq.mark_failed(post_id, "tiktok", "boom")
    assert pq.get_entry(post_id)["status"] == "failed"


# ── schedule_retry backoff ──────────────────────────────────────────────────────

def test_schedule_retry_backoff_and_budget(queue_paths, tmp_path):
    post_id = pq.enqueue(clip_path=_clip(tmp_path), platforms=["tiktok"],
                         title="t", description="d", scheduled_time_iso=_future_iso())
    pq.mark_failed(post_id, "tiktok", "boom")

    for attempt in range(1, pq.DEFAULT_MAX_ATTEMPTS):
        will_retry, attempts = pq.schedule_retry(post_id)
        assert will_retry is True
        assert attempts == attempt
        assert pq.get_entry(post_id)["next_retry_at"] is not None

    will_retry, attempts = pq.schedule_retry(post_id)
    assert will_retry is False
    assert attempts == pq.DEFAULT_MAX_ATTEMPTS
    assert pq.get_entry(post_id)["next_retry_at"] is None


def test_get_entry_missing_returns_none(queue_paths):
    assert pq.get_entry("nope1234") is None


# ── TikTok manual-post confirmation ─────────────────────────────────────────────

def test_confirm_manual_post_sets_flag(queue_paths, tmp_path):
    post_id = pq.enqueue(clip_path=_clip(tmp_path), platforms=["tiktok"],
                         title="t", description="d", scheduled_time_iso=_future_iso())
    pq.mark_complete(post_id, "tiktok", {
        "publish_id": "v_inbox_file~123", "requires_manual_post": True,
    })
    assert pq.confirm_manual_post(post_id, "tiktok") is True
    res = pq.get_entry(post_id)["results"]["tiktok"]
    assert res["manually_posted"] is True


def test_confirm_manual_post_rejects_direct_post(queue_paths, tmp_path):
    post_id = pq.enqueue(clip_path=_clip(tmp_path), platforms=["tiktok"],
                         title="t", description="d", scheduled_time_iso=_future_iso())
    pq.mark_complete(post_id, "tiktok", {"publish_id": "v_pub.456"})
    assert pq.confirm_manual_post(post_id, "tiktok") is False


def test_confirm_manual_post_missing_entry(queue_paths):
    assert pq.confirm_manual_post("nope1234", "tiktok") is False
