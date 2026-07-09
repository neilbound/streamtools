# streamtools — Project Context

> Running document. Update this as the project evolves.

---

## Vision

A local, GPU-accelerated video pipeline that takes raw Streamyard recordings and outputs
ready-to-upload Shorts with cleaned audio and burned-in karaoke captions — automatically.

**Why:** The current workflow spans Streamyard → CapCut, with manual handoffs at every step.
Streamyard's transcription is inaccurate; CapCut's caption styling is manual every time.
streamtools replaces both with a single browser-based UI that runs on your own machine.

**Show context:** Built around the *Is Love Blind? Podcast* covering reality TV (Love is Blind,
Age of Attraction, etc.), hosted in Ohio. Vertical video (1080×1920) exported natively from
Streamyard via MARS (Multi-Aspect Ratio Streaming).

**Scope has since expanded** beyond the original "produce Shorts" goal. streamtools now also
**publishes automatically** — a JSON-backed publish queue + a daemon upload clips and full
episodes to YouTube, Instagram, and TikTok on a schedule (see *Publishing & Distribution* below).

**Deliberate non-goals:**
- **Cloud hosting** — runs locally on the user's machine. This was re-evaluated (a cloud daemon
  was considered for PC-independence) and **deliberately rejected**: the clips, credentials, and
  queue are local and multi-GB, so a cloud move's file-sync cost outweighs the benefit. Posting
  depends on the (desktop) PC being on. YouTube's own server-side scheduling is the escape hatch
  when PC-independence is truly needed.
- **Multi-user access** — single operator.
- **Full video editing** (b-roll, graphics, music).

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
| MCP server | ✅ Working | `mcp_server.py` — full pipeline + publishing exposed as MCP tools |
| Broadcast pipeline | ✅ Working | StreamYard dual-output (16:9 episode + 9:16 shorts) → `run_broadcast()` |
| Shorts-season pipeline | ✅ Working | Multi-segment → stitched episode + per-segment videos + shorts → `run_shorts_season()` |
| Publish queue | ✅ Working | `pipeline/publish_queue.py` — JSON queue, cross-process FileLock, idempotent |
| Publisher daemon | ✅ Working | `publisher_daemon.py` — Task Scheduler every 15 min; rotating file log |
| Multi-platform upload | ✅ Working | YouTube (scheduled/immediate + playlists), Instagram Reels, TikTok* |
| Auto-retry + backoff | ✅ Working | Failed platforms retry 30/60/120 min, cap 4 attempts |
| Episode descriptions | ✅ Working | `pipeline/describe.py` — Claude-generated YouTube/Spotify copy |
| QA validation | ✅ Working | `pipeline/validate.py` — per-clip checks before scheduling |
| Upload self-healing | ✅ Working | YouTube timeout-recovery + truncation detect/retry; playlist-add retry |
| Reconciliation | ✅ Working | `reconcile_youtube()` audits posted vs channel; daemon runs daily |
| Drive archive | ✅ Working | `pipeline/archive.py` — per-episode deliverable move to Drive folder |
| Performance analytics | ✅ Working | `pipeline/analytics.py` — views/engagement + retention; daily snapshots |
| Opening hook overlay | ✅ Working | bold hook card on first 3.5s (`render_hook_card`); clip-finder emits `hook` |
| Test suite | ✅ Working | `tests/` — 104 pure-Python tests (`pytest tests -q`) |

\* TikTok (`ilb`) posts via **inbox mode** (`video.upload`): clip lands in the account's
drafts, operator taps Post in the mobile app. Direct public auto-post needs a TikTok audit
+ domain verification (deferred). See Publishing.

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

The Streamlit app is the **manual/interactive** path. Most production work now runs through the
**automated pipelines** below, driven by `run_pipeline.py` or the MCP tools in `mcp_server.py`.

---

## Automated Pipelines

Three orchestrators in `run_pipeline.py`, all writing to `output/{group}/{show}_{id}_{date}/`:

| Pipeline | Entry point | Input | Produces |
|---|---|---|---|
| **Standard** | `run()` | 1+ source files | Social + YouTube clips |
| **Broadcast** | `run_broadcast()` | StreamYard 16:9 + 9:16 pair | Full episode + clips (both from one session) |
| **Shorts season** | `run_shorts_season()` | Directory of StreamYard segment pairs | Stitched episode + per-segment videos + shorts |

### Shorts season — the three-layer content model

