"""Tests for _run_ffmpeg output verification (no real ffmpeg)."""

import subprocess
from types import SimpleNamespace

import pytest

from pipeline.export import _run_ffmpeg


def _fake_run(returncode=0, stderr=b""):
    def fake(cmd, capture_output=True, cwd=None):
        return SimpleNamespace(returncode=returncode, stderr=stderr, stdout=b"")
    return fake


def test_nonzero_returncode_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(returncode=1, stderr=b"boom"))
    with pytest.raises(RuntimeError, match="FFmpeg failed"):
        _run_ffmpeg(["ffmpeg"])


def test_no_expected_output_skips_verification(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run())
    _run_ffmpeg(["ffmpeg"])  # no raise


def test_missing_output_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", _fake_run())
    missing = str(tmp_path / "out.mp4")
    with pytest.raises(RuntimeError, match="produced no output file"):
        _run_ffmpeg(["ffmpeg"], expected_output=missing)


def test_tiny_output_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", _fake_run())
    out = tmp_path / "out.mp4"
    out.write_bytes(b"x" * 100)
    with pytest.raises(RuntimeError, match="bytes"):
        _run_ffmpeg(["ffmpeg"], expected_output=str(out))


def test_valid_output_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", _fake_run())
    out = tmp_path / "out.mp4"
    out.write_bytes(b"x" * 20_000)
    _run_ffmpeg(["ffmpeg"], expected_output=str(out))  # no raise


def test_probe_output_no_video_stream_raises(monkeypatch, tmp_path):
    import pipeline.export as export_mod
    monkeypatch.setattr(subprocess, "run", _fake_run())
    out = tmp_path / "out.mp4"
    out.write_bytes(b"x" * 20_000)
    monkeypatch.setattr(
        export_mod.ffmpeg, "probe",
        lambda p: {"streams": [{"codec_type": "audio"}]},
    )
    with pytest.raises(RuntimeError, match="no video stream"):
        _run_ffmpeg(["ffmpeg"], expected_output=str(out), probe_output=True)


def test_probe_output_with_video_stream_passes(monkeypatch, tmp_path):
    import pipeline.export as export_mod
    monkeypatch.setattr(subprocess, "run", _fake_run())
    out = tmp_path / "out.mp4"
    out.write_bytes(b"x" * 20_000)
    monkeypatch.setattr(
        export_mod.ffmpeg, "probe",
        lambda p: {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]},
    )
    _run_ffmpeg(["ffmpeg"], expected_output=str(out), probe_output=True)  # no raise
