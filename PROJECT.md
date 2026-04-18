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
| Streamlit UI | ✅ Done | Upload → Process → Review & Export → Download |
| Transcription (WhisperX) | ✅ Working | large-v3 + wav2vec2 phoneme alignment, CUDA |
| Audio cleanup (DeepFilterNet3) | ✅ Working | 48kHz, GPU, speech-optimised denoiser |
| Clip selection (Claude API) | ✅ Working | Optional; claude-sonnet-4-6, JSON response |
| Karaoke captions (ASS) | ✅ Working | `{\k}` instant highlight, all-caps, 4 words/line |
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
1. Upload       Drop a .mp4 / .mov / .mkv Streamyard recording (1080×1920 portrait)
                Optional: fill in episode context (who's being discussed, episode notes)

2. Process      DeepFilterNet3 cleans audio → 48kHz mono WAV
                WhisperX transcribes (large-v3) then phoneme-aligns (wav2vec2)
                Optional: profanity filter bleeps audio + censors transcript text

3. Review       Editable table: ✓ Approve | Title | Start(s) | End(s) | Description
  & Export      Optional: "Suggest Clips with Claude" populates table via AI
                Caption style per show (sidebar) — font, size, colors, margin
                Export format: Social / YouTube / Both
                "Export Approved Clips" → progress bar

4. Download     "Download All as ZIP" (flat zip, all files)
                Individual download buttons per file
```

---

## Tech Stack

| Component | Tool | Notes |
|---|---|---|
| UI | Streamlit ≥1.40 | `layout="wide"`, local browser |
| Transcription | WhisperX ≥3.8 | large-v3 + wav2vec2 forced alignment; much more accurate word timestamps than plain Whisper |
| Audio cleanup | DeepFilterNet3 ≥0.5.6 | 48kHz, GPU/CPU, speech-optimised — significant quality upgrade over Facebook Denoiser |
| Clip selection | Anthropic Claude (claude-sonnet-4-6) | Optional; ~$0.001/video |
| Captions | ASS subtitles | `{\k}` instant highlight, all-caps, 4 words/line, 0.3s pause break |
| Chyron overlay | FFmpeg drawbox + drawtext | Full-width dark bar, white text, bottom of frame |
| Video export | FFmpeg (subprocess) | libx264 + AAC, CRF 18, faststart, `-accurate_seek` |
| Review table | pandas + st.data_editor | Inline editing, dynamic rows |
| ZIP download | Python zipfile | Flat archive of all output files |
| Profanity filter | better-profanity | Text censor + audio bleep; `god` whitelisted |

**Hardware:** NVIDIA RTX 4070 Laptop — CUDA used by WhisperX and DeepFilterNet3
**Python:** 3.12 (venv at `.venv312`) — required for PyTorch CUDA wheels

---

## Dependency Notes

### PyTorch version pinning
WhisperX 3.8.4 requires `torch~=2.8.0` but no CUDA wheels exist for 2.8.0.
Workaround: install `torch==2.6.0+cu124` and use `--no-deps` for whisperx.

```powershell
& ".venv312\Scripts\python.exe" -m pip install "torch==2.6.0+cu124" "torchaudio==2.6.0+cu124" --index-url https://download.pytorch.org/whl/cu124
& ".venv312\Scripts\python.exe" -m pip install whisperx --no-deps
& ".venv312\Scripts\python.exe" -m pip install pyannote.audio
```

### numpy conflict
DeepFilterNet3 declares `numpy<2.0` but works fine with numpy 2.x at runtime.
Always reinstall numpy 2.x after deepfilternet to satisfy WhisperX:

```powershell
& ".venv312\Scripts\python.exe" -m pip install "numpy>=2.1.0"
```

---

## File Structure

```
streamtools/
  app.py                  Streamlit UI — 4-step workflow
  config.py               Load/save show profiles + caption style to config.json
  config.json             Persisted profiles and style settings (gitignored)
  requirements.txt        Python dependencies
  launch.bat              Double-click launcher (activates venv, starts Streamlit)
  create_shortcut.ps1     One-time script to create desktop shortcut
  .streamlit/config.toml  Upload size limit (2GB)
  .env                    ANTHROPIC_API_KEY (gitignored)
  .env.example            Placeholder — safe to commit
  .gitignore
  PROJECT.md              This file
  pipeline/
    __init__.py
    transcribe.py         WhisperX: large-v3 transcription + wav2vec2 alignment
    audio_clean.py        DeepFilterNet3: 48kHz speech enhancement
    filter.py             Profanity detection, audio bleep, transcript censor
    captions.py           ASS karaoke builder + SRT builder
    export.py             FFmpeg clip export (social + YouTube)
    clip_finder.py        Claude API: suggest clips from transcript
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
# Two-pass: Whisper large-v3 transcription → wav2vec2 phoneme alignment.
# Models cached after first run. First run downloads ~1.5GB + ~300MB.
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
- Model: `claude-sonnet-4-6`

### WhisperX
- `large-v3` model (~1.5GB, `~/.cache/huggingface`)
- `wav2vec2` alignment model (~300MB, downloaded on first run)
- `compute_type="float16"` — optimal for RTX 4070 (12GB VRAM)
- Words without alignment timestamps are silently skipped

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

- [ ] **Transcript editor** — fix transcription errors before export
- [ ] **Batch upload** — queue multiple videos
- [ ] **Speaker diarization** — label who is speaking (WhisperX supports via pyannote)
- [ ] **Thumbnail generator** — still frame + text overlay for YouTube
- [ ] **Chyron UI controls** — font size and position currently hardcoded in `export.py`
- [ ] **Resemble Enhance via WSL** — best audio quality; blocked on Windows by deepspeed/libaio
- [ ] **Caption presets** — save/load named styles beyond per-show profiles