A "shorts season" is several short segments (intro, overall impressions, per-couple/topic) recorded
in one StreamYard session as dual-output pairs (16:9 `Title.mp4` + 9:16 `Title 📱.mp4`). One run
produces **three layers** from the same recording:

| Layer | Format | Source | Purpose |
|---|---|---|---|
| **Shorts** | 9:16 | `vertical_stitched.mp4` | TikTok / Reels / YouTube Shorts |
| **Segment videos** | 9:16 **and** 16:9 | vertical + horizontal stitches | Per-couple/topic YouTube uploads, both orientations |
| **Full episode** | 16:9 | `stitched.mp4` | YouTube long-form + Spotify |

**Design choice — independent vertical pipeline (zero H/V drift):** the vertical StreamYard file
runs ~3.8% shorter than the horizontal. Rather than fight sync drift, the vertical sub-pipeline is
fully independent: it stitches, cleans (DeepFilter), and transcribes (Deepgram) the *vertical*
files separately, so every caption timestamp is native to the vertical timeline. Shorts are cut
from the vertical source for both audio and video — no drift, and the StreamYard per-segment title
cards are preserved. `vertical_paths` is auto-derived from the detected segment pairs.

**Per-show config** lives in each profile's `pipeline` block (`config.active_pipeline`): so show
identity is never hardcoded in pipeline code. Keys: `default_channel`, `posting_slots_utc`,
`segment_label_prefixes`. See CLAUDE.md → *Per-Show Pipeline Config*.

---

## Publishing & Distribution

Publishing is decoupled from production via a **JSON publish queue** (`output/publish_queue.json`)
and a **daemon** (`publisher_daemon.py`) run by Windows Task Scheduler every 15 minutes.

### The daemon model
- Producers `enqueue()` clips with a `scheduled_time` + target platforms.
- Each run, the daemon processes `get_due()` (pending, time reached) **+** `get_retryable()`
  (failed/partial, past backoff).
- It uploads each clip's platforms, recording per-platform results on the entry.

**Publish-on-due, not pre-schedule:** the daemon runs *at* a post's scheduled time and publishes it
**immediately**. YouTube's `publishAt` only accepts *future* times, so passing the (now-past)
scheduled time would strand the video private — `upload_youtube` therefore publishes immediately as
public when the time has arrived, and only uses `publishAt` (private + scheduled) for genuinely
future times. (Pre-uploading everything as future-scheduled is the alternative "PC-independent"
mode, used only if posting must survive the PC being off.)

### Reliability design choices
- **Idempotency:** the daemon skips any platform already marked `ok`; `retry_failed()` re-arms only
  *failed* platforms, preserving successes. A post is never uploaded twice to the same platform.
- **Cross-process lock:** all queue read-modify-write goes through a `FileLock` so the daemon and
  MCP tools can't drop each other's entries.
- **Auto-retry with backoff:** failed/partial entries retry at 30/60/120 min, capped at 4 attempts,
  then left for manual `retry_failed`.
- **Rotating log:** `output/publisher_daemon.log` (the daemon runs unattended, so stdout is lost).

### Channels & credentials — operational facts (hard-won)
Credentials are per-channel env vars (`{CHANNEL}_YOUTUBE_*`, etc.) resolved by `_cred(channel, key)`.

- **Channel `ilb` → the *Is Love Blind? Podcast* YouTube channel, which is a SEPARATE Google
  login** — *not* a brand account under the operator's personal ("Neil") Google account. Re-auth
  must sign into that specific account or uploads land on the wrong channel. **Always verify the
  authorized channel after any re-auth** (`channels().list(mine=True)`).
- **The OAuth consent screen MUST be "In production," not "Testing."** Testing-mode refresh tokens
  expire every **7 days** — the cause of recurring "invalid_grant" outages. Production (even
  unverified) issues long-lived tokens. Unverified is fine for a single operator's own account.
- **YouTube scopes:** `youtube.upload` *and* `youtube` — the broader scope is required to add
  uploaded Shorts to a playlist (`youtube_playlist_shorts` in the brand config).
- **TikTok:** requires per-channel dev app credentials. Unaudited apps can only post to the owner's
  own account and are restricted to `privacy_level=SELF_ONLY` (the default `PUBLIC_TO_EVERYONE` is
  rejected). Workflow without the audit/demo-video: post `SELF_ONLY`, then flip to public in-app.

### Scheduling convention
- **Primary cadence: 1 short/day at 6pm EDT (22:00 UTC).** 2/day oversaturated and split
  the algorithm's attention while the audience is small — revisit 2/day once followers grow.
  The active profile's `posting_slots_utc` is `[22]` (single 6pm slot ⇒ 1/day at 6pm).
