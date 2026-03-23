"""
streamtools — Video Content Pipeline
Streamlit UI: Upload → Process → Review Clips → Export
"""

import os
import shutil
import tempfile

import streamlit as st
from dotenv import load_dotenv

import config
from pipeline.captions import build_karaoke_ass, build_srt
from pipeline.clip_finder import find_clips
from pipeline.export import export_clip, export_clip_clean, get_video_duration
from pipeline.filter import censor_transcript, filter_profanity
from pipeline.transcribe import transcribe
from pipeline.audio_clean import clean_audio

load_dotenv()

TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

st.set_page_config(page_title="streamtools", layout="wide")
st.title("streamtools")
st.caption("Upload → Transcribe → Find Clips → Export with karaoke captions")

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar — Producer context
# ─────────────────────────────────────────────────────────────────────────────
_cfg = config.load()
with st.sidebar:
    st.header("Show Context")
    st.caption("Paste your show/producer context here. Claude uses this when suggesting clips and writing descriptions.")
    producer_context = st.text_area(
        "Producer context",
        value=_cfg.get("producer_context", ""),
        height=220,
        label_visibility="collapsed",
        placeholder="e.g. You are a producer for Love is Blind Season 10. The cast includes Nick, Chelsea, AD, Clay... Focus on dramatic moments, relationship revelations, and fan-favourite interactions.",
    )
    if st.button("Save Context"):
        _cfg["producer_context"] = producer_context
        config.save(_cfg)
        st.success("Saved.")

