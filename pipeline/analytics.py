"""
pipeline/analytics.py — YouTube performance metrics for content optimization.

Pulls per-video stats (views/likes/comments via the Data API; retention, avg view
duration, subscribers gained, shares via the Analytics API), joins them to what we
control (which couple/segment, hook/title, clip length, posting weekday/hour), records
a daily snapshot time series, and produces leaderboards + group-by breakdowns.

Two metric tiers:
  - Tier 1 (Data API): views, likes, comments, duration — works with youtube/upload scopes.
  - Tier 2 (Analytics API): averageViewPercentage (retention — the key Shorts signal),
    averageViewDuration, subscribersGained, shares — needs the yt-analytics.readonly scope
    (one re-auth). fetch_video_analytics() degrades gracefully (returns {}) until then.

Caveats surfaced in report(): small N is directional only; compare views at the same age
(snapshots enable this); couple × posting-time confounds — describe, don't claim causation.
"""

import json
import os
import re
from datetime import datetime, timezone

from pipeline.publish import _youtube_service, _youtube_analytics_service
from pipeline.publish_queue import list_all

_ANALYTICS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "output", "analytics"
)
_SNAPSHOTS = os.path.join(_ANALYTICS_DIR, "snapshots.jsonl")

# Analytics API metrics we pull per video (Tier 2)
_ANALYTICS_METRICS = (
    "views,averageViewPercentage,averageViewDuration,"
    "estimatedMinutesWatched,subscribersGained,shares,likes,comments"
)


# ── Parsing / metadata ──────────────────────────────────────────────────────────

def _parse_iso_duration(s: str) -> int:
    """ISO-8601 duration (PT#H#M#S) -> seconds. Returns 0 for missing/zero (e.g. P0D)."""
    if not s:
        return 0
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    if not m:
        return 0
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + se


def _segment_of(clip_path: str) -> str:
    """Couple/topic slug from a clip filename (same parse as pipeline/archive.py).
    Shorts-season clips are `{segment}__{clip}_…`; standalone clips (no `__`) just get
    their output-file suffix stripped for a clean label."""
    fn = os.path.basename(clip_path)
    if "__" in fn:
        return fn.split("__")[0]
    for suf in ("_horizontal_youtube.mp4", "_youtube.mp4", "_social.mp4", ".mp4"):
        if fn.endswith(suf):
            return fn[:-len(suf)]
    return fn


def _kind_of(clip_path: str) -> str:
    sep = os.sep
    if (sep + "segments" + sep) in clip_path or "/segments/" in clip_path:
        return "segment"
    return "short"


def _duration_bucket(seconds: float) -> str:
    if not seconds:
        return "unknown"
    if seconds <= 45:
        return "<=45s"
    if seconds <= 55:
        return "46-55s"
    if seconds <= 60:
        return "56-60s"
    return ">60s"


def video_metadata() -> dict[str, dict]:
    """Map each posted YouTube video_id -> what we control about it (from the queue)."""
    meta: dict[str, dict] = {}
    for e in list_all():
        yt = e.get("results", {}).get("youtube", {})
        vid = yt.get("video_id")
        if yt.get("status") != "ok" or not vid:
            continue
        st = e.get("scheduled_time", "")
        weekday = hour = None
        try:
            dt = datetime.fromisoformat(st)
            weekday = dt.strftime("%a")
            hour = dt.hour
        except (TypeError, ValueError):
            pass
        cp = e.get("clip_path", "")
        meta[vid] = {
            "title": e.get("title", ""),
            "scheduled_time": st,
            "weekday": weekday,
            "hour": hour,
            "segment": _segment_of(cp),
            "kind": _kind_of(cp),
            "channel": e.get("channel", "neilbound"),
        }
    return meta


# ── Fetching ──────────────────────────────────────────────────────────────────

def fetch_video_stats(youtube, video_ids: list[str]) -> dict[str, dict]:
    """Data API cumulative stats per video (Tier 1). Batched 50 ids/call."""
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        r = youtube.videos().list(
            part="statistics,contentDetails,snippet", id=",".join(batch)
        ).execute()
        for it in r.get("items", []):
            stt = it.get("statistics", {})
            out[it["id"]] = {
                "views": int(stt.get("viewCount", 0)),
                "likes": int(stt.get("likeCount", 0)),
                "comments": int(stt.get("commentCount", 0)),
                "duration_sec": _parse_iso_duration(
                    it.get("contentDetails", {}).get("duration", "")),
                "published_at": it.get("snippet", {}).get("publishedAt", ""),
            }
    return out


