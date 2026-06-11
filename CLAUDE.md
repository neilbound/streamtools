# streamtools — Claude Code Rules

## Python Environment

- **Always use `.venv312`** — never `python`, `streamlit`, or `pip` directly
- Correct: `.venv312\Scripts\python.exe -m streamlit run app.py`
- Wrong: `streamlit run app.py` (uses wrong Python, will fail)
- Run: `launch.bat` or double-click the desktop shortcut for normal use

## FFmpeg

- Path is hardcoded in `pipeline/export.py` and `pipeline/audio_clean.py` via `os.environ["PATH"]` injection
- Do not change or remove the hardcoded path — this is a deliberate workaround for the Windows `setx` 1024-char PATH truncation bug
- ASS/font files are copied to `tempfile.gettempdir()` and referenced by filename only (`cwd=tmp_dir`) — FFmpeg's filter parser cannot handle Windows drive-letter colons in `-vf` strings

## Encoding Conventions

- `compose_portrait` → NVENC GPU encoding (`h264_nvenc -preset p4 -cq 18`) — fast intermediate file
- `export_clip` / `export_clip_clean` → `libx264 CRF 18` — quality output for delivery
- Do not swap these; GPU intermediate is fine to re-encode, delivery files should not be NVENC

## Pipeline Contracts

- `transcribe()` must always return `{"text": str, "words": [{"word", "start", "end"}]}`
  — captions, clip finder, and export all depend on this shape
- `find_clips()` is optional and requires `ANTHROPIC_API_KEY` in `.env`
- `clean_audio()` outputs a 48kHz WAV — DeepFilterNet3 requirement; do not change sample rate

## Three-Layer Content Strategy (Shorts Season)

Every `run_shorts_season()` run produces three layers of output from a single recording session:

| Layer | Format | Source | Purpose |
|---|---|---|---|
| **Shorts clips** | 9:16 vertical | `vertical_stitched.mp4` | TikTok / Instagram / YouTube Shorts |
| **Segment videos** | 9:16 vertical + 16:9 horizontal | vertical + horizontal stitched | Per-couple/topic YouTube uploads |
| **Full episode** | 16:9 horizontal | `stitched.mp4` | YouTube long-form + Spotify podcast |

### Output directory structure

```
output/{group}/{show}_{episode_id}_{date}/
  stitched.mp4                        ← horizontal 16:9 full-episode stitch
  vertical_stitched.mp4               ← vertical 9:16 shorts stitch
  clean.wav / filtered.wav            ← horizontal enhanced audio
  vertical_clean.wav / vertical_filtered.wav
  transcript.json                     ← horizontal timeline
  vertical_transcript.json            ← vertical timeline
  pipeline_status.json
  segment_manifest.json
  episode/
    {slug}_youtube.mp4                ← full episode (16:9) for YouTube
    {slug}.srt / .mp3 / _description.txt / _shownotes.txt
  clips/
    {seg}__{clip}_social.mp4          ← 9:16 karaoke captions (for posting)
    {seg}__{clip}_youtube.mp4         ← 9:16 clean (for YouTube upload)
    {seg}__{clip}.srt / _descriptions.json
  segments/
    {seg_slug}_youtube.mp4            ← per-segment vertical 9:16
    {seg_slug}.srt
    {seg_slug}_horizontal_youtube.mp4 ← per-segment horizontal 16:9
    {seg_slug}_horizontal.srt
```

### Scheduling convention

- **Full episode** → schedule at same time as intro short
- **Segment videos (both orientations)** → schedule at same time as first short from that segment
- **Shorts clips** → scheduled via `schedule_episode_clips` or manually via `schedule_clip`

### H/V sync note

Vertical StreamYard files run slightly shorter than horizontal (~3.8% for AoA S1). The vertical
sub-pipeline is fully independent — it stitches, cleans, and transcribes vertical files separately,
so all caption timestamps are native to the vertical timeline. Zero drift.

