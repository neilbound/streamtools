"""
ASS subtitle builder with karaoke (word-by-word highlight) timing.

ASS karaoke uses {\\k<duration>} tags where duration is in centiseconds.
Each word is wrapped: {\\k<hold_time>}{\\kf<highlight_duration>}word
  - hold_time:        time before this word highlights (cs)
  - highlight_duration: time the word is highlighted (cs)

Color format: &HAABBGGRR (ASS is BGR not RGB, alpha 00 = fully opaque)
"""

import os


def _seconds_to_ass_time(seconds: float) -> str:
    """Convert float seconds to ASS time format H:MM:SS.cc"""
    cs = int(round(seconds * 100))
    h = cs // 360000
    cs %= 360000
    m = cs // 6000
    cs %= 6000
    s = cs // 100
    cs %= 100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_karaoke_ass(
    words: list[dict],
    style: dict,
    output_path: str,
    start_offset: float = 0.0,
) -> str:
    """
    Build an ASS subtitle file with karaoke word highlighting.

    Args:
        words:        list of {"word": str, "start": float, "end": float}
        style:        caption style dict from config.py
        output_path:  where to write the .ass file
        start_offset: subtract this from all timestamps (use clip start_time
                      so captions are relative to the clip, not the full video)

    Returns:
        output_path
    """
    font_name = style.get("font_name", "Arial")
    font_size = style.get("font_size", 18)
    primary = style.get("primary_color", "&H00FFFFFF")
    highlight = style.get("highlight_color", "&H0000FFFF")
    bold = -1 if style.get("bold", True) else 0
    margin_v = style.get("margin_v", 40)

    # ASS header
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 1

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,{font_name},{font_size},{primary},{highlight},&H00000000,&H80000000,{bold},0,0,0,100,100,0,0,1,2,1,2,20,20,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    # Group words into lines of ~8 words (or by natural pause > 0.5s)
    lines = _group_words_into_lines(words, max_words=8, pause_threshold=0.5)

    events = []
    for line_words in lines:
        if not line_words:
            continue

        line_start = line_words[0]["start"] - start_offset
        line_end = line_words[-1]["end"] - start_offset

        if line_start < 0:
            continue

        # Build karaoke text: {\\kf<cs>}word for each word
        karaoke_parts = []
        for w in line_words:
            duration_cs = max(1, int(round((w["end"] - w["start"]) * 100)))
            word_text = w["word"].strip()
            karaoke_parts.append(f"{{\\kf{duration_cs}}}{word_text}")

        text = " ".join(karaoke_parts)

        start_str = _seconds_to_ass_time(line_start)
        end_str = _seconds_to_ass_time(line_end)

        events.append(
            f"Dialogue: 0,{start_str},{end_str},Karaoke,,0,0,0,,{text}"
        )

    ass_content = header + "\n".join(events) + "\n"

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(ass_content)

    return output_path


def _group_words_into_lines(
    words: list[dict],
    max_words: int = 8,
    pause_threshold: float = 0.5,
) -> list[list[dict]]:
    """
    Split words into subtitle lines.
    Breaks on natural pauses (gap > pause_threshold) or max_words reached.
    """
    if not words:
        return []

    lines = []
    current_line = []

    for i, word in enumerate(words):
        current_line.append(word)

        is_last = i == len(words) - 1
        next_word = words[i + 1] if not is_last else None

        # Break conditions
        pause = (next_word["start"] - word["end"]) if next_word else 0
        hit_max = len(current_line) >= max_words
        natural_break = pause > pause_threshold

        if hit_max or natural_break or is_last:
            lines.append(current_line)
            current_line = []

    return lines
