"""Tests for YouTube upload-health classification (pure logic, no network)."""
from pipeline.publish import classify_youtube_health


def test_healthy_real_duration():
    item = {"contentDetails": {"duration": "PT1M2S"},
            "processingDetails": {"processingStatus": "processing"}}
    assert classify_youtube_health(item) == "ok"


def test_truncated_p0d_failed():
    item = {"contentDetails": {"duration": "P0D"},
            "processingDetails": {"processingStatus": "failed"}}
    assert classify_youtube_health(item) == "truncated"


def test_truncated_empty_duration_terminated():
    item = {"contentDetails": {},
            "processingDetails": {"processingStatus": "terminated"}}
    assert classify_youtube_health(item) == "truncated"


def test_pending_p0d_still_processing():
    # P0D while still processing is ambiguous at this instant — caller polls.
    item = {"contentDetails": {"duration": "P0D"},
            "processingDetails": {"processingStatus": "processing"}}
    assert classify_youtube_health(item) == "pending"


def test_pt0s_treated_as_no_duration():
    item = {"contentDetails": {"duration": "PT0S"},
            "processingDetails": {"processingStatus": "processing"}}
    assert classify_youtube_health(item) == "pending"


def test_missing_item():
    assert classify_youtube_health(None) == "missing"


def test_ok_even_if_processing_succeeded():
    item = {"contentDetails": {"duration": "PT54S"},
            "processingDetails": {"processingStatus": "succeeded"}}
    assert classify_youtube_health(item) == "ok"
