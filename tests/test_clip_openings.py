"""Tests for clip opening filler-trim (pure logic, no network)."""
from pipeline.clip_finder import _trim_leading_filler


def _w(tokens, t0=0.0, step=0.4):
    return [{"word": tok, "start": round(t0 + i * step, 2),
             "end": round(t0 + i * step + step, 2)} for i, tok in enumerate(tokens)]


def test_trims_single_filler_opener():
    words = _w(["So", "the", "ratio", "of", "genuine", "people", "is", "wild", "and", "here", "now", "ok"])
    new = _trim_leading_filler(words, words[0]["start"], words[-1]["end"])
    assert new == words[1]["start"]   # past "So"


def test_trims_filler_phrase():
    words = _w(["By", "the", "way", "Andrew", "is", "thirty", "eight", "and", "she", "is", "young", "wow"])
    new = _trim_leading_filler(words, words[0]["start"], words[-1]["end"])
    assert new == words[3]["start"]   # past "by the way"


def test_strong_open_unchanged():
    words = _w(["Andrew", "is", "thirty", "eight", "Libby", "twenty", "two", "huge", "gap", "here", "wow", "yes"])
    new = _trim_leading_filler(words, words[0]["start"], words[-1]["end"])
    assert new == words[0]["start"]   # no trim


def test_too_short_clip_unchanged():
    words = _w(["So", "yeah", "okay"])
    new = _trim_leading_filler(words, words[0]["start"], words[-1]["end"])
    assert new == words[0]["start"]   # < 6 words: leave it


def test_never_trims_into_tail():
    # all filler-ish but the trim must stop, never consume the whole clip
    words = _w(["so", "well", "yeah", "okay", "like", "and", "but", "now", "look", "see", "real", "point"])
    new = _trim_leading_filler(words, words[0]["start"], words[-1]["end"])
    assert new < words[-1]["end"]
    assert new >= words[0]["start"]
