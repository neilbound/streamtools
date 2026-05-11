# streamtools — Project Context

> Running document. Update this as the project evolves.

---

## Vision

A local, GPU-accelerated video pipeline that takes raw Streamyard recordings and outputs
ready-to-upload Shorts with cleaned audio and burned-in karaoke captions — automatically.

**Why:** The current workflow spans Streamyard → CapCut, with manual handoffs at every step.
Streamyard's transcription is inaccurate; CapCut's caption styling is manual every time.
streamtools replaces both with a single browser-based UI that runs on your own machine.

**Show context:** Built around a podcast covering reality TV (Love is Blind etc.), hosted in Ohio.
Vertical video (1080×1920) exported natively from Streamyard via MARS (Multi-Aspect Ratio Streaming).

**Non-goals (for now):**
- Cloud hosting or multi-user access — runs locally only
- Full video editing (b-roll, graphics, music)
- Automatic publishing to social platforms

---

## Current Status

| Area | Status | Notes |
|---|---|---|
| Project scaffolding | ✅ Done | All files written |
| Streamlit UI | ✅ Done | 4-tab layout: Source → Enhance → Full Episode → Clips |
| Portrait compose | ✅ Working | Stack 2–4 landscape recordings → 1080×1920 via NVENC |
| Transcription (Deepgram Nova-3) | ✅ Working | Cloud API, ~10s/episode, word-level timestamps |
| Audio cleanup (DeepFilterNet3) | ✅ Working | 48kHz, GPU, speech-optimised denoiser |
| Clip selection (Claude API) | ✅ Working | Optional; claude-opus-4-6, JSON response |
| Karaoke captions (ASS) | ✅ Working | `{\k}` instant highlight, all-caps, 4 words/line |
| Full Episode export | ✅ Working | Clean MP4 + SRT, or fully captioned MP4 — independent of clips workflow |
| Show profiles | ✅ Working | Per-show producer context + caption style, persisted |
| Episode context | ✅ Working | Per-upload optional context, resets on new file |
| Profanity filter | ✅ Working | better-profanity text censor + 1kHz audio bleep |
| Description chyron overlay | ✅ Working | Full-width dark bar + white text via FFmpeg drawbox/drawtext |
| SRT export | ✅ Working | YouTube upload format |
| Bulk review table | ✅ Working | st.data_editor, inline editing, ZIP download |
| Python 3.12 venv | ✅ Done | `.venv312` in project root |
| Launch script | ✅ Done | `launch.bat` + desktop shortcut via `create_shortcut.ps1` |

---

## Workflow

```
Source tab      Option A: Upload a single Streamyard export (.mp4/.mov/.mkv)
                Option B: Compose — stack 2–4 local landscape recordings into
                          1080×1920 portrait (Fill/Fit mode, NVENC GPU encode)
                "Clear session" resets all state

Enhance tab     Audio Cleaning — DeepFilterNet3 → 48kHz mono WAV (independent, re-runnable)
                Transcription — Deepgram Nova-3 → word-level timestamps (~10s/episode)
                Optional: profanity filter bleeps audio + censors transcript text
                Episode context (per-upload notes for Claude)

Full Episode    Download clean MP4 + SRT for YouTube — independent of clips workflow
tab             Or download a fully captioned MP4 for social

Clips tab       Optional: "Suggest Clips with Claude" — Opus 4.6 analyses transcript
                Editable table: ✓ Approve | Title | Start(s) | End(s) | Description
                Export format: Social (burned-in captions) / YouTube / Both / SRT Only
                "Download All as ZIP" or individual download buttons
```

---

## Tech Stack

| Component | Tool | Notes |
|---|---|---|
| UI | Streamlit ≥1.40 | `layout="wide"`, local browser, 4-tab layout |
| Portrait compose | FFmpeg filter_complex + NVENC | vstack 2–4 clips, amix audio, h264_nvenc -cq 18 |
| Transcription | Deepgram Nova-3 (cloud API) | ~10s/episode, ~$0.19/45min, word-level timestamps |
| Audio cleanup | DeepFilterNet3 ≥0.5.6 | 48kHz, GPU/CPU, speech-optimised |
| Clip selection | Anthropic Claude (claude-opus-4-6) | Optional; JSON response |
| Captions | ASS subtitles | `{\k}` instant highlight, all-caps, 4 words/line, 0.3s pause break |
| Chyron overlay | FFmpeg drawbox + drawtext | Full-width dark bar, white text, bottom of frame |
| Video export | FFmpeg (subprocess) | libx264 + AAC, CRF 18, faststart, `-accurate_seek` |
| Review table | pandas + st.data_editor | Inline editing, dynamic rows |
| ZIP download | Python zipfile | Flat archive of all output files |
| Profanity filter | better-profanity | Text censor + audio bleep; `god` whitelisted |

**Hardware:** NVIDIA RTX 4070 Laptop — CUDA used by DeepFilterNet3; NVENC used by compose_portrait
**Python:** 3.12 (venv at `.venv312`) — required for PyTorch CUDA wheels