def fetch_video_analytics(yta, video_ids: list[str], start: str, end: str) -> dict[str, dict]:
    """
    Analytics API per-video metrics (Tier 2). Returns {} if the scope isn't granted yet
    (403) so the caller can still snapshot Tier-1 stats. Keyed by video_id.
    """
    if not video_ids:
        return {}
    out: dict[str, dict] = {}
    try:
        for i in range(0, len(video_ids), 200):   # filter cap is generous; stay well under
            batch = video_ids[i:i + 200]
            resp = yta.reports().query(
                ids="channel==MINE",
                startDate=start, endDate=end,
                metrics=_ANALYTICS_METRICS,
                dimensions="video",
                filters="video==" + ",".join(batch),
                maxResults=len(batch),
            ).execute()
            headers = [h["name"] for h in resp.get("columnHeaders", [])]
            for row in resp.get("rows", []):
                rec = dict(zip(headers, row))
                vid = rec.get("video")
                if not vid:
                    continue
                out[vid] = {
                    "avg_view_pct": rec.get("averageViewPercentage"),
                    "avg_view_dur": rec.get("averageViewDuration"),
                    "est_minutes": rec.get("estimatedMinutesWatched"),
                    "subs_gained": rec.get("subscribersGained"),
                    "shares": rec.get("shares"),
                }
    except Exception as exc:
        print(f"[analytics] Analytics API unavailable (Tier 2 skipped): "
              f"{str(exc)[:100]}")
        return {}
    return out


# ── Snapshot time series ────────────────────────────────────────────────────────

