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

## Secrets

- `.env` — `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY` — never commit
- `config.json` — persisted show profiles — never commit (gitignored)

## Git

- `master` is the stable base; feature work goes on named branches
- Next planned feature: `mcp_server.py` — expose pipeline as MCP tools for Claude Desktop Cowork

## Dependencies

- DeepFilterNet3 and PyTorch are installed separately (see PROJECT.md — PyTorch version pinning)
- `requirements.txt` lists API/utility packages only; do not add PyTorch/deepfilternet there
- After installing deepfilternet, always reinstall `numpy>=2.1.0` to resolve the numpy<2.0 conflict
