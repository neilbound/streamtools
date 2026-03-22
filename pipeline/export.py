"""
Video export via FFmpeg.
Cuts a clip from the source video, replaces the audio with the cleaned
version, burns in ASS karaoke captions, and outputs an mp4.
"""

import os
import ffmpeg


def export_clip(
    video_path: str,
    clean_audio_path: str,
    ass_path: str,
    start: float,
    end: float,
    output_path: str,
) -> str:
    """
    Cut a clip and export with cleaned audio and burned-in karaoke captions.

    Args:
        video_path:       source video file
        clean_audio_path: DeepFilterNet-cleaned WAV (full video length)
        ass_path:         ASS subtitle file (timestamps relative to clip start)
        start:            clip start time in seconds
        end:              clip end time in seconds
        output_path:      destination mp4 path

    Returns:
        output_path
    """
    duration = end - start

    # Escape the ASS path for FFmpeg's vf filter (backslashes and colons)
    ass_escaped = ass_path.replace("\\", "/").replace(":", "\\:")

    (
        ffmpeg
        .input(video_path, ss=start, t=duration)
        .output(
            ffmpeg.input(clean_audio_path, ss=start, t=duration).audio,
            output_path,
            vf=f"ass={ass_escaped}",
            vcodec="libx264",
            acodec="aac",
            audio_bitrate="192k",
            crf=18,
            preset="fast",
            movflags="+faststart",
        )
        .overwrite_output()
        .run(capture_stdout=True, capture_stderr=True)
    )

    return output_path


def get_video_duration(video_path: str) -> float:
    """Return the duration of a video file in seconds."""
    probe = ffmpeg.probe(video_path)
    return float(probe["format"]["duration"])
