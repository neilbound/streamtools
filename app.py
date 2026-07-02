"""
streamtools — Video Content Pipeline
Streamlit UI: Source → Enhance → Full Episode → Clips

DEPRECATED (2026-07): this UI predates the shorts-season pipeline, the opening
hook overlay, per-segment dual-orientation exports, and the per-show config
`pipeline` block — clips exported here BYPASS those features. Use the MCP tools
(process_shorts_season / process_broadcast_episode) or run_pipeline.py instead.
Kept for ad-hoc single-clip experiments only.
"""

import io
import json
import os
import shutil
import subprocess as _sp
import sys
import zipfile

# Windows consoles default to cp1252 — emoji/box-drawing chars in printed
# output have crashed real runs elsewhere in the toolchain.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import config
from pipeline.audio_clean import clean_audio
from pipeline.captions import build_karaoke_ass, build_srt
from pipeline.clip_finder import find_clips
from pipeline.export import (
    compose_portrait,
    export_clip,
    export_clip_clean,
    get_video_duration,
)
from pipeline.filter import censor_transcript, filter_profanity
from pipeline.transcribe import transcribe

load_dotenv(override=True)

TEMP_DIR   = os.path.join(os.path.dirname(__file__), "temp")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache")
for _d in (TEMP_DIR, OUTPUT_DIR, CACHE_DIR):
    os.makedirs(_d, exist_ok=True)

# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_meta_path(basename):
    return os.path.join(CACHE_DIR, f"{basename}.json")

def _cache_audio_path(basename):
    return os.path.join(CACHE_DIR, f"{basename}_clean.wav")

def _load_cache(basename):
    meta  = _cache_meta_path(basename)
    audio = _cache_audio_path(basename)
    if os.path.exists(meta) and os.path.exists(audio):
        with open(meta, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def _save_cache(basename, transcript, video_duration):
    with open(_cache_meta_path(basename), "w", encoding="utf-8") as f:
        json.dump({"transcript": transcript, "video_duration": video_duration}, f)

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(page_title="streamtools", layout="wide")

try:
    _branch = _sp.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=os.path.dirname(__file__), stderr=_sp.DEVNULL,
    ).decode().strip()
except Exception:
    _branch = "unknown"

st.title("streamtools")
_branch_badge = f"  `{_branch}`" if _branch != "master" else ""
st.caption(f"Source → Enhance → Full Episode → Clips{_branch_badge}")
st.warning(
    "**This UI is deprecated.** It predates the shorts-season pipeline, opening hook "
    "overlays, and per-segment exports — clips exported here bypass those features. "
    "Use the MCP tools (`process_shorts_season`) or `run_pipeline.py` for production work.",
    icon="⚠️",
)

# ── Sidebar — Show profiles + caption style ───────────────────────────────────

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
        placeholder="e.g. You are a producer for Love is Blind Season 11…",
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
    style = config.active_style(_cfg)
    with st.expander("Caption Style"):
        col1, col2 = st.columns(2)
        with col1:
            style["font_name"]  = st.text_input("Font",  value=style["font_name"])
            style["font_size"]  = st.number_input("Size", min_value=8, max_value=200, value=int(style["font_size"]))
            style["bold"]       = st.checkbox("Bold", value=bool(style["bold"]))
        with col2:
            style["primary_color"]   = st.text_input("Text color (ASS)",   value=style["primary_color"],   help="&HAABBGGRR — white = &H00FFFFFF")
            style["highlight_color"] = st.text_input("Highlight color",     value=style["highlight_color"], help="Warm yellow = &H0000C8FF")
            style["margin_v"]        = st.number_input("Vertical position (px)", min_value=0, max_value=1900, value=int(style["margin_v"]), help="960 = center of 1920px frame")
        if st.button("Save Style", use_container_width=True):
            _cfg["profiles"][selected].update(style)
            config.save(_cfg)
            st.success("Style saved.")

# ── Session state ─────────────────────────────────────────────────────────────

_CLIP_COLS = ["approved", "title", "start_time", "end_time", "description", "reason"]