## Per-Show Pipeline Config

Show-specific behavior lives in each profile's `pipeline` block in `config.json`
(accessed via `config.active_pipeline(cfg)`) — never hardcode show identity in pipeline code:

| Key | Purpose |
|---|---|
| `default_channel` | Publishing channel when a caller passes no `channel` (e.g. `"ilb"`, `"neilbound"`) |
| `posting_slots_utc` | Posting-time rotation, hours in UTC. Scheduler cycles through it. Default `[16, 22, 13]` (12pm/6pm/9am EST) |
| `segment_label_prefixes` | Filename prefixes stripped to build clean segment labels, e.g. `"Age of Attraction - Season 1 - "`. Longest match wins |

Channel resolution: pipeline entry points (`run_shorts_season`, `run_broadcast`,
`process_*`, `schedule_episode_clips`) default `channel=""` and resolve empty →
`pipeline.default_channel`. Pass an explicit channel to override per-call.

## Secrets

- `.env` — `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY` — never commit
- `config.json` — persisted show profiles — never commit (gitignored)

## Running a Shorts Season

Call `process_shorts_season` (MCP) or `run_pipeline.py --shorts-season` with just
`--segments-dir`. Vertical sources are auto-derived from the detected StreamYard
pairs — no need to pass `--vertical-paths`, and no need to hand-write `temp/`
launcher scripts to work around emoji (📱) paths in CLI args. Pass `vertical_paths`
only to override the auto-detected ordering.

## Publishing & Channels — operational rules

The publish queue (`output/publish_queue.json`) + `publisher_daemon.py` (Task Scheduler,
every 15 min) handle uploads. Hard-won rules — violate these and posts silently fail:

- **Verify the authorized channel after ANY YouTube re-auth.** Channel `ilb` must authorize the
  *Is Love Blind? Podcast* Google account — a **separate login**, NOT a brand account under the
  operator's personal ("Neil") account. Picking the wrong account at the OAuth screen sends every
  upload to the wrong channel. Check with `channels().list(part='snippet', mine=True)`.
- **The OAuth consent screen must be "In production," not "Testing."** Testing tokens expire every
  7 days (recurring `invalid_grant`). Production (even unverified) = long-lived tokens. Unverified
  is fine for a single operator's own account.
- **YouTube needs two scopes:** `youtube.upload` + `youtube`. The broad `youtube` scope is required
  to add Shorts to the playlist; upload-only returns 403 on `playlistItems.insert`.
- **Daemon publishes on-due, immediately.** `upload_youtube` only sets `publishAt` (private+scheduled)
  for times >60s in the future; once a post is due it publishes public immediately. Never "fix" this
  to always schedule — a past `publishAt` strands the video private.
- **Idempotency:** the daemon skips platforms already `ok`. To re-post, use `retry_failed()` (re-arms
  only failed platforms). To force a re-upload of an already-`ok` platform (e.g. wrong channel), clear
  that platform's result and set status `pending` — never blanket-reset `results={}` (re-posts the
  successful platforms too).
- **TikTok (`ilb`) is unconfigured** — uploads fail until `setup_credentials.py --platform tiktok
  --channel ilb`. Unaudited apps must post `privacy_level=SELF_ONLY` (default `PUBLIC_TO_EVERYONE`
  is rejected); flip to public in-app afterward.
- **Use the venv python** for any queue/credential script — `filelock`, `dotenv`, google libs live
  in `.venv312`, not system Python.

## Git

- `master` is the stable base; feature work goes on named branches
- `temp/` scripts are one-off helpers — do not commit them to master

## Dependencies

- DeepFilterNet3 and PyTorch are installed separately (see PROJECT.md — PyTorch version pinning)
- `requirements.txt` lists API/utility packages only; do not add PyTorch/deepfilternet there
- After installing deepfilternet, always reinstall `numpy>=2.1.0` to resolve the numpy<2.0 conflict