---

## Dependency Notes

### Installing the full stack

`requirements.txt` covers API and utility packages. PyTorch + DeepFilterNet3 must be installed separately:

```powershell
# 1. Install PyTorch with CUDA 12.4 wheels
& ".venv312\Scripts\python.exe" -m pip install "torch==2.6.0+cu124" "torchaudio==2.6.0+cu124" --index-url https://download.pytorch.org/whl/cu124

# 2. Install DeepFilterNet3
& ".venv312\Scripts\python.exe" -m pip install deepfilternet

# 3. Reinstall numpy — deepfilternet downgrades it; WhisperX (if used) needs >=2.1.0
& ".venv312\Scripts\python.exe" -m pip install "numpy>=2.1.0"

# 4. Install everything else
& ".venv312\Scripts\python.exe" -m pip install -r requirements.txt
```

### numpy conflict
DeepFilterNet3 declares `numpy<2.0` but works fine with numpy 2.x at runtime.
Always reinstall numpy 2.x after installing deepfilternet.

### Deepgram
Free tier covers ~775 hours before any payment is required.
`DEEPGRAM_API_KEY` must be set in `.env`.

---

## File Structure

```
streamtools/
  app.py                  Streamlit UI — 4-tab layout (Source/Enhance/Full Episode/Clips)
  config.py               Load/save show profiles + caption style to config.json
  config.json             Persisted profiles and style settings (gitignored)
  requirements.txt        Python dependencies (API + utils; PyTorch installed separately)
  launch.bat              Double-click launcher (activates venv, starts Streamlit)
  create_shortcut.ps1     One-time script to create desktop shortcut
  .streamlit/config.toml  Upload size limit (2GB)
  .env                    ANTHROPIC_API_KEY + DEEPGRAM_API_KEY (gitignored)
  .env.example            Placeholder — safe to commit
  .gitignore
  CLAUDE.md               Claude Code rules for this project
  PROJECT.md              This file
  pipeline/
    __init__.py
    transcribe.py         Deepgram Nova-3 API: word-level transcription (~10s/episode)
    audio_clean.py        DeepFilterNet3: 48kHz GPU speech enhancement
    filter.py             Profanity detection, audio bleep, transcript censor
    captions.py           ASS karaoke builder + SRT builder
    export.py             FFmpeg: compose_portrait, export_clip, export_clip_clean
    clip_finder.py        Claude Opus 4.6: suggest clips from transcript
```

---

## Show Profiles

Profiles stored in `config.json` under `"profiles"`. Each profile has:
- `producer_context` — injected as Claude system prompt for clip suggestions
- All caption style keys (font, size, colors, margin)

Switch profiles in the sidebar. **Episode context** (Step 1) is optional per-upload
text — who's being discussed, episode notes. Resets on new upload. Combined with
show producer context when calling Claude.

---

## Caption Style

**ASS color format:** `&HAABBGGRR` (BGR not RGB — alpha `00` = fully opaque)
- White text: `&H00FFFFFF`
- Warm yellow highlight (active word): `&H0000C8FF` (RGB 255,200,0)
- Pure yellow: `&H0000FFFF`

**Caption behaviour:** All-caps, `{\k}` instant-highlight, 4 words/line max,
natural break on 0.3s pause, `Outline=6` (thick black border), `Shadow=2`.
`margin_v` = px from bottom edge; `960` = vertical centre of 1920px frame.

---

## Running the App

```powershell
cd "C:\GitHub Repositories\streamtools"
& ".venv312\Scripts\python.exe" -m streamlit run app.py
```

Or double-click `launch.bat` / the desktop shortcut.

> `streamlit run app.py` alone will fail — must use the `.venv312` Python.

---

## Pipeline Module APIs

### `pipeline/transcribe.py`
```python
transcribe(audio_path: str) -> dict
# Returns: {"text": str, "words": [{"word": str, "start": float, "end": float}]}
# Deepgram Nova-3 API — ~10 seconds per episode. Requires DEEPGRAM_API_KEY in .env.
# Input: 48kHz WAV from clean_audio(). Words without timestamps are silently skipped.
```

### `pipeline/export.py`
```python
compose_portrait(video_paths, output_path, canvas_w=1080, canvas_h=1920, fill=True) -> str
# Stack 2–4 landscape recordings vertically into a single portrait video.
# fill=True: scale-to-fill + center crop (recommended for talking heads)
# fill=False: letterbox with black bars
# Encodes with h264_nvenc -preset p4 -cq 18 (GPU intermediate).
# Audio: amix all input tracks — each Streamyard local recording carries only that
# participant's audio, so mixing is required.
```

### `pipeline/audio_clean.py`
```python
clean_audio(video_path: str, output_path: str) -> str
# Extracts audio at 48kHz mono, runs DeepFilterNet3 enhancement, saves WAV.
# Model downloaded automatically on first run (~60MB).
```