defaults = {
    "video_path":            None,
    "compose_video_path":    None,
    "clean_audio_path":      None,
    "filtered_audio_path":   None,
    "censored_words":        [],
    "filter_enabled":        False,
    "transcript":            None,
    "video_duration":        None,
    "episode_context":       "",
    "clips":                 [],
    "output_files":          [],
    "cache_loaded":          False,
    "full_episode_mp4":      None,
    "full_episode_srt":      None,
    "full_episode_captioned": None,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_source, tab_enhance, tab_episode, tab_clips = st.tabs(
    ["🎬  Source", "⚙️  Enhance", "📥  Full Episode", "✂️  Clips"]
)

# ═════════════════════════════════════════════════════════════════════════════
# TAB 1 · SOURCE
# ═════════════════════════════════════════════════════════════════════════════

with tab_source:

    # ── Compose portrait ──────────────────────────────────────────────────────
    with st.expander("Compose Portrait from Local Recordings", expanded=False):
        st.caption(
            "If you recorded locally in Streamyard without a live stream, stack "
            "2–4 landscape recordings into a single 1080×1920 portrait video."
        )

        fit_mode = st.radio(
            "Fit mode",
            ["Fill (crop sides — recommended for talking heads)", "Fit (letterbox with black bars)"],
            horizontal=True,
            key="compose_fit",
        )
        fill = fit_mode.startswith("Fill")

        slot_labels = ["Top", "2nd", "3rd (optional)", "Bottom (optional)"]
        compose_slots = []
        for i, label in enumerate(slot_labels):
            f = st.file_uploader(label, type=["mp4", "mov", "mkv"], key=f"compose_slot_{i}")
            compose_slots.append(f)

        active_slots = [(i, f) for i, f in enumerate(compose_slots) if f is not None]

        if len(active_slots) >= 2:
            if st.button("Compose Portrait Video", type="primary", key="compose_run"):
                try:
                    saved_paths = []
                    with st.spinner(f"Composing {len(active_slots)} clips into 1080×1920…"):
                        for i, f in active_slots:
                            dest = os.path.join(TEMP_DIR, f"compose_{i}_{f.name}")
                            with open(dest, "wb") as fh:
                                shutil.copyfileobj(f, fh)
                            saved_paths.append(dest)
                        composed_path = os.path.join(TEMP_DIR, "composed_portrait.mp4")
                        compose_portrait(saved_paths, composed_path, fill=fill)

                    st.session_state.video_path         = composed_path
                    st.session_state.compose_video_path = composed_path
                    st.session_state.transcript         = None
                    st.session_state.clean_audio_path   = None
                    st.session_state.video_duration     = None
                    st.session_state.filtered_audio_path = None
                    st.session_state.censored_words     = []
                    st.session_state.clips              = []
                    st.session_state.output_files       = []
                    st.session_state.cache_loaded       = False
                    st.session_state.episode_context    = ""
                    st.session_state.full_episode_mp4   = None
                    st.session_state.full_episode_srt   = None
                    st.session_state.full_episode_captioned = None
                    st.success("Portrait video composed — go to Enhance to process it.")
                except Exception as e:
                    st.error(f"Compose failed: {e}")
        elif active_slots:
            st.info("Upload at least 2 videos to compose.")

    st.divider()

    # ── Direct upload ─────────────────────────────────────────────────────────
    st.subheader("Upload Video")

    uploaded = st.file_uploader(
        "Drag and drop your Streamyard recording here",
        type=["mp4", "mov", "mkv"],
        label_visibility="collapsed",
    )

    if uploaded:
        dest      = os.path.join(TEMP_DIR, uploaded.name)
        base_name = os.path.splitext(uploaded.name)[0]
        if st.session_state.video_path != dest:
            with open(dest, "wb") as f:
                shutil.copyfileobj(uploaded, f)
            st.session_state.video_path          = dest
            st.session_state.compose_video_path  = None
            st.session_state.filtered_audio_path = None
            st.session_state.censored_words      = []
            st.session_state.clips               = []
            st.session_state.output_files        = []
            st.session_state.episode_context     = ""
            st.session_state.full_episode_mp4    = None
            st.session_state.full_episode_srt    = None
            st.session_state.full_episode_captioned = None

            cached = _load_cache(base_name)
            if cached:
                st.session_state.transcript      = cached["transcript"]
                st.session_state.video_duration  = cached["video_duration"]
                st.session_state.clean_audio_path = _cache_audio_path(base_name)
                duration = float(cached["video_duration"] or 0)
                st.session_state.clips = [{
                    "approved": True, "title": base_name,
                    "start_time": 0.0, "end_time": duration,
                    "description": "", "reason": "_fullvideo",
                }]
                st.session_state.cache_loaded = True
            else:
                st.session_state.transcript      = None
                st.session_state.clean_audio_path = None
                st.session_state.video_duration  = None
                st.session_state.cache_loaded    = False

    # ── Current source status ─────────────────────────────────────────────────
    if st.session_state.video_path:
        fname = os.path.basename(st.session_state.video_path)
        dur   = st.session_state.video_duration
        dur_str = f" · {int(dur // 60)}m {int(dur % 60)}s" if dur else ""
        cache_note = " · loaded from cache" if st.session_state.cache_loaded else ""
        src_note = " · composed portrait" if st.session_state.compose_video_path else ""
        st.success(f"**{fname}**{dur_str}{src_note}{cache_note}")

        if st.button("Clear session", type="secondary"):
            for k, v in defaults.items():
                st.session_state[k] = v
            st.rerun()
    else:
        st.info("No video loaded. Upload a file above or compose one from local recordings.")

# ═════════════════════════════════════════════════════════════════════════════
# TAB 2 · ENHANCE
# ═════════════════════════════════════════════════════════════════════════════

with tab_enhance:
    if not st.session_state.video_path:
        st.info("Load a video in the Source tab first.")
    else:
        # ── Episode context ───────────────────────────────────────────────────
        st.session_state.episode_context = st.text_area(
            "Episode context (optional)",
            value=st.session_state.episode_context,
            height=80,
            placeholder="Who's being discussed? Any episode notes?",
            help="Used by Claude when suggesting clips. Resets when you load a new file.",
        )

        st.divider()

        col_audio, col_transcript = st.columns(2)

        # ── Audio cleaning ────────────────────────────────────────────────────
        with col_audio:
            st.subheader("Audio Cleaning")
            if st.session_state.clean_audio_path and os.path.exists(st.session_state.clean_audio_path):
                st.success("✅ Audio cleaned")
                st.caption(os.path.basename(st.session_state.clean_audio_path))
                btn_audio_label = "Re-clean Audio"
            else:
                st.warning("⬜ Not yet cleaned")
                btn_audio_label = "Clean Audio"

            if st.button(btn_audio_label, type="primary", key="btn_clean"):
                video_path = st.session_state.video_path
                base_name  = os.path.splitext(os.path.basename(video_path))[0]
                clean_path = _cache_audio_path(base_name)
                with st.spinner("Cleaning audio with DeepFilterNet3…"):
                    clean_audio(video_path, clean_path)
                    st.session_state.clean_audio_path = clean_path
                    st.session_state.video_duration   = get_video_duration(video_path)
                st.session_state.filtered_audio_path = None
                st.session_state.censored_words = []
                st.success("Audio cleaned.")
                st.rerun()

        # ── Transcription ─────────────────────────────────────────────────────
        with col_transcript:
            st.subheader("Transcription")
            if st.session_state.transcript:
                dur = st.session_state.video_duration or 0
                wc  = len(st.session_state.transcript["words"])
                st.success(f"✅ {wc:,} words · {int(dur // 60)}m {int(dur % 60)}s")
                btn_tx_label = "Re-transcribe"
            else:
                st.warning("⬜ Not yet transcribed")
                btn_tx_label = "Transcribe with Deepgram"

            filter_enabled = st.checkbox(
                "Filter profanity",
                value=st.session_state.filter_enabled,
                key="filter_enabled",
            )

            audio_ready = (
                st.session_state.clean_audio_path
                and os.path.exists(st.session_state.clean_audio_path)
            )
            if not audio_ready:
                st.caption("Clean audio first.")

            if st.button(btn_tx_label, type="primary", key="btn_transcribe", disabled=not audio_ready):
                audio_src  = st.session_state.clean_audio_path
                base_name  = os.path.splitext(os.path.basename(st.session_state.video_path))[0]
                with st.spinner("Transcribing with Deepgram Nova-3…"):
                    transcript = transcribe(audio_src)

                if filter_enabled:
                    with st.spinner("Filtering profanity…"):
                        filtered_path = os.path.join(TEMP_DIR, f"{base_name}_filtered.wav")
                        censored_tx, censored = censor_transcript(transcript)
                        st.session_state.transcript = censored_tx
                        filter_profanity(audio_src, transcript["words"], filtered_path)
                        st.session_state.filtered_audio_path = filtered_path
                        st.session_state.censored_words = censored
                    if censored:
                        st.info(f"Censored {len(censored)} word(s): {', '.join(set(censored))}")
                else:
                    st.session_state.transcript          = transcript
                    st.session_state.filtered_audio_path = None
                    st.session_state.censored_words      = []

                # Seed clips table with full-video row
                duration = float(st.session_state.video_duration or get_video_duration(st.session_state.video_path))
                st.session_state.video_duration = duration
                st.session_state.clips = [{
                    "approved": True, "title": base_name,
                    "start_time": 0.0, "end_time": duration,
                    "description": "", "reason": "_fullvideo",
                }]
                st.session_state.output_files        = []
                st.session_state.full_episode_mp4    = None
                st.session_state.full_episode_srt    = None
                st.session_state.full_episode_captioned = None

                _save_cache(base_name, st.session_state.transcript, duration)
                st.session_state.cache_loaded = True
                st.success("Transcription complete.")
                st.rerun()

        # ── Transcript display ────────────────────────────────────────────────
        if st.session_state.transcript:
            st.divider()
            st.text_area(
                "Transcript",
                value=st.session_state.transcript["text"],
                height=200,
                label_visibility="collapsed",
            )

# ═════════════════════════════════════════════════════════════════════════════
# TAB 3 · FULL EPISODE
# ═════════════════════════════════════════════════════════════════════════════

with tab_episode:
    if not st.session_state.transcript:
        st.info("Complete the Enhance tab (clean audio + transcribe) first.")
    else:
        audio_for_export = (
            st.session_state.filtered_audio_path or st.session_state.clean_audio_path
        )
        base_name = os.path.splitext(os.path.basename(st.session_state.video_path))[0]
        duration  = float(st.session_state.video_duration or 0)
        all_words = st.session_state.transcript["words"]

        # ── Clean episode (MP4 + SRT) ─────────────────────────────────────────
        st.subheader("Clean Episode")
        st.caption("Full episode with clean audio — no burned-in captions. Download the SRT separately for YouTube.")

        both_ready = (
            st.session_state.full_episode_mp4 and os.path.exists(st.session_state.full_episode_mp4)
            and st.session_state.full_episode_srt and os.path.exists(st.session_state.full_episode_srt)
        )

        if not both_ready:
            if st.button("Prepare Clean Episode + SRT", type="primary", key="btn_ep_clean"):
                mp4_path = os.path.join(OUTPUT_DIR, f"{base_name}_fullepisode.mp4")
                srt_path = os.path.join(OUTPUT_DIR, f"{base_name}_fullepisode.srt")
                with st.spinner("Exporting clean episode…"):
                    export_clip_clean(
                        st.session_state.video_path, audio_for_export,
                        0, duration, mp4_path,
                    )
                with st.spinner("Building SRT…"):
                    build_srt(all_words, srt_path, start_offset=0)
                st.session_state.full_episode_mp4 = mp4_path
                st.session_state.full_episode_srt = srt_path
                st.success("Ready to download.")
                st.rerun()
        else:
            col1, col2 = st.columns(2)
            with col1:
                with open(st.session_state.full_episode_mp4, "rb") as f:
                    st.download_button(
                        "⬇ Clean Episode MP4",
                        data=f,
                        file_name=os.path.basename(st.session_state.full_episode_mp4),
                        mime="video/mp4",
                        use_container_width=True,
                        type="primary",
                    )
            with col2:
                with open(st.session_state.full_episode_srt, "rb") as f:
                    st.download_button(
                        "⬇ SRT Captions",
                        data=f,
                        file_name=os.path.basename(st.session_state.full_episode_srt),
                        mime="text/plain",
                        use_container_width=True,
                    )
            if st.button("Re-prepare", key="btn_ep_clean_redo"):
                st.session_state.full_episode_mp4 = None
                st.session_state.full_episode_srt = None
                st.rerun()

        st.divider()

        # ── Captioned episode ─────────────────────────────────────────────────
        st.subheader("Captioned Episode")
        st.caption("Full episode with karaoke captions burned in (for social uploads).")

        cap_ready = (
            st.session_state.full_episode_captioned
            and os.path.exists(st.session_state.full_episode_captioned)
        )

        if not cap_ready:
            if st.button("Prepare Captioned Episode", type="primary", key="btn_ep_cap"):
                cap_path = os.path.join(OUTPUT_DIR, f"{base_name}_fullepisode_captioned.mp4")
                ass_path = os.path.join(TEMP_DIR,   f"{base_name}_fullepisode.ass")
                with st.spinner("Building captions…"):
                    build_karaoke_ass(all_words, style, ass_path, start_offset=0)
                with st.spinner("Exporting captioned episode (this may take a while for long videos)…"):
                    export_clip(
                        st.session_state.video_path, audio_for_export,
                        ass_path, 0, duration, cap_path,
                    )
                st.session_state.full_episode_captioned = cap_path
                st.success("Ready to download.")
                st.rerun()
        else:
            with open(st.session_state.full_episode_captioned, "rb") as f:
                st.download_button(
                    "⬇ Captioned Episode MP4",
                    data=f,
                    file_name=os.path.basename(st.session_state.full_episode_captioned),
                    mime="video/mp4",
                    use_container_width=True,
                    type="primary",
                )
            if st.button("Re-prepare", key="btn_ep_cap_redo"):
                st.session_state.full_episode_captioned = None
                st.rerun()

# ═════════════════════════════════════════════════════════════════════════════
# TAB 4 · CLIPS
# ═════════════════════════════════════════════════════════════════════════════

with tab_clips:
    if not st.session_state.transcript:
        st.info("Complete the Enhance tab (clean audio + transcribe) first.")
    else:
        audio_for_export = (
            st.session_state.filtered_audio_path or st.session_state.clean_audio_path
        )
        max_dur = float(st.session_state.video_duration or 9999)

        # ── AI clip suggestions ───────────────────────────────────────────────
        with st.expander("AI Clip Suggestions (optional)"):
            st.caption("Claude analyses the transcript and suggests the best clips with descriptions.")
            if st.button("Suggest Clips with Claude", type="primary"):
                with st.spinner("Asking Claude…"):
                    show_context  = config.active_context(config.load())
                    episode_ctx   = st.session_state.episode_context.strip()
                    full_context  = f"{show_context}\n\nEpisode context: {episode_ctx}".strip() if episode_ctx else show_context
                    suggestions   = find_clips(
                        st.session_state.transcript,
                        st.session_state.video_duration,
                        producer_context=full_context,
                    )
                    st.session_state.clips = [
                        {
                            "approved":    True,
                            "title":       c["title"],
                            "start_time":  float(c["start_time"]),
                            "end_time":    float(c["end_time"]),
                            "description": c.get("description", ""),
                            "reason":      c.get("reason", ""),
                        }
                        for c in suggestions
                    ]
                st.success(f"{len(suggestions)} clip(s) suggested.")

        # ── Clip table ────────────────────────────────────────────────────────
        st.subheader("Clips")
        st.caption("Edit inline. ✓ = include in export.")

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
                "end_time":    st.column_config.NumberColumn("End (s)",   min_value=0.0, max_value=max_dur, step=0.5, format="%.1f"),
                "description": st.column_config.TextColumn("Video Subtitle", width="large", help="Format: Cast Name | Show S#"),
                "reason":      None,
            },
            num_rows="dynamic",
            use_container_width=True,
        )
        st.session_state.clips = edited_df.to_dict("records")

        # ── Export ────────────────────────────────────────────────────────────
        approved_clips = [c for c in st.session_state.clips if c.get("approved")]

        if not approved_clips:
            st.warning("No clips selected. Check ✓ on at least one row.")
        else:
            st.write(f"**{len(approved_clips)} clip(s) ready to export.**")
            export_format = st.radio(
                "Export format",
                ["Social (burned-in captions)", "YouTube (clean video + SRT)", "Both", "SRT Only"],
                horizontal=True,
            )

            if st.button("Export Approved Clips", type="primary"):
                st.session_state.output_files = []
                export_bar  = st.progress(0, text="Exporting…")
                output_files = []

                for step, clip in enumerate(approved_clips):
                    start       = float(clip.get("start_time", 0))
                    end         = float(clip.get("end_time", 0))
                    description = str(clip.get("description", "") or "")
                    safe_title  = "".join(
                        c if c.isalnum() or c in " -_" else "_"
                        for c in str(clip.get("title", "clip"))
                    ).strip().replace(" ", "_") or f"clip_{step + 1}"

                    clip_words = [
                        w for w in st.session_state.transcript["words"]
                        if start <= w["start"] <= end
                    ]

                    if export_format in ("Social (burned-in captions)", "Both"):
                        ass_path    = os.path.join(TEMP_DIR,   f"{safe_title}.ass")
                        social_path = os.path.join(OUTPUT_DIR, f"{safe_title}_social.mp4")
                        build_karaoke_ass(clip_words, style, ass_path, start_offset=start)
                        export_clip(
                            video_path=st.session_state.video_path,
                            clean_audio_path=audio_for_export,
                            ass_path=ass_path,
                            start=start, end=end,
                            output_path=social_path,
                            description=description,
                        )
                        output_files.append((f"{clip['title']} (Social)", social_path, "video/mp4"))

                    if export_format in ("YouTube (clean video + SRT)", "Both"):
                        yt_path  = os.path.join(OUTPUT_DIR, f"{safe_title}_youtube.mp4")
                        srt_path = os.path.join(OUTPUT_DIR, f"{safe_title}.srt")
                        export_clip_clean(
                            video_path=st.session_state.video_path,
                            clean_audio_path=audio_for_export,
                            start=start, end=end, output_path=yt_path,
                        )
                        build_srt(clip_words, srt_path, start_offset=start)
                        output_files.append((f"{clip['title']} (YouTube)", yt_path, "video/mp4"))
                        output_files.append((f"{clip['title']} (SRT)",     srt_path, "text/plain"))

                    if export_format == "SRT Only":
                        srt_path = os.path.join(OUTPUT_DIR, f"{safe_title}.srt")
                        build_srt(clip_words, srt_path, start_offset=start)
                        output_files.append((f"{clip['title']} (SRT)", srt_path, "text/plain"))

                    export_bar.progress(
                        int((step + 1) / len(approved_clips) * 100),
                        text=f"Exported {step + 1}/{len(approved_clips)}",
                    )

                st.session_state.output_files = output_files
                st.success("All clips exported!")

        # ── Download ──────────────────────────────────────────────────────────
        if st.session_state.output_files:
            st.divider()
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for _, path, _ in st.session_state.output_files:
                    if os.path.exists(path):
                        zf.write(path, os.path.basename(path))
            zip_buf.seek(0)
            st.download_button(
                "⬇ Download All as ZIP",
                data=zip_buf,
                file_name="clips.zip",
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )
            st.caption("Or download individually:")
            for title, path, mime in st.session_state.output_files:
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        st.download_button(
                            label=f"⬇ {title}",
                            data=f,
                            file_name=os.path.basename(path),
                            mime=mime,
                            key=path,
                        )
