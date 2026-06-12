"""Tests for pipeline/validate.py — pure parsers and probe-mocked checks."""

import wave

import pytest

from pipeline import validate as v


# ── Pure parsers ────────────────────────────────────────────────────────────────

BLACKDETECT_STDERR = """
[blackdetect @ 000001] black_start:0 black_end:2.1 black_duration:2.1
frame= 1500 fps=300
[blackdetect @ 000001] black_start:30.5 black_end:31.1 black_duration:0.6
"""

VOLUMEDETECT_STDERR = """
[Parsed_volumedetect_0 @ 000001] n_samples: 2822400
[Parsed_volumedetect_0 @ 000001] mean_volume: -23.4 dB
[Parsed_volumedetect_0 @ 000001] max_volume: -5.0 dB
"""


def test_parse_blackdetect():
    runs = v.parse_blackdetect(BLACKDETECT_STDERR)
    assert runs == [(0.0, 2.1), (30.5, 31.1)]


def test_parse_blackdetect_empty():
    assert v.parse_blackdetect("frame= 1500 fps=300") == []


def test_parse_volumedetect():
    vol = v.parse_volumedetect(VOLUMEDETECT_STDERR)
    assert vol == {"mean_volume": -23.4, "max_volume": -5.0}


def test_parse_volumedetect_empty():
    assert v.parse_volumedetect("no volume info here") == {}


def test_parse_decoded_duration():
    stderr = (
        "frame= 100 time=00:00:05.00 bitrate=N/A\n"
        "frame= 500 time=00:00:25.50 bitrate=N/A\n"
        "frame= 900 time=00:01:02.75 bitrate=N/A speed=30x\n"
    )
    assert v.parse_decoded_duration(stderr) == 62.75


def test_parse_decoded_duration_none():
    assert v.parse_decoded_duration("no progress lines") is None


# ── check_format ────────────────────────────────────────────────────────────────

def _probe(width=1080, height=1920, vcodec="h264", acodec="aac",
           duration="45.0", with_audio=True):
    streams = [{"codec_type": "video", "codec_name": vcodec,
                "width": width, "height": height}]
    if with_audio:
        streams.append({"codec_type": "audio", "codec_name": acodec})
    return {"streams": streams, "format": {"duration": duration}}


def test_check_format_good_clip():
    issues, warnings = v.check_format(_probe(), v.QA_PROFILES["clip"])
    assert issues == [] and warnings == []


def test_check_format_wrong_orientation_is_issue():
    issues, _ = v.check_format(_probe(width=1920, height=1080), v.QA_PROFILES["clip"])
    assert any("ASPECT" in i for i in issues)


def test_check_format_streamyard_720x1280_is_clean():
    # StreamYard MARS vertical exports at 720x1280 — normal, no warning noise
    issues, warnings = v.check_format(_probe(width=720, height=1280), v.QA_PROFILES["clip"])
    assert issues == [] and warnings == []


def test_check_format_below_quality_floor_warns():
    issues, warnings = v.check_format(_probe(width=480, height=854), v.QA_PROFILES["clip"])
    assert issues == []
    assert any("quality floor" in w for w in warnings)


def test_check_format_missing_audio():
    issues, _ = v.check_format(_probe(with_audio=False), v.QA_PROFILES["clip"])
    assert any("no audio stream" in i for i in issues)


def test_check_format_wrong_vcodec_is_issue():
    issues, _ = v.check_format(_probe(vcodec="hevc"), v.QA_PROFILES["clip"])
    assert any("CODEC: video" in i for i in issues)


def test_check_format_duration_over_target_warns():
    issues, warnings = v.check_format(_probe(duration="90.0"), v.QA_PROFILES["clip"])
    assert issues == []
    assert any("sweet spot" in w for w in warnings)


def test_check_format_duration_over_platform_cap_is_issue():
    issues, _ = v.check_format(_probe(duration="200.0"), v.QA_PROFILES["clip"])
    assert any("platform limit" in i for i in issues)


def test_check_format_zero_duration():
    issues, _ = v.check_format(_probe(duration="0"), v.QA_PROFILES["clip"])
    assert any("zero/unknown" in i for i in issues)