### `pipeline/filter.py`
```python
censor_transcript(transcript: dict) -> tuple[dict, list[str]]
filter_profanity(audio_path: str, words: list, output_path: str) -> tuple[str, list[str]]
# "god" whitelisted; "goddamn" still censored.
```

### `pipeline/clip_finder.py`
```python
find_clips(transcript: dict, video_duration: float, producer_context: str = "") -> list[dict]
# producer_context = show context + "\n\nEpisode context: ..." if provided
# Returns [{title, start_time, end_time, reason, description}]
```

### `pipeline/captions.py`
```python
build_karaoke_ass(words, style, output_path, start_offset=0.0) -> str
build_srt(words, output_path, start_offset=0.0) -> str
```

### `pipeline/export.py`
```python
export_clip(video_path, clean_audio_path, ass_path, start, end, output_path, description="") -> str
export_clip_clean(video_path, clean_audio_path, start, end, output_path) -> str
get_video_duration(video_path: str) -> float
# subprocess + cwd=tempdir avoids Windows C: colon bug in FFmpeg filter strings.
# Font and ASS files copied to tempdir, referenced by filename only.
```

---

## Known Issues / Workarounds

### Windows-specific
- **FFmpeg PATH:** Hardcoded in `pipeline/export.py` and `pipeline/audio_clean.py` via
  `os.environ["PATH"]` injection — avoids `setx` 1024-char truncation bug.
- **FFmpeg filter path bug:** ASS and font files copied to `tempfile.gettempdir()`,
  referenced by filename only (`cwd=tmp_dir`). FFmpeg's filter parser splits on `:`
  so Windows drive-letter paths (`C:`) cannot appear in `-vf` filter strings.
- **`|` in chyron text:** Escaped as `\|` in drawtext filter string.
- **`'` in chyron text:** Replaced with Unicode `\u2019` to avoid terminating
  the drawtext single-quoted text value.

### Audio enhancement alternatives considered

| Option | Status | Notes |
|---|---|---|
| **DeepFilterNet3** | ✅ Current | Best practical option on Windows |
| **Facebook Denoiser** | Replaced | dns64, 16kHz — good but limited quality |
| **Resemble Enhance** | ❌ Windows incompatible | Best quality; blocked by `deepspeed` / `libaio` — see below |
| **noisereduce** | Not tried | Pure Python spectral subtraction; lower quality |

### Resemble Enhance — Why It Doesn't Work on Windows

`resemble-enhance` hard-depends on `deepspeed==0.12.4`. deepspeed:
- Has no precompiled Windows wheels
- Requires the full CUDA toolkit (`nvcc`), not just GPU drivers
- Requires `libaio` — a Linux-only system library

**To use Resemble Enhance in the future:**
1. Install WSL2 (Ubuntu)
2. Inside WSL: Python 3.12, CUDA toolkit, `pip install resemble-enhance`
3. Run the full pipeline inside WSL, or expose a subprocess/socket interface
   that the Windows Streamlit app calls into

---

## Integration Notes

### FFmpeg
Installed via winget. Bin path hardcoded in pipeline modules:
```
C:\Users\ntmas\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_...\ffmpeg-8.1-full_build\bin
```

### Anthropic API
- Key in `.env` → `ANTHROPIC_API_KEY`
- Used only in `pipeline/clip_finder.py` (optional step)
- Model: `claude-opus-4-6`

### Deepgram
- Key in `.env` → `DEEPGRAM_API_KEY`
- Model: `nova-3`, `smart_format=True`
- SDK: `deepgram-sdk>=3.0.0` — uses `client.listen.v1.media.transcribe_file()`
- Free tier: ~775 hours before any payment (~$0.19/45min after that)

### DeepFilterNet3
- Model downloaded automatically on first run (~60MB via `init_df()`)
- Runs at 48kHz — higher quality than old 16kHz Facebook Denoiser
- WhisperX resamples internally — no manual resampling needed
- GPU used automatically when available

### Montserrat Font
- Not installed by default on Windows — falls back to Arial
- Download: https://fonts.google.com/specimen/Montserrat
- Install to `C:\Windows\Fonts\` or `C:\Users\ntmas\AppData\Local\Microsoft\Windows\Fonts\`

---

## Future Work

- [ ] **MCP server** (`mcp_server.py`) — expose compose, clean, transcribe, find_clips, export as MCP tools so Claude Desktop Cowork can orchestrate the full pipeline autonomously or on a schedule
- [ ] **Transcript editor** — fix transcription errors before export
- [ ] **Batch upload** — queue multiple videos
- [ ] **Thumbnail generator** — still frame + text overlay for YouTube
- [ ] **Chyron UI controls** — font size and position currently hardcoded in `export.py`
- [ ] **Speaker diarization** — label who is speaking (Deepgram supports via `diarize=True`)
- [ ] **Resemble Enhance via WSL** — best audio quality; blocked on Windows by deepspeed/libaio
- [ ] **Caption presets** — save/load named styles beyond per-show profiles