def _load_snapshots() -> list[dict]:
    if not os.path.exists(_SNAPSHOTS):
        return []
    rows = []
    with open(_SNAPSHOTS, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def snapshot(channel: str = "ilb", force: bool = False) -> dict:
    """
    Record today's stats for every posted video, one row per video per day, appended to
    output/analytics/snapshots.jsonl. Tier-1 stats always; Tier-2 analytics when the scope
    is granted. Skips videos already snapshotted today unless force=True (manual refreshes
    force, so a re-pull after enabling Tier 2 captures it the same day; the newest row per
    video wins in latest_per_video). Returns a small summary.
    """
    meta = video_metadata()
    video_ids = list(meta.keys())
    if not video_ids:
        return {"snapshotted": 0, "note": "no posted videos"}

    today = datetime.now(timezone.utc).date().isoformat()
    already = {r["video_id"] for r in _load_snapshots() if r.get("date") == today}

    youtube = _youtube_service(channel)
    stats = fetch_video_stats(youtube, video_ids)

    # Analytics date range: from the earliest publish date through today.
    pubs = [s.get("published_at", "")[:10] for s in stats.values() if s.get("published_at")]
    start = min(pubs) if pubs else today
    try:
        yta = _youtube_analytics_service(channel)
        analytics = fetch_video_analytics(yta, video_ids, start, today)
    except Exception as exc:
        print(f"[analytics] Tier 2 client unavailable: {str(exc)[:100]}")
        analytics = {}

    os.makedirs(_ANALYTICS_DIR, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    written = 0
    with open(_SNAPSHOTS, "a", encoding="utf-8") as f:
        for vid in video_ids:
            if (vid in already and not force) or vid not in stats:
                continue
            s = stats[vid]
            a = analytics.get(vid, {})
            m = meta[vid]
            age_days = None
            if s.get("published_at"):
                try:
                    pub = datetime.fromisoformat(s["published_at"].replace("Z", "+00:00"))
                    age_days = round((datetime.now(timezone.utc) - pub).total_seconds() / 86400, 2)
                except ValueError:
                    pass
            rec = {
                "date": today, "ts": now_iso, "video_id": vid,
                "segment": m["segment"], "kind": m["kind"],
                "weekday": m["weekday"], "hour": m["hour"],
                "title": m["title"], "age_days": age_days,
                "views": s["views"], "likes": s["likes"], "comments": s["comments"],
                "duration_sec": s["duration_sec"],
                "avg_view_pct": a.get("avg_view_pct"),
                "avg_view_dur": a.get("avg_view_dur"),
                "subs_gained": a.get("subs_gained"),
                "shares": a.get("shares"),
            }
            f.write(json.dumps(rec) + "\n")
            written += 1
    return {"snapshotted": written, "tier2": bool(analytics), "date": today}


def latest_per_video() -> dict[str, dict]:
    """Most recent snapshot row per video_id."""
    latest: dict[str, dict] = {}
    for r in _load_snapshots():
        vid = r.get("video_id")
        if not vid:
            continue
        if vid not in latest or r.get("ts", "") > latest[vid].get("ts", ""):
            latest[vid] = r
    return latest


def views_at_age(video_id: str, target_age_days: float, tol: float = 1.0):
    """Views for a video at ~target age (for same-age comparison). None if no snapshot near it."""
    best = None
    for r in _load_snapshots():
        if r.get("video_id") != video_id or r.get("age_days") is None:
            continue
        if abs(r["age_days"] - target_age_days) <= tol:
            if best is None or abs(r["age_days"] - target_age_days) < abs(best["age_days"] - target_age_days):
                best = r
    return best["views"] if best else None


# ── Reporting ───────────────────────────────────────────────────────────────────

def _engagement_rate(rec: dict) -> float:
    v = rec.get("views") or 0
    if not v:
        return 0.0
    return round((rec.get("likes", 0) + rec.get("comments", 0)) / v * 100, 2)


def _group_means(rows: list[dict], key: str) -> list[dict]:
    groups: dict = {}
    for r in rows:
        g = r.get(key)
        if g is None:
            continue
        groups.setdefault(g, []).append(r)
    out = []
    for g, rs in groups.items():
        n = len(rs)
        views = [r["views"] for r in rs]
        rets = [r["avg_view_pct"] for r in rs if r.get("avg_view_pct") is not None]
        out.append({
            "group": g, "n": n,
            "avg_views": round(sum(views) / n, 1),
            "avg_retention_pct": round(sum(rets) / len(rets), 1) if rets else None,
            "avg_engagement_pct": round(sum(_engagement_rate(r) for r in rs) / n, 2),
        })
    return sorted(out, key=lambda x: (x["avg_retention_pct"] or 0, x["avg_views"]), reverse=True)


def report(shorts_only: bool = True) -> dict:
    """
    Build the optimization report from the latest snapshot per video joined with metadata.
    Returns a structured dict (the MCP tool formats it). Empty 'videos' means snapshot()
    hasn't run yet.
    """
    rows = list(latest_per_video().values())
    if shorts_only:
        rows = [r for r in rows if r.get("kind") == "short"]
    if not rows:
        return {"videos": 0, "note": "No snapshots yet — run snapshot() first."}

    has_tier2 = any(r.get("avg_view_pct") is not None for r in rows)
    for r in rows:
        r["engagement_pct"] = _engagement_rate(r)
        r["duration_bucket"] = _duration_bucket(r.get("duration_sec") or 0)

    by_views = sorted(rows, key=lambda r: r["views"], reverse=True)
    # Videos too new for the Analytics API to have processed return no retention —
    # exclude them from retention rankings/averages rather than treating as 0.
    by_retention = sorted([r for r in rows if r.get("avg_view_pct") is not None],
                          key=lambda r: r["avg_view_pct"], reverse=True)
    by_engagement = sorted(rows, key=lambda r: r["engagement_pct"], reverse=True)
    rets = [r["avg_view_pct"] for r in by_retention]

    def slim(r):
        return {
            "video_id": r["video_id"], "title": r.get("title", "")[:50],
            "segment": r.get("segment"), "age_days": r.get("age_days"),
            "views": r["views"],
            "retention_pct": round(r["avg_view_pct"], 1) if r.get("avg_view_pct") is not None else None,
            "engagement_pct": r["engagement_pct"], "duration_sec": r.get("duration_sec"),
        }

    return {
        "videos": len(rows),
        "tier2": has_tier2,
        "totals": {
            "views": sum(r["views"] for r in rows),
            "avg_views": round(sum(r["views"] for r in rows) / len(rows), 1),
            "avg_engagement_pct": round(sum(r["engagement_pct"] for r in rows) / len(rows), 2),
            "avg_retention_pct": round(sum(rets) / len(rets), 1) if rets else None,
            "retention_coverage": f"{len(rets)}/{len(rows)} videos have analytics data",
        },
        "top_by_views": [slim(r) for r in by_views[:5]],
        "bottom_by_views": [slim(r) for r in by_views[-5:]],
        "top_by_retention": [slim(r) for r in by_retention[:5]],
        "bottom_by_retention": [slim(r) for r in by_retention[-5:]],
        "top_by_engagement": [slim(r) for r in by_engagement[:5]],
        "by_segment": _group_means(rows, "segment"),
        "by_weekday": _group_means(rows, "weekday"),
        "by_hour": _group_means(rows, "hour"),
        "by_duration_bucket": _group_means(rows, "duration_bucket"),
    }
