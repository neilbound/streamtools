"""Tests for episode deliverable archiving (no network; uses temp dirs)."""
import os
import pytest
import pipeline.archive as arch


def _make_episode(tmp_path):
    ep = tmp_path / "output" / "grp" / "show_ep_2026-01-01"
    for sub in ("clips", "segments", "episode"):
        (ep / sub).mkdir(parents=True)
    (ep / "clips" / "a_social.mp4").write_bytes(b"x" * 100)
    (ep / "clips" / "a.srt").write_text("cap")
    (ep / "segments" / "seg_youtube.mp4").write_bytes(b"y" * 200)
    (ep / "episode" / "show_ep_youtube.mp4").write_bytes(b"z" * 300)
    # intermediates that must NOT be archived and must stay local
    (ep / "stitched.mp4").write_bytes(b"w" * 1000)
    (ep / "vertical_stitched.mp4").write_bytes(b"v" * 1000)
    (ep / "pipeline_status.json").write_text("{}")
    return ep


def test_deliverables_exclude_intermediates(tmp_path):
    ep = _make_episode(tmp_path)
    names = {os.path.basename(p) for p in arch.episode_deliverables(str(ep))}
    assert names == {"a_social.mp4", "a.srt", "seg_youtube.mp4", "show_ep_youtube.mp4"}
    assert "stitched.mp4" not in names and "pipeline_status.json" not in names


def test_archive_copies_verifies_deletes(tmp_path):
    ep = _make_episode(tmp_path)
    root = tmp_path / "drive"; root.mkdir()
    rep = arch.archive_episode(str(ep), archive_root=str(root))
    dest = root / "show_ep_2026-01-01"
    # deliverables landed in archive
    assert (dest / "clips" / "a_social.mp4").read_bytes() == b"x" * 100
    assert (dest / "segments" / "seg_youtube.mp4").exists()
    assert (dest / "episode" / "show_ep_youtube.mp4").exists()
    # local deliverables removed (empty subdirs cleaned)
    assert not (ep / "clips").exists()
    # intermediates remain local
    assert (ep / "stitched.mp4").exists()
    assert (ep / "pipeline_status.json").exists()
    # marker written, counts correct
    assert (ep / "ARCHIVED.json").exists()
    assert rep["copied"] == 4 and rep["deleted"] == 4


def test_dry_run_moves_nothing(tmp_path):
    ep = _make_episode(tmp_path)
    root = tmp_path / "drive"; root.mkdir()
    rep = arch.archive_episode(str(ep), archive_root=str(root), dry_run=True)
    assert (ep / "clips" / "a_social.mp4").exists()
    assert not (root / "show_ep_2026-01-01").exists()
    assert rep["dry_run"] and rep["files"] == 4


def test_missing_archive_root_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("STREAMTOOLS_ARCHIVE_ROOT", raising=False)
    ep = _make_episode(tmp_path)
    with pytest.raises(ValueError):
        arch.archive_episode(str(ep))


def test_find_archivable_requires_all_youtube_ok(tmp_path, monkeypatch):
    ep = _make_episode(tmp_path)
    cp = str(ep / "clips" / "a_social.mp4")
    monkeypatch.setattr(arch, "list_all",
                        lambda: [{"clip_path": cp, "results": {"youtube": {"status": "ok"}}}])
    assert str(ep) in arch.find_archivable_episodes()
    # a non-ok clip blocks the whole episode
    monkeypatch.setattr(arch, "list_all",
                        lambda: [{"clip_path": cp, "results": {"youtube": {"status": "error"}}}])
    assert str(ep) not in arch.find_archivable_episodes()


def test_find_archivable_skips_already_archived(tmp_path, monkeypatch):
    ep = _make_episode(tmp_path)
    (ep / "ARCHIVED.json").write_text("{}")
    cp = str(ep / "clips" / "a_social.mp4")
    monkeypatch.setattr(arch, "list_all",
                        lambda: [{"clip_path": cp, "results": {"youtube": {"status": "ok"}}}])
    assert str(ep) not in arch.find_archivable_episodes()