- **Segment videos (both orientations)** → same day/time as the *first short of that segment*
  (the cluster). Those are the only days carrying more than one upload.
- **Full episode** → same time as the intro short.

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
  config.py               Show profiles: producer_context, caption style, brand links, pipeline cfg
  config.json             Persisted profiles (gitignored)
  run_pipeline.py         Orchestrators: run(), run_broadcast(), run_shorts_season() + CLI
  mcp_server.py           MCP tools — pipeline + publishing for Claude Desktop / Cowork
  publisher_daemon.py     Processes due publish-queue entries; run by Task Scheduler every 15 min
  setup_credentials.py    Interactive OAuth setup for YouTube / TikTok / Instagram per channel
  test_credentials.py     Verify stored credentials per platform/channel
  requirements.txt        Python deps (API + utils; PyTorch + DeepFilterNet installed separately)
  launch.bat              Double-click launcher (activates venv, starts Streamlit)
  .env                    API keys + per-channel publishing credentials (gitignored)
  .env.example            Placeholder — safe to commit
  CLAUDE.md               Claude Code rules for this project
  PROJECT.md              This file
  output/                 Per-episode outputs + publish_queue.json + daemon log (gitignored)
  pipeline/
    episode.py            Episode naming, directory tree (clips/, episode/, segments/), status JSON
    transcribe.py         Deepgram Nova-3 API: word-level transcription
    audio_clean.py        DeepFilterNet3: 48kHz GPU speech enhancement
    filter.py             Profanity detection, audio bleep, transcript censor
    captions.py           ASS karaoke builder + SRT builder
    export.py             FFmpeg: compose_portrait, export_clip(_clean), stitch_segments,
                          episode export, render_hook_card (opening hook overlay)
    clip_finder.py        Claude: suggest clips (+ per-clip hook); filler-trim openings
    describe.py           Claude: episode + per-clip platform descriptions
    validate.py           Per-clip QA checks (timing, transcript coverage, black/silence)
    podcast.py            Podcast MP3 export (ID3 tags) for Spotify
    publish.py            Platform uploaders: YouTube (self-healing) / Instagram / TikTok;
                          reconcile_youtube() channel audit
    publish_queue.py      JSON publish queue: enqueue, get_due, get_retryable, schedule_retry,
                          find_schedule_gaps, FileLock
    analytics.py          YouTube stats + retention snapshots (Data + Analytics APIs); report()
    archive.py            Move posted-episode deliverables to a Drive-synced folder
  tests/                  Pure-Python test suite (no network/ffmpeg/GPU) — pytest tests -q
  scheduled-tasks/        (~/.claude) cron tasks: john-cohort-retention-check, weekly-stats-deep-dive
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

- [x] ~~**MCP server**~~ — built (`mcp_server.py`): full pipeline + publishing exposed as MCP tools
- [x] ~~**Batch upload / scheduling**~~ — built (publish queue + daemon)
- [x] ~~**TikTok for `ilb`**~~ — inbox/draft mode working (`upload_tiktok(post_mode="inbox")`, default);
      operator taps Post in the mobile app. Direct public auto-post deferred (needs audit + domain verify).
- [x] ~~**Upload self-healing**~~ — YouTube Shorts + episode uploads recover from timeout-after-success
      (id recovered via uploads playlist) and truncation (`classify_youtube_health` → delete+retry);
      playlist add retries transient errors.
- [x] ~~**Schedule-stuck / drift verification**~~ — `reconcile_youtube()` + `reconcile_uploads` MCP tool
      audit ok'd entries against the channel; daemon runs it once/24h (`_maybe_reconcile`).
- [x] ~~**Channel performance analytics**~~ — built (`pipeline/analytics.py` + `refresh_analytics`,
      `performance_report`, `video_performance` MCP tools). Daily snapshot time series
      (`output/analytics/snapshots.jsonl`); leaderboards + group-bys. **Tier 2 (retention) live** —
      needed the `yt-analytics.readonly` scope AND enabling the YouTube Analytics API in the GCP
      project (separate step). Daemon runs `reconcile_uploads` daily; scheduled deep-dive task weekly.
- [x] ~~**Opening hook overlay + clip-opening optimization**~~ — retention data showed clips opening on
      a concrete claim retain ~40% vs ~22% for filler openers, but conversational source rarely opens
      punchy. So: hardened clip-finder (concrete-hook prompt + `_trim_leading_filler`), and an on-screen
      bold hook card on the first 3.5s (`render_hook_card` + `export_clip(hook_text=)`, clip-finder emits
      a `hook`, `pipeline.hook_overlay` flag). Rolled to all remaining shorts; under measurement.
