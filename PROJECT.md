# streamtools — Project Context

> Running document. Update this as the project evolves.

---

## Vision

A local, GPU-accelerated video pipeline that takes raw Streamyard recordings and outputs
ready-to-upload Shorts with cleaned audio and burned-in karaoke captions — automatically.

**Why:** The current workflow spans Streamyard (transcription + AI clips) → CapCut (audio
cleanup + captions), with manual handoffs at every step. Streamyard's transcription is
inaccurate; CapCut's caption styling is manual every time. This tool replaces both with a
single browser-based UI that runs on your own machine.

**Non-goals (for now):**
- Cloud hosting or multi-user access — runs locally only
- Full video editing (trimming b-roll, graphics, music)
- Automatic publishing to social platforms

---

## Current Status

| Area | Status | Notes |
|---|---|---|
| Project scaffolding | ✅ Done | All files written |
| Streamlit UI (4 steps) | ✅ Done | Upload → Process → Clips → Export |
| Transcription (faster-whisper) | ✅ Done | large-v3, CUDA, word timestamps |
| Audio cleanup (Resemble Enhance) | ✅ Code done | Awaiting Python 3.12 venv |
| Clip selection (Claude API) | ✅ Done | claude-sonnet-4-6, JSON response |
| Karaoke captions (ASS) | ✅ Done | Word-level `{\kf}` timing |
| FFmpeg export | ✅ Done | Cut + clean audio + burn captions |
| Profanity filter | ✅ Done | 1kHz beep via torchaudio; checkbox in Step 2 |
| Dependencies installed | ⚠️ Blocked | Python 3.14 incompatible with PyTorch |
| First end-to-end test | ⏳ Pending | |

### Blocked: Python 3.14 → PyTorch incompatibility

PyTorch (required by `faster-whisper` and `resemble-enhance`) does not yet publish
wheels for Python 3.14. The fix is to install **Python 3.12** alongside 3.14 and create
a new venv using it. Python 3.14 can remain the system default — they coexist fine.

**Steps to unblock:**
1. Download Python 3.12 from python.org (Windows 64-bit installer)
2. During install: uncheck "Add to PATH" to avoid conflicts
3. Create a new venv: `C:\Python312\python.exe -m venv .venv312`
4. Install: `.venv312\Scripts\pip install -r requirements.txt`
5. Point VSCode to `.venv312\Scripts\python.exe`
6. Launch: `.venv312\Scripts\streamlit run app.py`

---

## Workflow (once running)

```
1. Upload       Drop a .mp4 / .mov / .mkv Streamyard recording
2. Process      Whisper large-v3 transcribes (word timestamps)
                Resemble Enhance cleans audio (denoise + enhance)
3. Find Clips   Claude reads transcript, suggests 3–5 Shorts (45–90s each)
                Review each suggestion: approve/reject, tweak start/end times
4. Export       For each approved clip:
                  - ASS karaoke subtitle built from Whisper word timestamps
                  - FFmpeg cuts clip, muxes cleaned audio, burns captions
                  - Download button appears for each output .mp4
```

---

## Tech Stack

| Component | Tool | Version | Notes |
|---|---|---|---|
| UI | Streamlit | ≥1.40 | Local browser, `streamlit run app.py` |
| Transcription | faster-whisper | ≥1.0 | Whisper large-v3, CUDA float16 |
| Audio cleanup | resemble-enhance | latest | Local GPU, no API cost; first run downloads ~500MB model |
| Clip selection | Anthropic Claude | claude-sonnet-4-6 | API call, ~$0.001/video |
| Captions | ASS subtitles | — | `{\kf}` karaoke tags, configured once in config.json |
| Video export | FFmpeg + ffmpeg-python | ≥0.2 | libx264 + AAC, CRF 18, hardcoded captions |

**Hardware:** NVIDIA RTX 4070 — used by faster-whisper (CUDA float16) and Resemble Enhance (torch CUDA)

---

## File Structure

```
streamtools/
  app.py                  Streamlit UI — 4-step workflow
  config.py               Load/save caption style to config.json
  config.json             Persisted style settings (gitignored)
  requirements.txt        Python dependencies
  .env                    ANTHROPIC_API_KEY (gitignored)
  .env.example            Placeholder — safe to commit
  .gitignore
  PROJECT.md              This file
  pipeline/
    __init__.py
    transcribe.py         faster-whisper; returns {text, words[{word,start,end}]}
    audio_clean.py        Resemble Enhance; outputs cleaned .wav
    filter.py             Profanity filter; replaces flagged words with 1kHz beep
    clip_finder.py        Claude API; returns [{title,start_time,end_time,reason}]
    captions.py           Builds .ass file with {\kf} karaoke timing per word
    export.py             FFmpeg: cut + clean audio + burn captions → .mp4
  output/                 Exported clips land here (gitignored)
  temp/                   Intermediate files: raw video copy, cleaned WAV, .ass (gitignored)
```

