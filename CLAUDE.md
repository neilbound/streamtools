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

## Secrets

- `.env` — `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY` — never commit
- `config.json` — persisted show profiles — never commit (gitignored)

## Git

- `master` is the stable base; feature work goes on named branches
- `temp/` scripts are one-off helpers — do not commit them to master

## Dependencies

- DeepFilterNet3 and PyTorch are installed separately (see PROJECT.md — PyTorch version pinning)
- `requirements.txt` lists API/utility packages only; do not add PyTorch/deepfilternet there
- After installing deepfilternet, always reinstall `numpy>=2.1.0` to resolve the numpy<2.0 conflict
