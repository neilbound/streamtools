"""Turn a game-show result export into clip-production context.

The AI OBS game-show tool writes `output/gameshow/<title>_<date>.json` (schema 1):
title, winners, a per-round breakdown (question + each contestant's pick/rank/points),
and an `events` log with wall-clock timestamps. This module reads that and produces
**titles** and **per-round time boundaries** for the clip pipeline.

Supplementary only: clip finding stays transcript-driven (`clip_finder.py`). This adds
structure — who won overall, who took each category, and (given the recording's start
epoch) the offsets to segment the recording per round.
"""
import json

SUPPORTED_SCHEMA = 1


def load_export(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    schema = data.get("schema")
    if schema != SUPPORTED_SCHEMA:
        raise ValueError(f"unsupported game-show export schema {schema!r} "
                         f"(this reader handles {SUPPORTED_SCHEMA})")
    return data


def _winner_str(data: dict) -> str | None:
    names = [w["name"] for w in data.get("winners", [])]
    if not names:
        return None
    return names[0] if len(names) == 1 else " & ".join(names) + " (tie)"


def episode_title(data: dict) -> str:
    """Full-show title, e.g. 'Best Anime — Yuki wins'."""
    base = data.get("title") or "Game Show"
    win = _winner_str(data)
    return f"{base} — {win} wins" if win else base


def round_winner(data: dict, round_index: int) -> str | None:
    """Name of whoever placed 1st in a round (the category winner), or None."""
    for r in data.get("rounds", []):
        if r.get("index") == round_index:
            firsts = [x for x in r.get("ranks", []) if x.get("rank") == 1]
            return firsts[0]["name"] if firsts else None
    return None


def round_titles(data: dict) -> list[dict]:
    """Per-round clip titles: {round, question, title}."""
    base = data.get("title") or "Game Show"
    out = []
    for r in data.get("rounds", []):
        i = r.get("index", 0)
        q = r.get("question", "")
        win = round_winner(data, i)
        title = f"{base} — Round {i + 1}: {q}"
        if win:
            title += f" — {win} takes the category"
        out.append({"round": i, "question": q, "title": title})
    return out


def round_boundaries(data: dict, recording_start_epoch: float) -> list[dict]:
    """Per-round [start, end] offsets in recording seconds, from the event log.

    `recording_start_epoch` is when the recording began (e.g. from the OBS filename).
    Each round runs from when it became current until the next round started; the last
    round's end is None (open to end of recording).
    """
    events = data.get("events", [])
    starts: dict[int, float] = {}
    for e in events:
        if e.get("event") == "round" and "to" in e:
            starts.setdefault(e["to"], e["ts"])
    if 0 not in starts and events:
        starts[0] = events[0]["ts"]          # round 0 is the default (no explicit event)

    rounds = {r.get("index"): r for r in data.get("rounds", [])}
    ordered = sorted(starts.items())
    out = []
    for j, (ri, ts) in enumerate(ordered):
        end_ts = ordered[j + 1][1] if j + 1 < len(ordered) else None
        out.append({
            "round": ri,
            "question": rounds.get(ri, {}).get("question", ""),
            "start": round(ts - recording_start_epoch, 1),
            "end": round(end_ts - recording_start_epoch, 1) if end_ts is not None else None,
        })
    return out
