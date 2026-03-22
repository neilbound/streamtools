"""
Audio cleanup via DeepFilterNet.
Extracts the audio track from a video, applies noise/reverb suppression,
and saves the cleaned audio as a WAV file.
"""

import os
import subprocess
import tempfile

from df.enhance import enhance, init_df, load_audio, save_audio


def clean_audio(video_path: str, output_path: str) -> str:
    """
    Extract audio from video_path, run DeepFilterNet noise suppression,
    and save the result to output_path (.wav).

    Returns the output_path on success.
    """
    # Step 1: Extract audio from video to a temporary WAV using FFmpeg
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        raw_audio_path = tmp.name

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", video_path,
                "-vn",                  # no video
                "-ar", "48000",         # DeepFilterNet expects 48kHz
                "-ac", "1",             # mono
                "-f", "wav",
                raw_audio_path,
            ],
            check=True,
            capture_output=True,
        )

        # Step 2: Run DeepFilterNet
        model, df_state, _ = init_df()
        audio, _ = load_audio(raw_audio_path, sr=df_state.sr())
        enhanced = enhance(model, df_state, audio)
        save_audio(output_path, enhanced, df_state.sr())

    finally:
        if os.path.exists(raw_audio_path):
            os.remove(raw_audio_path)

    return output_path
