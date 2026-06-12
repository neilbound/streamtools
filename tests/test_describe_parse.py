"""Tests for shorts-description output parsing and fallback — no API calls."""

import sys
import types

# describe.py imports anthropic at module level; stub it so tests don't need
# the package configured (the client is never constructed in these tests).
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))

from pipeline.describe import _parse_shorts_output, _empty_sections  # noqa: E402


FULL_OUTPUT = """YOUTUBE_SHORT:
Neil reacts to the celibacy conversation. Full ep on YouTube: link #Shorts #Podcast

TIKTOK:
He hid his kids until she was hooked. #datingshow #redflags #reactions

INSTAGRAM:
This conversation was not a conversation.
What would you have done? Comment below.
#dating #reality
Full episode on YouTube, link in bio."""


def test_parse_all_sections():
    out = _parse_shorts_output(FULL_OUTPUT)
    assert "Neil reacts" in out["youtube_short"]
    assert "hid his kids" in out["tiktok"]
    assert "link in bio" in out["instagram"]
    assert _empty_sections(out) == []


def test_parse_missing_tiktok():
    raw = """YOUTUBE_SHORT:
Some copy here.

INSTAGRAM:
IG copy here."""
    out = _parse_shorts_output(raw)
    assert out["tiktok"] == ""
    assert _empty_sections(out) == ["tiktok"]


def test_parse_no_markers_at_all():
    out = _parse_shorts_output("Here are three descriptions for your clip!")
    assert _empty_sections(out) == ["youtube_short", "tiktok", "instagram"]


def test_parse_strips_markdown():
    raw = """YOUTUBE_SHORT:
**Bold copy** here.

TIKTOK:
tk

INSTAGRAM:
ig"""
    out = _parse_shorts_output(raw)
    assert "**" not in out["youtube_short"]
    assert "Bold copy" in out["youtube_short"]


def test_empty_sections_whitespace_only_counts_as_empty():
    assert _empty_sections({"youtube_short": "  \n", "tiktok": "x", "instagram": "y"}) \
        == ["youtube_short"]
