"""Game-show export reader — pure, no network."""
import json

import pytest

from pipeline import gameshow


def _export():
    return {
        "schema": 1,
        "title": "Best Anime",
        "date": "2026-07-10",
        "moderator": "Neil",
        "winners": [{"id": 3, "name": "Yuki", "points": 5}],
        "tie": False,
        "board": [],
        "rounds": [
            {"index": 0, "question": "Best Protagonist",
             "ranks": [{"id": 0, "name": "Kenji", "pick": "FMA", "rank": 1, "points": 4},
                       {"id": 3, "name": "Yuki", "pick": "Frieren", "rank": 4, "points": 1}]},
            {"index": 1, "question": "Best Villain",
             "ranks": [{"id": 3, "name": "Yuki", "pick": "Frieren", "rank": 1, "points": 4}]},
        ],
        "events": [
            {"event": "phase", "round": 0, "ts": 1000.0, "phase": "play"},
            {"event": "round", "round": 1, "ts": 1120.0, "to": 1},
        ],
    }


def test_load_export_rejects_unknown_schema(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"schema": 99}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        gameshow.load_export(str(p))


def test_titles():
    d = _export()
    assert gameshow.episode_title(d) == "Best Anime — Yuki wins"
    titles = gameshow.round_titles(d)
    assert titles[0]["title"] == "Best Anime — Round 1: Best Protagonist — Kenji takes the category"
    assert titles[1]["title"] == "Best Anime — Round 2: Best Villain — Yuki takes the category"


def test_episode_title_tie():
    d = _export()
    d["winners"] = [{"id": 0, "name": "Kenji", "points": 5}, {"id": 3, "name": "Yuki", "points": 5}]
    assert gameshow.episode_title(d) == "Best Anime — Kenji & Yuki (tie) wins"


def test_round_boundaries_offsets():
    d = _export()
    # recording started 30s before the first logged event
    b = gameshow.round_boundaries(d, recording_start_epoch=970.0)
    assert b[0] == {"round": 0, "question": "Best Protagonist", "start": 30.0, "end": 150.0}
    assert b[1] == {"round": 1, "question": "Best Villain", "start": 150.0, "end": None}
