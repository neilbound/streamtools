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
- `find_clips()` is optional and requires `ANTHROPIC_API_KEY` in `.env`; each clip also
  carries a short `hook` (≤5 words) used for the opening hook overlay
- `clean_audio()` outputs a 48kHz WAV — DeepFilterNet3 requirement; do not change sample rate

## Opening hook overlay

Conversational source rarely opens on a punchy spoken line, and retention analysis showed
clips that present a concrete claim up front hold far better — so the hook lives ON SCREEN.
`export_clip(..., hook_text=...)` renders a bold, rounded, opaque Montserrat-ExtraBold card
(`render_hook_card`, Pillow) and overlays it on the first `HOOK_SECS` (3.5s) via filter_complex.
`run_shorts_season` passes each clip's `hook` when the active profile's `pipeline.hook_overlay`
is true (default). Multi-line text MUST be built in-memory (Pillow), not via a drawtext textfile —
Windows CRLF in a textfile renders the line break as tofu rectangles.

## Tests

- `tests/` is pure-Python pytest — no network, no real ffmpeg, no GPU. Run with:
  `.venv312\Scripts\python.exe -m pytest tests -q`
- Queue tests use the `queue_paths` conftest fixture (repoints the queue at tmp_path) —
  never let a test touch `output/publish_queue.json`

## QA Gate (pre-publish validation)

- `pipeline/validate.py` is the QA hub: `QA_PROFILES` (clip = portrait ≤180s hard cap,
  62s perf warning; episode = landscape probe-only), `validate_media()` (deep=True adds a
  one-pass blackdetect+volumedetect+truncation decode), `quick_probe_check()` (cheap ffprobe
  for daemon pre-flight/resume), `valid_intermediate()` (resume-time file integrity)
- **Issues BLOCK scheduling; warnings don't.** `schedule_episode_clips`, `schedule_clip`,
  `publish_clip_now` refuse on QA issues unless `force=True`. `schedule_episode_clips`
  also supports `dry_run=True` (full report, zero side effects)
- StreamYard MARS vertical = 720x1280 — that's normal, not a defect (quality floor, not target)
- Truncated-but-probe-clean files (intact faststart moov, missing data) are only caught by
  the deep decode pass — that's why the one-off schedulers use `deep=True`
- Manual QA CLI: `.venv312\Scripts\python.exe -m pipeline.validate <file-or-dir> [--profile clip|episode] [--quick]`
- `_run_ffmpeg()` verifies output exists/size (and probes intermediates reused on resume) —
  pass `expected_output=` on any new call site
- Resume branches use `valid_intermediate()`, not `os.path.exists()` — invalid intermediates
  regenerate instead of silently poisoning later steps

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
- **YouTube uploads self-heal.** `upload_youtube`/`upload_youtube_episode` (3 attempts): a clean upload
  is trusted; on an upload exception the id is recovered from the channel's uploads playlist (the bytes
  may have landed — avoids a duplicate re-upload); a *recovered* video is health-checked
  (`classify_youtube_health`: real duration = ok, `P0D`+failed = truncated) and deleted+retried if
  truncated. Do NOT health-check clean uploads — a healthy fresh upload reads `P0D` for ~a minute.
  Playlist add retries transient `429`/`409`. `reconcile_youtube()` / the `reconcile_uploads` MCP tool
  audits ok'd entries against the channel after the fact (flags `missing`/`truncated`); the daemon
  also runs this automatically once per 24h (`_maybe_reconcile`) and logs any drift.
- **TikTok (`ilb`) needs its own account auth.** The Is Love Blind? TikTok is a **separate account**
  from `neilbound` — same wrong-account trap as YouTube. Log the browser into the *Is Love Blind?*
  TikTok ONLY, then run `setup_credentials.py --platform tiktok --channel ilb` (writes `ILB_TIKTOK_*`).
  `neilbound` already has `NEILBOUND_TIKTOK_*`; do not reuse it for `ilb` content.
- **TikTok uploads to drafts (inbox) by default.** `upload_tiktok` has two modes, resolved from the
  `post_mode` arg then `{CHANNEL}_TIKTOK_POST_MODE` / `TIKTOK_POST_MODE`, default `"inbox"`:
  - `"inbox"` (scope `video.upload`): lands the clip in the TikTok app's drafts. The operator opens
    TikTok and taps Post to publish. No audit, no domain verification. The daemon marks the platform
    `ok` once uploaded, but the video is NOT live until the manual tap. This is the working default.
  - `"direct"` (scope `video.publish`): Direct Post at `privacy_level` (default `SELF_ONLY`; unaudited
    apps may only post private). Requires Direct Post + `neilbound.me` domain verification on the app.
    A `creator_info/query` preflight fails fast if the privacy level isn't available.
  Setup auths the `video.upload` scope (`setup_credentials.py`). To switch to direct posting later,
  enable Direct Post on the app, re-auth with `video.publish`, and set `ILB_TIKTOK_POST_MODE=direct`.
- **Use the venv python** for any queue/credential script — `filelock`, `dotenv`, google libs live
  in `.venv312`, not system Python.
