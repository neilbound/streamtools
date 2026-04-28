"""
streamtools — Video Content Pipeline
Streamlit UI: Upload → Process → Review & Export
"""

import io
import json
import os
import shutil
import zipfile

import pandas as pd
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
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)


def _cache_meta_path(basename: str) -> str:
    return os.path.join(CACHE_DIR, f"{basename}.json")


def _cache_audio_path(basename: str) -> str:
    return os.path.join(CACHE_DIR, f"{basename}_clean.wav")


def _load_cache(basename: str) -> dict | None:
    meta = _cache_meta_path(basename)
    audio = _cache_audio_path(basename)
    if os.path.exists(meta) and os.path.exists(audio):
        with open(meta, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _save_cache(basename: str, transcript: dict, video_duration: float) -> None:
    with open(_cache_meta_path(basename), "w", encoding="utf-8") as f:
        json.dump({"transcript": transcript, "video_duration": video_duration}, f)

_CLIP_COLS = ["approved", "title", "start_time", "end_time", "description", "reason"]

st.set_page_config(page_title="streamtools", layout="wide")
import subprocess as _sp
try:
    _branch = _sp.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=os.path.dirname(__file__), stderr=_sp.DEVNULL).decode().strip()
except Exception:
    _branch = "unknown"

st.title("streamtools")
_branch_badge = f"  `{_branch}`" if _branch != "master" else ""
st.caption(f"Upload → Process → Review & Export with karaoke captions{_branch_badge}")

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar — Show profiles + Caption style
# ─────────────────────────────────────────────────────────────────────────────
_cfg = config.load()
with st.sidebar:
    st.header("Show Profiles")

    profile_names = list(_cfg["profiles"].keys())
    active = _cfg.get("active_profile", profile_names[0])
    if active not in profile_names:
        active = profile_names[0]

    selected = st.selectbox("Active show", profile_names, index=profile_names.index(active))
    _cfg["active_profile"] = selected

    st.caption("Claude uses this when suggesting clips and generating chyrons.")
    producer_context = st.text_area(
        "Show context",
        value=_cfg["profiles"][selected].get("producer_context", ""),
        height=150,
        placeholder="e.g. You are a producer for Love is Blind Season 11. Focus on dramatic moments and relationship reveals. Chyron format: [Names] | Love is Blind S11",
    )

    col_save, col_del = st.columns(2)
    with col_save:
        if st.button("Save", use_container_width=True):
            _cfg["profiles"][selected]["producer_context"] = producer_context
            config.save(_cfg)
            st.success("Saved.")
    with col_del:
        if st.button("Delete", use_container_width=True, type="secondary"):
            if len(_cfg["profiles"]) > 1:
                del _cfg["profiles"][selected]
                _cfg["active_profile"] = list(_cfg["profiles"].keys())[0]
                config.save(_cfg)
                st.rerun()
            else:
                st.warning("Can't delete the last profile.")

    st.divider()
    new_name = st.text_input("New show name", placeholder="e.g. The Bachelor S29")
    if st.button("Add Profile", use_container_width=True):
        name = new_name.strip()
        if name and name not in _cfg["profiles"]:
            _cfg["profiles"][name] = {"producer_context": "", **config.DEFAULT_STYLE}
            _cfg["active_profile"] = name
            config.save(_cfg)
            st.rerun()
        elif name in _cfg["profiles"]:
            st.warning("A profile with that name already exists.")

    st.divider()
    # ── Caption style (persistent, per show) ────────────────────────────────
    style = config.active_style(_cfg)
    with st.expander("Caption Style"):
        col1, col2 = st.columns(2)
        with col1:
            style["font_name"] = st.text_input("Font", value=style["font_name"])
            style["font_size"] = st.number_input("Size", min_value=8, max_value=200, value=int(style["font_size"]))
            style["bold"] = st.checkbox("Bold", value=bool(style["bold"]))
        with col2:
            style["primary_color"] = st.text_input(
                "Text color (ASS)", value=style["primary_color"],
                help="&HAABBGGRR — white = &H00FFFFFF",
            )
            style["highlight_color"] = st.text_input(
                "Highlight color", value=style["highlight_color"],
                help="Warm yellow = &H0000C8FF",
            )
            style["margin_v"] = st.number_input(
                "Vertical position (px)", min_value=0, max_value=1900, value=int(style["margin_v"]),
                help="960 = center of 1920px frame",
            )
        if st.button("Save Style", use_container_width=True):
            _cfg["profiles"][selected].update(style)
            config.save(_cfg)
            st.success("Style saved.")