# ─────────────────────────────────────────────────────────────────────────────
# Session state initialisation
# ─────────────────────────────────────────────────────────────────────────────
defaults = {
    "video_path": None,
    "clean_audio_path": None,
    "filtered_audio_path": None,
    "censored_words": [],
    "filter_enabled": False,
    "transcript": None,
    "video_duration": None,
    "clips": [],
    "approved": {},        # clip index → bool
    "start_times": {},
    "end_times": {},
    "descriptions": {},    # clip index → editable description string
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Upload
# ─────────────────────────────────────────────────────────────────────────────
st.header("1 · Upload Video")

uploaded = st.file_uploader(
    "Drag and drop your Streamyard recording here",
    type=["mp4", "mov", "mkv"],
)

if uploaded:
    dest = os.path.join(TEMP_DIR, uploaded.name)
    if st.session_state.video_path != dest:
        with open(dest, "wb") as f:
            shutil.copyfileobj(uploaded, f)
        st.session_state.video_path = dest
        # Reset downstream state when a new file is uploaded
        st.session_state.transcript = None
        st.session_state.clean_audio_path = None
        st.session_state.filtered_audio_path = None
        st.session_state.censored_words = []
        st.session_state.clips = []
        st.session_state.approved = {}
        st.session_state.start_times = {}
        st.session_state.end_times = {}
        st.session_state.descriptions = {}

    st.success(f"Loaded: **{uploaded.name}**")

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Transcribe + Clean Audio
# ─────────────────────────────────────────────────────────────────────────────
st.divider()
st.header("2 · Transcribe & Clean Audio")

if not st.session_state.video_path:
    st.info("Upload a video above to continue.")
else:
    filter_enabled = st.checkbox(
        "Filter profanity (bleep offensive words)",
        value=st.session_state.filter_enabled,
        key="filter_enabled",
    )

    if st.button("Process Video", type="primary"):
        video_path = st.session_state.video_path
        base_name = os.path.splitext(os.path.basename(video_path))[0]
        clean_path = os.path.join(TEMP_DIR, f"{base_name}_clean.wav")

        progress = st.progress(0, text="Starting…")

        with st.spinner("Transcribing with Whisper large-v3…"):
            progress.progress(10, text="Transcribing audio…")
            transcript = transcribe(video_path)
            st.session_state.transcript = transcript
            st.session_state.video_duration = get_video_duration(video_path)
            progress.progress(60, text="Transcription complete. Cleaning audio…")

        with st.spinner("Cleaning audio with Denoiser…"):
            clean_audio(video_path, clean_path)
            st.session_state.clean_audio_path = clean_path
            progress.progress(90, text="Audio cleaned. Filtering…" if filter_enabled else "Done!")

        if filter_enabled:
            with st.spinner("Filtering profanity…"):
                filtered_path = os.path.join(TEMP_DIR, f"{base_name}_filtered.wav")
                # Censor transcript text + caption words first
                censored_transcript, censored = censor_transcript(transcript)
                st.session_state.transcript = censored_transcript
                # Bleep audio using original word timestamps
                filter_profanity(clean_path, transcript["words"], filtered_path)
                st.session_state.filtered_audio_path = filtered_path
                st.session_state.censored_words = censored
            progress.progress(100, text="Done!")
            if censored:
                st.info(f"Censored {len(censored)} word(s): {', '.join(set(censored))}")
        else:
            st.session_state.filtered_audio_path = None
            st.session_state.censored_words = []
            progress.progress(100, text="Done!")

        st.success("Transcription and audio cleanup complete.")

    if st.session_state.transcript:
        st.subheader("Transcript Preview")
        st.text_area(
            label="Full transcript",
            value=st.session_state.transcript["text"],
            height=200,
            label_visibility="collapsed",
        )
        word_count = len(st.session_state.transcript["words"])
        dur = st.session_state.video_duration or 0
        st.caption(f"{word_count:,} words · {int(dur // 60)}m {int(dur % 60)}s")

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Find Clips
# ─────────────────────────────────────────────────────────────────────────────
st.divider()
st.header("3 · Find Clips (Optional)")
st.caption("Let Claude suggest clips automatically, or skip to Step 4 to define them manually.")

if not st.session_state.transcript:
    st.info("Complete Step 2 first.")
else:
    if st.button("Find Clips with Claude", type="primary"):
        with st.spinner("Asking Claude to find the best clips…"):
            clips = find_clips(
                st.session_state.transcript,
                st.session_state.video_duration,
                producer_context=config.load().get("producer_context", ""),
            )
            st.session_state.clips = clips
            st.session_state.approved = {i: True for i in range(len(clips))}
            st.session_state.start_times = {i: c["start_time"] for i, c in enumerate(clips)}
            st.session_state.end_times = {i: c["end_time"] for i, c in enumerate(clips)}
            st.session_state.descriptions = {i: c.get("description", "") for i, c in enumerate(clips)}

    if st.session_state.clips:
        st.subheader("Suggested Clips")
        st.caption("Check the clips you want to export. Adjust timestamps if needed.")

        for i, clip in enumerate(st.session_state.clips):
            with st.container(border=True):
                col_check, col_info = st.columns([0.05, 0.95])

                with col_check:
                    approved = st.checkbox(
                        "include",
                        value=st.session_state.approved.get(i, True),
                        key=f"approve_{i}",
                        label_visibility="collapsed",
                    )
                    st.session_state.approved[i] = approved

                with col_info:
                    st.markdown(f"**{clip['title']}**")
                    st.caption(clip["reason"])

                    t_col1, t_col2 = st.columns(2)
                    with t_col1:
                        start = st.number_input(
                            "Start (s)",
                            min_value=0.0,
                            max_value=float(st.session_state.video_duration or 9999),
                            value=float(st.session_state.start_times.get(i, clip["start_time"])),
                            step=0.5,
                            key=f"start_{i}",
                        )
                        st.session_state.start_times[i] = start

                    with t_col2:
                        end = st.number_input(
                            "End (s)",
                            min_value=0.0,
                            max_value=float(st.session_state.video_duration or 9999),
                            value=float(st.session_state.end_times.get(i, clip["end_time"])),
                            step=0.5,
                            key=f"end_{i}",
                        )
                        st.session_state.end_times[i] = end

                    clip_duration = end - start
                    st.caption(f"Duration: {clip_duration:.1f}s")

                    desc = st.text_area(
                        "Description (burned into video)",
                        value=st.session_state.descriptions.get(i, ""),
                        height=80,
                        key=f"desc_{i}",
                        help="Shown at the top of the video. Use \\n for a new line.",
                    )
                    st.session_state.descriptions[i] = desc

                    # Show transcript excerpt for this segment
                    excerpt_words = [
                        w["word"] for w in st.session_state.transcript["words"]
                        if start <= w["start"] <= end
                    ]
                    if excerpt_words:
                        st.markdown(
                            f"> *{' '.join(excerpt_words[:60])}{'…' if len(excerpt_words) > 60 else ''}*"
                        )

# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Export
# ─────────────────────────────────────────────────────────────────────────────
st.divider()
st.header("4 · Export")

if not st.session_state.transcript:
    st.info("Complete Step 2 first.")
else:
    # Manual clip adder
    with st.expander("Add clip manually"):
        m_title = st.text_input("Title", key="manual_title", placeholder="e.g. Key Insight")
        m_col1, m_col2 = st.columns(2)
        with m_col1:
            m_start = st.number_input("Start (s)", min_value=0.0,
                                      max_value=float(st.session_state.video_duration or 9999),
                                      value=0.0, step=0.5, key="manual_start")
        with m_col2:
            m_end = st.number_input("End (s)", min_value=0.0,
                                    max_value=float(st.session_state.video_duration or 9999),
                                    value=float(st.session_state.video_duration or 60.0),
                                    step=0.5, key="manual_end")
        if st.button("Add Clip"):
            if m_title and m_end > m_start:
                new_clip = {"title": m_title, "start_time": m_start, "end_time": m_end, "reason": "Manual"}
                st.session_state.clips.append(new_clip)
                i = len(st.session_state.clips) - 1
                st.session_state.approved[i] = True
                st.session_state.start_times[i] = m_start
                st.session_state.end_times[i] = m_end
                st.rerun()
            else:
                st.warning("Enter a title and make sure End > Start.")

    # Caption style settings
    st.subheader("Caption Style")
    style = config.load()

    col1, col2, col3 = st.columns(3)
    with col1:
        style["font_name"] = st.text_input("Font", value=style["font_name"])
        style["font_size"] = st.number_input("Size", min_value=8, max_value=48, value=int(style["font_size"]))
    with col2:
        style["primary_color"] = st.text_input(
            "Text color (ASS format)",
            value=style["primary_color"],
            help="ASS color: &HAABBGGRR. White = &H00FFFFFF",
        )
        style["highlight_color"] = st.text_input(
            "Highlight color",
            value=style["highlight_color"],
            help="Yellow = &H0000FFFF",
        )
    with col3:
        style["bold"] = st.checkbox("Bold", value=bool(style["bold"]))
        style["margin_v"] = st.number_input(
            "Bottom margin (px)",
            min_value=0,
            max_value=300,
            value=int(style["margin_v"]),
        )

    if st.button("Save Style"):
        config.save(style)
        st.success("Style saved.")

    st.divider()

    approved_indices = [i for i, v in st.session_state.approved.items() if v]

    if not approved_indices:
        st.warning("No clips selected. Check at least one clip above.")
    else:
        st.write(f"**{len(approved_indices)} clip(s) selected for export.**")

        export_format = st.radio(
            "Export format",
            ["Social (burned-in captions)", "YouTube (clean video + SRT)", "Both"],
            horizontal=True,
        )

        if st.button("Export Selected Clips", type="primary"):
            export_bar = st.progress(0, text="Exporting…")
            output_files = []

            audio_for_export = (
                st.session_state.filtered_audio_path or st.session_state.clean_audio_path
            )

            for step, i in enumerate(approved_indices):
                clip = st.session_state.clips[i]
                start = st.session_state.start_times[i]
                end = st.session_state.end_times[i]
                description = st.session_state.descriptions.get(i, "")
                safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in clip["title"])
                safe_title = safe_title.strip().replace(" ", "_")

                st.write(f"Exporting **{clip['title']}**…")

                clip_words = [
                    w for w in st.session_state.transcript["words"]
                    if start <= w["start"] <= end
                ]

                if export_format in ("Social (burned-in captions)", "Both"):
                    ass_path = os.path.join(TEMP_DIR, f"{safe_title}.ass")
                    build_karaoke_ass(clip_words, style, ass_path, start_offset=start)
                    social_path = os.path.join(OUTPUT_DIR, f"{safe_title}_social.mp4")
                    export_clip(
                        video_path=st.session_state.video_path,
                        clean_audio_path=audio_for_export,
                        ass_path=ass_path,
                        start=start,
                        end=end,
                        output_path=social_path,
                        description=description,
                    )
                    output_files.append((f"{clip['title']} (Social)", social_path, "video/mp4"))

                if export_format in ("YouTube (clean video + SRT)", "Both"):
                    yt_path = os.path.join(OUTPUT_DIR, f"{safe_title}_youtube.mp4")
                    export_clip_clean(
                        video_path=st.session_state.video_path,
                        clean_audio_path=audio_for_export,
                        start=start,
                        end=end,
                        output_path=yt_path,
                    )
                    srt_path = os.path.join(OUTPUT_DIR, f"{safe_title}.srt")
                    build_srt(clip_words, srt_path, start_offset=start)
                    output_files.append((f"{clip['title']} (YouTube)", yt_path, "video/mp4"))
                    output_files.append((f"{clip['title']} (Captions .srt)", srt_path, "text/plain"))

                export_bar.progress(
                    int((step + 1) / len(approved_indices) * 100),
                    text=f"Exported {step + 1}/{len(approved_indices)}",
                )

            st.success("All clips exported!")

            for title, path, mime in output_files:
                with open(path, "rb") as f:
                    st.download_button(
                        label=f"Download: {title}",
                        data=f,
                        file_name=os.path.basename(path),
                        mime=mime,
                    )
