"""Tests for analytics parsing, metadata join, and reporting (pure logic, no network)."""
import os
import pipeline.analytics as an


def test_parse_iso_duration():
    assert an._parse_iso_duration("PT1M2S") == 62
    assert an._parse_iso_duration("PT54S") == 54
    assert an._parse_iso_duration("PT1H2M3S") == 3723
    assert an._parse_iso_duration("P0D") == 0
    assert an._parse_iso_duration("") == 0


def test_segment_and_kind():
    short = os.path.join("o", "ep", "clips", "overall_impressions__candy_land_social.mp4")
    assert an._segment_of(short) == "overall_impressions"
    assert an._kind_of(short) == "short"
    seg = os.path.join("o", "ep", "segments", "andrew_and_libby_horizontal_youtube.mp4")
    assert an._segment_of(seg) == "andrew_and_libby"
    assert an._kind_of(seg) == "segment"


def test_duration_bucket():
    assert an._duration_bucket(40) == "<=45s"
    assert an._duration_bucket(50) == "46-55s"
    assert an._duration_bucket(58) == "56-60s"
    assert an._duration_bucket(75) == ">60s"
    assert an._duration_bucket(0) == "unknown"


def test_video_metadata_joins_and_filters(monkeypatch):
    cp = os.path.join("o", "grp", "ep", "clips", "jorge_and_vanelle__celibacy_social.mp4")
    monkeypatch.setattr(an, "list_all", lambda: [
        {"clip_path": cp, "title": "T", "scheduled_time": "2026-06-19T22:00:00+00:00",
         "channel": "ilb", "results": {"youtube": {"status": "ok", "video_id": "VID1"}}},
        # not-ok youtube -> excluded
        {"clip_path": cp, "title": "X", "scheduled_time": "2026-06-20T22:00:00+00:00",
         "results": {"youtube": {"status": "error"}}},
    ])
    m = an.video_metadata()
    assert list(m) == ["VID1"]
    assert m["VID1"]["segment"] == "jorge_and_vanelle"
    assert m["VID1"]["kind"] == "short"
    assert m["VID1"]["hour"] == 22 and m["VID1"]["weekday"] == "Fri"


def test_engagement_rate():
    assert an._engagement_rate({"views": 100, "likes": 4, "comments": 1}) == 5.0
    assert an._engagement_rate({"views": 0, "likes": 4}) == 0.0


def _fake_snaps():
    return [
        {"video_id": "A", "kind": "short", "segment": "jorge", "weekday": "Fri", "hour": 22,
         "title": "a", "age_days": 2, "views": 900, "likes": 9, "comments": 0,
         "duration_sec": 40, "avg_view_pct": 70, "ts": "2026-06-19T00:00:00"},
        {"video_id": "B", "kind": "short", "segment": "andrew", "weekday": "Sat", "hour": 22,
         "title": "b", "age_days": 2, "views": 400, "likes": 2, "comments": 0,
         "duration_sec": 58, "avg_view_pct": 50, "ts": "2026-06-19T00:00:00"},
        {"video_id": "C", "kind": "segment", "segment": "andrew", "weekday": "Sat", "hour": 22,
         "title": "seg", "age_days": 2, "views": 50, "likes": 1, "comments": 0,
         "duration_sec": 600, "avg_view_pct": 30, "ts": "2026-06-19T00:00:00"},
    ]


def test_report_shorts_only_and_rankings(monkeypatch):
    monkeypatch.setattr(an, "_load_snapshots", _fake_snaps)
    rep = an.report(shorts_only=True)
    assert rep["videos"] == 2          # segment C excluded
    assert rep["tier2"] is True
    assert rep["top_by_views"][0]["video_id"] == "A"
    assert rep["top_by_retention"][0]["video_id"] == "A"
    assert {g["group"] for g in rep["by_segment"]} == {"jorge", "andrew"}
    # duration buckets computed
    assert {g["group"] for g in rep["by_duration_bucket"]} == {"<=45s", "56-60s"}


def test_report_empty(monkeypatch):
    monkeypatch.setattr(an, "_load_snapshots", lambda: [])
    assert an.report()["videos"] == 0


def test_latest_per_video_picks_newest_ts(monkeypatch):
    rows = [
        {"video_id": "A", "views": 100, "ts": "2026-06-18T00:00:00"},
        {"video_id": "A", "views": 250, "ts": "2026-06-19T00:00:00"},
    ]
    monkeypatch.setattr(an, "_load_snapshots", lambda: rows)
    assert an.latest_per_video()["A"]["views"] == 250