- [x] ~~**Quote → clip extractor**~~ — built (`pipeline/quote_clip.py` + `find_quote_clips` MCP tool).
      Search raw footage by the words spoken (exact word-subsequence match, difflib fuzzy fallback),
      cut padded rough candidates + a manifest for hand-trimming. Deepgram transcript cached next to
      the video. First pass that feeds the **AI OBS effect board** (video-soundboard Stream Deck
      buttons) — the trimmed keepers become effect clips. See [AI OBS](../AI OBS/SPEC.md).
- [ ] **Instagram analytics** — Reel insights (reach, plays, saves, shares) via the Graph API for
      cross-platform comparison (deferred; YouTube-only for now).
- [ ] **Daemon supervision** — Task Scheduler works but is fragile (was found *Disabled*, causing a
      silent multi-day posting gap). Consider a `--loop` mode + NSSM service, or at least a health
      alert. Requires the desktop to stay on + logged in (task is `Interactive` logon).
- [x] ~~**Archive posted videos to Google Drive**~~ — built (`pipeline/archive.py` +
      `archive_posted_episodes` MCP tool). Per-episode sweep: once every queued clip of an episode
      is YouTube-confirmed, its deliverables (clips/segments/episode) are copied to the archive root,
      size-verified, then deleted locally (intermediates stay). Point `STREAMTOOLS_ARCHIVE_ROOT` at a
      Google Drive for Desktop synced folder. *Pending: operator to install Drive for Desktop.*
- [ ] **Transcript editor** — fix transcription errors before export
- [ ] **Thumbnail generator** — still frame + text overlay for YouTube
- [ ] **Chyron UI controls** — font size and position currently hardcoded in `export.py`
- [ ] **Speaker diarization** — Deepgram `diarize=True`
- [ ] **Resemble Enhance via WSL** — best audio quality; blocked on Windows by deepspeed/libaio
- [ ] **Caption presets** — save/load named styles beyond per-show profiles

---

## History

Reverse-chronological log of major milestones. Operational gotchas live in CLAUDE.md.

- **2026-07-09** — **Quote → clip extractor** (`quote_clip.py` + `find_quote_clips`): find a
  spoken line in raw footage (exact + fuzzy), cut padded rough candidates for hand-trimming.
  Bootstraps the AI OBS video-soundboard/effect-board workflow.
- **2026-06-22** — Rolled the hardened finder + hook overlay to all remaining shorts
  (John / Jorge / Logan = the "new-approach" cohort, posting Jun 19–27) vs the older baseline.
  Performance finding: Chris & Leah retain ~44% but get few views → a *distribution* problem,
  not hooks. Scheduled tasks added: one-time new-vs-old retention check + weekly stats deep-dive.
- **2026-06-17** — Channel **performance analytics** (`analytics.py`: Tier-1 stats + Tier-2
  retention via the YouTube Analytics API; daily snapshots; `report()` + MCP tools). Data showed
  openings drive retention → **clip-opening optimization** (concrete-hook prompt + filler-trim) and
  an **on-screen hook overlay** (`render_hook_card`). Cadence cut to **1 short/day at 6pm**.
  Fixed `config.py` UTF-8 read/write (Windows cp1252 crash on curly quotes).
- **2026-06-12** — Reliability pass: **upload self-healing** (timeout recovery + truncation
  detect/retry), **daily reconciliation**, daemon **run-lock** (no overlap double-posts),
  **schedule gap-check**, **Drive archive** (`archive.py`), fatal-vs-retryable retry classification,
  pre-publish **QA gate** (`validate.py`), and a pure-Python **test suite**. **TikTok inbox mode**
  working for `ilb`. Root-caused the recurring outages: publish-when-due (past `publishAt`),
  playlist scope, wrong-channel (`ilb` = a separate Google login, not a brand account), and the
  OAuth consent screen needing **Production** status (Testing = 7-day token expiry).
- **2026-05** — **Shorts-season** pipeline (three-layer: shorts + per-segment videos in both
  orientations + stitched episode; independent vertical sub-pipeline = zero H/V drift),
  **broadcast** pipeline, **publish queue + daemon**, **MCP server**, per-show `pipeline` config.
- **Earlier** — Core local pipeline (compose → clean → transcribe → find clips → caption → export)
  and the Streamlit UI.