# ─────────────────────────────────────────────────────────────────────────────
# Session state
# ─────────────────────────────────────────────────────────────────────────────
defaults = {
    "video_path": None,
    "clean_audio_path": None,
    "filtered_audio_path": None,
    "censored_words": [],
    "filter_enabled": False,
    "transcript": None,
    "video_duration": None,
    "episode_context": "",   # per-upload context; resets on new file
    "clips": [],             # list of dicts: approved, title, start_time, end_time, description, reason
    "output_files": [],      # list of (title, path, mime) after export
    "cache_loaded": False,   # True when transcript/audio were loaded from cache
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
    base_name = os.path.splitext(uploaded.name)[0]
    if st.session_state.video_path != dest:
        with open(dest, "wb") as f:
            shutil.copyfileobj(uploaded, f)
        st.session_state.video_path = dest
        st.session_state.filtered_audio_path = None
        st.session_state.censored_words = []
        st.session_state.clips = []
        st.session_state.output_files = []
        st.session_state.episode_context = ""

        cached = _load_cache(base_name)
        if cached:
            st.session_state.transcript = cached["transcript"]
            st.session_state.video_duration = cached["video_duration"]
            st.session_state.clean_audio_path = _cache_audio_path(base_name)
            duration = float(cached["video_duration"] or 0)
            st.session_state.clips = [{
                "approved": True,
                "title": base_name,
                "start_time": 0.0,
                "end_time": duration,
                "description": "",
                "reason": "_fullvideo",
            }]
            st.session_state.cache_loaded = True
        else:
            st.session_state.transcript = None
            st.session_state.clean_audio_path = None
            st.session_state.video_duration = None
            st.session_state.cache_loaded = False

    if st.session_state.cache_loaded:
        dur = st.session_state.video_duration or 0
        st.success(f"Loaded from cache: **{uploaded.name}** — {int(dur // 60)}m {int(dur % 60)}s · ready to export")
    else:
        st.success(f"Loaded: **{uploaded.name}**")

    st.session_state.episode_context = st.text_area(
        "Episode context (optional)",
        value=st.session_state.episode_context,
        height=80,
        placeholder="Who's being discussed? Any episode notes? e.g. 'This clip is about Jordan and Alexis during the wedding episode.'",
        help="Used by Claude when suggesting clips and can inform chyron descriptions. Resets when you upload a new file.",
    )

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Process
# ─────────────────────────────────────────────────────────────────────────────
st.divider()
st.header("2 · Process")

if not st.session_state.video_path:
    st.info("Upload a video above to continue.")
else:
    if st.session_state.cache_loaded:
        st.info("Transcript and clean audio loaded from cache. Jump straight to Step 3, or re-process below.")

    filter_enabled = st.checkbox(
        "Filter profanity (bleep offensive words)",
        value=st.session_state.filter_enabled,
        key="filter_enabled",
    )

    btn_label = "Re-process Video" if st.session_state.cache_loaded else "Process Video"
    if st.button(btn_label, type="primary"):
        video_path = st.session_state.video_path
        base_name = os.path.splitext(os.path.basename(video_path))[0]
        clean_path = _cache_audio_path(base_name)   # save directly to cache
        progress = st.progress(0, text="Starting…")

        with st.spinner("Cleaning audio with DeepFilterNet3…"):
            progress.progress(10, text="Cleaning audio…")
            clean_audio(video_path, clean_path)
            st.session_state.clean_audio_path = clean_path
            st.session_state.video_duration = get_video_duration(video_path)
            progress.progress(40, text="Audio cleaned. Transcribing…")

        with st.spinner("Transcribing with Whisper large-v3…"):
            transcript = transcribe(clean_path)
            st.session_state.transcript = transcript
            progress.progress(90, text="Transcription complete. Filtering…" if filter_enabled else "Saving cache…")

        if filter_enabled:
            with st.spinner("Filtering profanity…"):
                filtered_path = os.path.join(TEMP_DIR, f"{base_name}_filtered.wav")
                censored_transcript, censored = censor_transcript(transcript)
                st.session_state.transcript = censored_transcript
                filter_profanity(clean_path, transcript["words"], filtered_path)
                st.session_state.filtered_audio_path = filtered_path
                st.session_state.censored_words = censored
            if censored:
                st.info(f"Censored {len(censored)} word(s): {', '.join(set(censored))}")
        else:
            st.session_state.filtered_audio_path = None
            st.session_state.censored_words = []

        # Save transcript + duration to cache (audio already written to cache dir)
        _save_cache(base_name, st.session_state.transcript, st.session_state.video_duration)
        st.session_state.cache_loaded = True

        # Auto-populate a full-video row so the table is ready immediately
        duration = float(st.session_state.video_duration or 0)
        st.session_state.clips = [{
            "approved": True,
            "title": base_name,
            "start_time": 0.0,
            "end_time": duration,
            "description": "",
            "reason": "_fullvideo",
        }]
        st.session_state.output_files = []
        progress.progress(100, text="Done!")
        st.success("Processing complete — results cached.")

    if st.session_state.transcript:
        st.text_area(
            label="Transcript",
            value=st.session_state.transcript["text"],
            height=160,
            label_visibility="collapsed",
        )
        word_count = len(st.session_state.transcript["words"])
        dur = st.session_state.video_duration or 0
        st.caption(f"{word_count:,} words · {int(dur // 60)}m {int(dur % 60)}s")

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Review & Export
# ─────────────────────────────────────────────────────────────────────────────
st.divider()
st.header("3 · Review & Export")

if not st.session_state.transcript:
    st.info("Complete Step 2 first.")
else:
    # ── Optional: AI clip suggestions ──────────────────────────────────────
    with st.expander("AI Clip Suggestions (optional)"):
        st.caption("Claude analyses the transcript and suggests the best clips with descriptions.")
        if st.button("Suggest Clips with Claude", type="primary"):
            with st.spinner("Asking Claude…"):
                show_context = config.active_context(config.load())
                episode_ctx = st.session_state.episode_context.strip()
                full_context = show_context
                if episode_ctx:
                    full_context = f"{show_context}\n\nEpisode context: {episode_ctx}".strip()
                suggestions = find_clips(
                    st.session_state.transcript,
                    st.session_state.video_duration,
                    producer_context=full_context,
                )
                st.session_state.clips = [
                    {
                        "approved": True,
                        "title": c["title"],
                        "start_time": float(c["start_time"]),
                        "end_time": float(c["end_time"]),
                        "description": c.get("description", ""),
                        "reason": c.get("reason", ""),
                    }
                    for c in suggestions
                ]
            st.success(f"{len(suggestions)} clip(s) suggested — review the table below.")

    # ── Editable review table ───────────────────────────────────────────────
    st.subheader("Clips")
    st.caption("Edit titles, timestamps, and descriptions inline. ✓ = include in export. Use the bottom row to add new clips.")

    max_dur = float(st.session_state.video_duration or 9999)

    if st.session_state.clips:
        df = pd.DataFrame(st.session_state.clips)
        for col in _CLIP_COLS:
            if col not in df.columns:
                df[col] = "" if col in ("title", "description", "reason") else (True if col == "approved" else 0.0)
    else:
        df = pd.DataFrame(columns=_CLIP_COLS).astype({
            "approved": bool, "title": str, "start_time": float,
            "end_time": float, "description": str, "reason": str,
        })

    edited_df = st.data_editor(
        df,
        column_config={
            "approved":    st.column_config.CheckboxColumn("✓", default=True, width="small"),
            "title":       st.column_config.TextColumn("Title", width="medium"),
            "start_time":  st.column_config.NumberColumn("Start (s)", min_value=0.0, max_value=max_dur, step=0.5, format="%.1f"),
            "end_time":    st.column_config.NumberColumn("End (s)", min_value=0.0, max_value=max_dur, step=0.5, format="%.1f"),
            "description": st.column_config.TextColumn("Video Subtitle", width="large", help="Format: Cast Name (& Cast Name) | Show Being Discussed S#"),
            "reason":      None,
        },
        num_rows="dynamic",
        use_container_width=True,
    )
    st.session_state.clips = edited_df.to_dict("records")

    # ── Export ──────────────────────────────────────────────────────────────
    approved_clips = [c for c in st.session_state.clips if c.get("approved")]

    if not approved_clips:
        st.warning("No clips selected. Check ✓ on at least one row above.")
    else:
        st.write(f"**{len(approved_clips)} clip(s) ready to export.**")
        export_format = st.radio(
            "Export format",
            ["Social (burned-in captions)", "YouTube (clean video + SRT)", "Both", "SRT Only"],
            horizontal=True,
        )

        if st.button("Export Approved Clips", type="primary"):
            st.session_state.output_files = []
            export_bar = st.progress(0, text="Exporting…")
            output_files = []
            audio_for_export = (
                st.session_state.filtered_audio_path or st.session_state.clean_audio_path
            )

            for step, clip in enumerate(approved_clips):
                start = float(clip.get("start_time", 0))
                end = float(clip.get("end_time", 0))
                description = str(clip.get("description", "") or "")
                safe_title = "".join(
                    c if c.isalnum() or c in " -_" else "_"
                    for c in str(clip.get("title", "clip"))
                ).strip().replace(" ", "_") or f"clip_{step + 1}"

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

                if export_format == "SRT Only":
                    srt_path = os.path.join(OUTPUT_DIR, f"{safe_title}.srt")
                    build_srt(clip_words, srt_path, start_offset=start)
                    output_files.append((f"{clip['title']} (Captions .srt)", srt_path, "text/plain"))

                export_bar.progress(
                    int((step + 1) / len(approved_clips) * 100),
                    text=f"Exported {step + 1}/{len(approved_clips)}",
                )

            st.session_state.output_files = output_files
            st.success("All clips exported!")

# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Download
# ─────────────────────────────────────────────────────────────────────────────
if st.session_state.output_files:
    st.divider()
    st.header("4 · Download")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for _, path, _ in st.session_state.output_files:
            if os.path.exists(path):
                zf.write(path, os.path.basename(path))
    zip_buf.seek(0)

    st.download_button(
        "Download All as ZIP",
        data=zip_buf,
        file_name="clips.zip",
        mime="application/zip",
        type="primary",
    )

    st.caption("Or download individually:")
    for title, path, mime in st.session_state.output_files:
        if os.path.exists(path):
            with open(path, "rb") as f:
                st.download_button(
                    label=f"Download: {title}",
                    data=f,
                    file_name=os.path.basename(path),
                    mime=mime,
                    key=path,
                )