---

## Configuration

### Caption style (`config.json`)

Saved via the "Save Style" button in Step 4. Persists across sessions.

```json
{
  "font_name": "Arial",
  "font_size": 18,
  "primary_color": "&H00FFFFFF",
  "highlight_color": "&H0000FFFF",
  "bold": true,
  "margin_v": 40
}
```

**ASS color format:** `&HAABBGGRR` (note: BGR not RGB, alpha 00 = fully opaque)
- White: `&H00FFFFFF`
- Yellow highlight: `&H0000FFFF`
- Black: `&H00000000`

The captions target a **1080×1920 canvas** (vertical Shorts format). `margin_v` controls
distance from the bottom edge in pixels.

### Environment (`.env`)

```
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Pipeline Module APIs

### `pipeline/transcribe.py`
```python
transcribe(video_path: str) -> dict
# Returns: {"text": str, "words": [{"word": str, "start": float, "end": float}]}
# Model loaded once and cached in memory for the session.
```

### `pipeline/audio_clean.py`
```python
clean_audio(video_path: str, output_path: str) -> str
# Extracts audio via FFmpeg, runs Resemble Enhance (denoise + enhance), saves WAV.
# Returns output_path. First call downloads model weights (~500MB).
```

### `pipeline/clip_finder.py`
```python
find_clips(transcript: dict, video_duration: float) -> list[dict]
# Sends transcript text to Claude. Returns [{title, start_time, end_time, reason}].
# Timestamps are clamped to [0, video_duration].
```

### `pipeline/captions.py`
```python
build_karaoke_ass(words: list[dict], style: dict, output_path: str, start_offset: float) -> str
# Groups words into lines (max 8 words, breaks on 0.5s pauses).
# Each word gets {\kf<centiseconds>} tag for karaoke highlighting.
# start_offset subtracts clip start so timestamps are relative to the clip.
```

### `pipeline/export.py`
```python
export_clip(video_path, clean_audio_path, ass_path, start, end, output_path) -> str
# FFmpeg: cuts video segment, replaces audio, burns ASS captions.
# Output: H.264 + AAC, CRF 18, preset fast, faststart flag for web.

get_video_duration(video_path: str) -> float
# Returns video duration in seconds via ffprobe.
```

---

## Known Issues / Future Work

### Near-term
- [ ] **Python 3.12 venv** — required to unblock PyTorch/faster-whisper/resemble-enhance
- [ ] **First end-to-end test** — run a real Streamyard video through the full pipeline
- [x] Update `app.py` spinner text from "DeepFilterNet" to "Resemble Enhance"

### Future features
- [ ] **Aspect ratio options** — 9:16 (Shorts), 16:9 (YouTube), 1:1 (Instagram)
- [ ] **Caption presets** — save/load multiple named styles (e.g. "Shorts Bold", "Subtle")
- [ ] **Batch processing** — queue multiple videos
- [ ] **Transcript editor** — fix transcription errors before exporting captions
- [ ] **Full video captions** — export the full video with clean captions (no karaoke scroll, for YouTube)
- [ ] **Screen share segments** — detect and handle screen-share portions differently
- [ ] **Speaker diarization** — label who is speaking in multi-person recordings

---

## Integration Notes

### FFmpeg
Must be installed system-wide and on PATH. Verify: `ffmpeg -version`
Install via winget: `winget install ffmpeg`

### Anthropic API
- Key stored in `.env` as `ANTHROPIC_API_KEY`
- Used only in `pipeline/clip_finder.py` (Step 3)
- Cost: ~$0.001 per video (transcript is ~3,000–5,000 tokens)
- Model: `claude-sonnet-4-6`

### Resemble Enhance
- Runs fully locally — no API key, no cost after install
- Downloads model weights on first run (~500MB, cached to `~/.cache`)
- Requires PyTorch with CUDA — needs Python ≤3.12 until PyTorch adds 3.14 support
- GitHub: https://github.com/resemble-ai/resemble-enhance

### faster-whisper
- Also requires PyTorch + CUDA
- Uses `large-v3` model (~1.5GB, downloaded on first run to `~/.cache/huggingface`)
- `compute_type="float16"` — optimal for RTX 4070 (12GB VRAM)