def test_check_format_episode_no_max_duration():
    probe = _probe(width=1920, height=1080, duration="4800.0")
    issues, warnings = v.check_format(probe, v.QA_PROFILES["episode"])
    assert issues == [] and warnings == []


# ── validate_media with mocked seams ────────────────────────────────────────────

def test_validate_media_missing_file(tmp_path):
    issues, _ = v.validate_media(str(tmp_path / "nope.mp4"))
    assert any("FILE: not found" in i for i in issues)


def test_validate_media_good(monkeypatch, tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x" * 200_000)
    monkeypatch.setattr(v, "probe_media", lambda p: _probe())
    monkeypatch.setattr(v, "run_signal_checks", lambda p, **kw: ([], []))
    issues, warnings = v.validate_media(str(f), profile="clip", deep=True)
    assert issues == [] and warnings == []


def test_validate_media_skips_signal_when_not_deep(monkeypatch, tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x" * 200_000)
    monkeypatch.setattr(v, "probe_media", lambda p: _probe())
    called = []
    monkeypatch.setattr(v, "run_signal_checks",
                        lambda p, **kw: called.append(p) or ([], []))
    v.validate_media(str(f), profile="clip", deep=False)
    assert called == []


def test_validate_media_signal_issues_propagate(monkeypatch, tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x" * 200_000)
    monkeypatch.setattr(v, "probe_media", lambda p: _probe())
    monkeypatch.setattr(v, "run_signal_checks",
                        lambda p, **kw: (["AUDIO: effectively silent (mean -60.0 dB)"], []))
    issues, _ = v.validate_media(str(f), profile="clip", deep=True)
    assert any("silent" in i for i in issues)


# ── quick_probe_check ───────────────────────────────────────────────────────────

def test_quick_probe_missing_file(tmp_path):
    assert "not found" in v.quick_probe_check(str(tmp_path / "nope.mp4"))


def test_quick_probe_good(monkeypatch, tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x" * 200_000)
    monkeypatch.setattr(v, "probe_media", lambda p: _probe())
    assert v.quick_probe_check(str(f), "portrait") is None


def test_quick_probe_wrong_orientation(monkeypatch, tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x" * 200_000)
    monkeypatch.setattr(v, "probe_media", lambda p: _probe(width=1920, height=1080))
    err = v.quick_probe_check(str(f), "portrait")
    assert err and "orientation" in err


# ── valid_intermediate ──────────────────────────────────────────────────────────

def test_valid_intermediate_wav_good(tmp_path):
    p = tmp_path / "audio.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x00\x00" * 48000 * 15)  # ~1.4 MB of frames
    assert v.valid_intermediate(str(p), "wav") is True


def test_valid_intermediate_wav_truncated(tmp_path):
    p = tmp_path / "audio.wav"
    p.write_bytes(b"RIFF" + b"\x00" * 2_000_000)  # big but not a valid wav
    assert v.valid_intermediate(str(p), "wav") is False


def test_valid_intermediate_wav_too_small(tmp_path):
    p = tmp_path / "audio.wav"
    p.write_bytes(b"RIFF tiny")
    assert v.valid_intermediate(str(p), "wav") is False


def test_valid_intermediate_transcript_good(tmp_path):
    p = tmp_path / "transcript.json"
    p.write_text('{"text": "hi there", "words": [{"word": "hi", "start": 0.0, "end": 0.4}]}')
    assert v.valid_intermediate(str(p), "transcript") is True


def test_valid_intermediate_transcript_empty_words(tmp_path):
    p = tmp_path / "transcript.json"
    p.write_text('{"text": "hi", "words": []}')
    assert v.valid_intermediate(str(p), "transcript") is False


def test_valid_intermediate_transcript_corrupt(tmp_path):
    p = tmp_path / "transcript.json"
    p.write_text('{"text": "hi", "words": [')
    assert v.valid_intermediate(str(p), "transcript") is False


def test_valid_intermediate_missing(tmp_path):
    assert v.valid_intermediate(str(tmp_path / "nope"), "video") is False


def test_valid_intermediate_unknown_kind(tmp_path):
    p = tmp_path / "x"
    p.write_text("data")
    with pytest.raises(ValueError):
        v.valid_intermediate(str(p), "bogus")
