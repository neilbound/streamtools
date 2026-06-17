"""Tests for the opening hook card (Pillow render + wrap). No ffmpeg/network."""
import os
from PIL import Image
from pipeline.export import _wrap_hook, render_hook_card


def test_wrap_uppercases_and_keeps_short_on_one_line():
    assert _wrap_hook("27-year age gap") == "27-YEAR AGE GAP"


def test_wrap_breaks_long_hook_into_lines():
    out = _wrap_hook("her son is older than her boyfriend")
    assert out == out.upper()
    assert 1 < len(out.splitlines()) <= 3
    assert all(len(line) <= 16 for line in out.splitlines())


def test_wrap_caps_at_three_lines():
    out = _wrap_hook("one two three four five six seven eight nine ten")
    assert len(out.splitlines()) <= 3


def test_render_hook_card_writes_valid_png(tmp_path):
    out = str(tmp_path / "card.png")
    w, h = render_hook_card("HER SON IS OLDER THAN HIM", out)
    assert os.path.exists(out)
    img = Image.open(out)
    assert img.mode == "RGBA"
    assert img.size == (w, h)
    assert w > 100 and h > 60


def test_render_hook_card_taller_for_more_lines(tmp_path):
    _, h1 = render_hook_card("SHORT", str(tmp_path / "a.png"))
    _, h3 = render_hook_card("one two three four five six seven eight", str(tmp_path / "b.png"))
    assert h3 > h1   # more wrapped lines -> taller card