- **Daemon failure semantics:** credential/config errors (`is_fatal_error` — 401/403,
  invalid_grant, missing creds, ValueError) are marked `fatal` and never auto-retry; fix the
  cause then `retry_failed_clip`. Network/5xx errors keep the 4-round exponential backoff.
- **Operator visibility:** enqueue warnings persist in each entry's `warnings` list (stdout is
  discarded under Task Scheduler). `list_scheduled_clips` opens with a NEEDS ATTENTION section
  (failed/partial entries, queue warnings, unposted TikTok drafts). After tapping Post on a
  TikTok inbox upload, run `confirm_tiktok_posted(post_id)` to clear the draft reminder.

## Archiving posted videos

`pipeline/archive.py` + the `archive_posted_episodes` MCP tool move finished episodes
to cold storage (a Google Drive for Desktop synced folder) to free local disk.

- **Trigger:** per-episode sweep — an episode is archivable once *every* queued clip
  for it has a confirmed YouTube upload (`results.youtube.status == "ok"`).
- **Scope:** deliverables only (`clips/`, `segments/`, `episode/`). Regenerable
  intermediates (`stitched.mp4`, `vertical_stitched.mp4`, `*.wav`, transcripts) stay local.
- **Safety:** move-once-verified — copy → verify size at destination → delete local
  originals only after *all* files verify. `ARCHIVED.json` marker makes it idempotent.
- **Config:** set `STREAMTOOLS_ARCHIVE_ROOT` to the Drive-synced folder. Always
  `dry_run=True` first. Note: with Drive for Desktop, "verified at destination" confirms
  the file reached the synced folder; Drive uploads to the cloud asynchronously after.

## Performance analytics

`pipeline/analytics.py` + MCP tools (`refresh_analytics`, `performance_report`,
`video_performance`) measure YouTube performance to optimize content.

- **Two tiers:** Tier 1 (views/likes/comments) works with the `youtube` scope. Tier 2
  (retention `averageViewPercentage`, avg view duration, subscribers, shares) needs the
  **`yt-analytics.readonly`** scope — one re-auth via `setup_credentials.py`. `fetch_video_analytics`
  degrades gracefully (returns `{}`) until the scope is granted, so Tier 1 always works.
- **GOTCHA (separate from the scope):** Tier 2 also requires the **YouTube Analytics API to be
  enabled in the GCP project** (console → Enable API). A 403 saying "API has not been used in
  project … or it is disabled" means enable the API — NOT another re-auth. The scope grants
  permission; the API toggle is independent. (Both are now done; Tier 2 retention is live.)
- A manual `refresh_analytics` forces a same-day re-snapshot (`snapshot(force=True)`) so a pull
  right after enabling Tier 2 captures retention without waiting for the next daily run.
- **Daily snapshots:** the daemon's `_maybe_snapshot()` (once/24h, mirrors `_maybe_reconcile`)
  appends one row/video/day to `output/analytics/snapshots.jsonl` — a growth time series so
  videos can be compared at the **same age** (Shorts accrue views over days; never rank raw
  lifetime views across different ages).
- **Metadata join:** video_id → couple/segment (clip_path `__` split), posting weekday/hour,
  clip length, from `publish_queue.list_all()`. Group-by breakdowns drive content decisions.
- Retention is the strongest Shorts signal; engagement and timing are secondary. Treat the
  small sample as directional, not statistical.

## Quote → clip extractor (effect-board bootstrap)

`pipeline/quote_clip.py` + the `find_quote_clips` MCP tool find a spoken line in raw footage
and cut padded rough candidates for hand-trimming — the first pass that feeds the AI OBS
effect-board (video-soundboard) buttons.

- `find_quote(transcript, quote)` is **pure logic** (no I/O): exact word-subsequence match
  first, then a difflib fuzzy fallback (`min_score=0.6`) so small misrememberings still hit.
  Returns ranked `{start,end,text,score,exact}`. Unit-tested with a fake transcript — keep it
  network/ffmpeg-free.
- `get_transcript(video)` caches the Deepgram pass next to the video as `<video>.transcript.json`
  so repeat searches on the same footage are free; delete it (or `use_cache=False`) to redo.
- `extract_rough_clips()` **re-encodes** (libx264 veryfast/CRF 20), never stream-copies — a copy
  snaps to the nearest keyframe and clips the first word. Pads both ends (`pad_lead`/`pad_tail`),
  clamps to the video duration, writes `manifest.json` alongside the cuts.
- These are review candidates, NOT final clips — the operator trims, then wires the keepers into
  the AI OBS studio's effects board.

## Git

- `master` is the stable base; feature work goes on named branches
- `temp/` scripts are one-off helpers — do not commit them to master

## Dependencies

- DeepFilterNet3 and PyTorch are installed separately (see PROJECT.md — PyTorch version pinning)
- `requirements.txt` lists API/utility packages only; do not add PyTorch/deepfilternet there
- After installing deepfilternet, always reinstall `numpy>=2.1.0` to resolve the numpy<2.0 conflict
