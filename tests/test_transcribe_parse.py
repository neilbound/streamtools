"""Tests for Deepgram response parsing — no httpx, no network."""

import pytest

from pipeline.transcribe import _parse_deepgram_response


def _good_payload():
    return {
        "results": {
            "channels": [{
                "alternatives": [{
                    "transcript": "hello there everyone",
                    "words": [
                        {"word": "hello", "start": 0.0, "end": 0.4},
                        {"word": "there", "start": 0.5, "end": 0.8},
                        {"word": "everyone", "start": 0.9, "end": 1.5},
                    ],
                }],
            }],
        }
    }


def test_good_payload():
    out = _parse_deepgram_response(_good_payload())
    assert out["text"] == "hello there everyone"
    assert len(out["words"]) == 3
    assert out["words"][0] == {"word": "hello", "start": 0.0, "end": 0.4}


def test_missing_channels():
    with pytest.raises(RuntimeError, match="unexpected response shape"):
        _parse_deepgram_response({"results": {"channels": []}})


def test_empty_alternatives():
    with pytest.raises(RuntimeError, match="unexpected response shape"):
        _parse_deepgram_response({"results": {"channels": [{"alternatives": []}]}})


def test_totally_wrong_shape():
    with pytest.raises(RuntimeError, match="unexpected response shape"):
        _parse_deepgram_response({"error": "quota exceeded"})


def test_empty_transcript_raises():
    payload = _good_payload()
    alt = payload["results"]["channels"][0]["alternatives"][0]
    alt["transcript"] = ""
    alt["words"] = []
    with pytest.raises(RuntimeError, match="empty transcript"):
        _parse_deepgram_response(payload)


def test_words_missing_timestamps_are_dropped():
    payload = _good_payload()
    alt = payload["results"]["channels"][0]["alternatives"][0]
    alt["words"].append({"word": "broken", "start": None, "end": None})
    out = _parse_deepgram_response(payload)
    assert len(out["words"]) == 3
