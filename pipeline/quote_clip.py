"""Find a spoken quote in raw footage and cut rough review clips.

Bootstrap for the effect-board / video-soundboard workflow: search a raw
recording by the *words spoken*, get padded rough cuts + a manifest to
hand-trim, before the trimmed clips become OBS effect-board buttons
(see the AI OBS studio's `effects:` block).

Design:
- `find_quote()` is pure logic (no I/O) — exact word-subsequence match first,
  then a difflib fuzzy fallback so small misrememberings still land. Unit-tested
  against a fake transcript; never touches the network or ffmpeg.
- `extract_rough_clips()` glues transcription (Deepgram, cached next to the
  video) + `find_quote()` + a fast re-encoded ffmpeg trim. Re-encode (not
  stream-copy) so the padded start is frame-accurate — a copy would snap to the
  nearest keyframe and clip the first word.
"""
import json
import os
import re
import tempfile
from difflib import SequenceMatcher

from pipeline.export import _FFMPEG_EXE, _run_ffmpeg, get_video_duration
from pipeline.transcribe import transcribe

_WORD_RE = re.compile(r"[a-z0-9']+")


def _norm_tokens(text: str) -> list[str]:
    """Lowercase alphanumeric word tokens (drops punctuation/casing)."""
    return _WORD_RE.findall(text.lower())


def _span(words: list[dict], i: int, j: int, score: float, exact: bool) -> dict:
    """Build a match record spanning word indices i..j (inclusive)."""
    return {
        "start": float(words[i]["start"]),
        "end": float(words[j]["end"]),
        "text": " ".join(w["word"] for w in words[i:j + 1]).strip(),
        "score": score,
        "exact": exact,
    }


def find_quote(transcript: dict, quote: str, *,
               max_results: int = 5, min_score: float = 0.6) -> list[dict]:
    """Locate a quote in a transcript's word timeline.

    Args:
        transcript: {"words": [{"word", "start", "end"}, ...]} (transcribe() shape).
        quote: the line to find, as remembered — casing/punctuation don't matter.
        max_results: cap on returned candidates.
        min_score: minimum difflib ratio for a fuzzy candidate (0..1).

    Returns:
        Ranked list of {start, end, text, score, exact}, best first. Exact
        contiguous matches (score 1.0, exact=True) win; otherwise the best
        non-overlapping fuzzy windows.
    """
    words = transcript.get("words") or []
    q_tokens = _norm_tokens(quote)
    if not q_tokens or not words:
        return []

    # Deepgram emits one token per word entry; normalize defensively and keep 1:1.
    toks = []
    for w in words:
        t = _norm_tokens(w.get("word", ""))
        toks.append(t[0] if t else "")
    n, m = len(toks), len(q_tokens)

    # 1) exact contiguous subsequence (first-token prefilter skips ~all positions)
    first = q_tokens[0]
    exact_hits = [
        _span(words, i, i + m - 1, 1.0, True)
        for i in range(0, n - m + 1)
        if toks[i] == first and toks[i:i + m] == q_tokens
    ]
    if exact_hits:
        return exact_hits[:max_results]

    # 2) fuzzy fallback — score sliding windows near the quote length.
    # An hour of speech is ~9k words x 4 window sizes; a full SequenceMatcher
    # ratio() on every window is tens of seconds. quick_ratio()/real_quick_ratio()
    # are cheap upper bounds — gate on them and only run the real ratio() on
    # survivors (difflib's documented fast path). Same results, ~10x faster.
    q_join = " ".join(q_tokens)
    sizes = {s for s in (m - 1, m, m + 1, m + 2) if s >= 1}
    matcher = SequenceMatcher(None)
    matcher.set_seq2(q_join)          # difflib caches seq2 — keep the fixed quote there
    scored: list[tuple[float, int, int]] = []
    for size in sizes:
        for i in range(0, max(1, n - size + 1)):
            window = toks[i:i + size]
            if not window:
                continue
            matcher.set_seq1(" ".join(window))
            if matcher.real_quick_ratio() < min_score or matcher.quick_ratio() < min_score:
                continue
            score = matcher.ratio()
            if score >= min_score:
                scored.append((score, i, i + len(window) - 1))
    scored.sort(key=lambda t: (-t[0], t[1]))

    chosen: list[dict] = []
    used: list[tuple[int, int]] = []
    for score, i, j in scored:
        if any(not (j < a or i > b) for a, b in used):   # overlaps a better hit
            continue
        used.append((i, j))
        chosen.append(_span(words, i, j, round(score, 3), False))
        if len(chosen) >= max_results:
            break
    return chosen


def _transcript_cache_path(video_path: str) -> str:
    return os.path.splitext(video_path)[0] + ".transcript.json"


def _extract_audio(video_path: str) -> str:
    """Extract mono 16k WAV from a video for transcription (temp file)."""
    fd, wav = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    _run_ffmpeg(
        [_FFMPEG_EXE, "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", wav],
        expected_output=wav, min_bytes=1000,
    )
    return wav


def _video_fingerprint(video_path: str) -> dict:
    st = os.stat(video_path)
    return {"mtime": st.st_mtime, "size": st.st_size}


def get_transcript(video_path: str, *, use_cache: bool = True) -> dict:
    """Transcribe a raw video, caching the result next to it as `.transcript.json`.

    Searching the same footage for several quotes reuses one (paid) Deepgram
    pass. The cache stores the video's mtime+size and is invalidated when they
    change — re-recording over the same filename must not serve the old words
    (timestamps would silently point at the wrong footage). Delete the cache
    file (or pass use_cache=False) to force a re-transcribe.
    """
    cache = _transcript_cache_path(video_path)
    fp = _video_fingerprint(video_path)
    if use_cache and os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("_source") == fp:
            return cached
        # stale (video changed since the transcript was made) — fall through
    wav = _extract_audio(video_path)
    try:
        tr = transcribe(wav)
    finally:
        os.unlink(wav)
    tr["_source"] = fp
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(tr, f, ensure_ascii=False)
    return tr


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "clip"


def extract_rough_clips(video_path: str, quote: str, out_dir: str, *,
                        pad_lead: float = 0.6, pad_tail: float = 0.8,
                        max_results: int = 5, use_cache: bool = True) -> dict:
    """Find `quote` in `video_path` and write padded rough cuts to `out_dir`.

    These are first-pass candidates for manual trimming, NOT final clips — they
    re-encode fast (veryfast/CRF 20) and pad both ends so no word is clipped.

    Returns a manifest dict (also written to out_dir/manifest.json):
        {quote, video, matches: [{start,end,text,score,exact,file,
                                   clip_start,clip_duration}]}
    """
    tr = get_transcript(video_path, use_cache=use_cache)
    hits = find_quote(tr, quote, max_results=max_results)
    manifest = {"quote": quote, "video": video_path, "matches": []}
    if not hits:
        return manifest

    os.makedirs(out_dir, exist_ok=True)
    total = get_video_duration(video_path)
    slug = _slug(quote)
    for idx, hit in enumerate(hits, 1):
        start = max(0.0, hit["start"] - pad_lead)
        end = min(total, hit["end"] + pad_tail) if total else hit["end"] + pad_tail
        duration = max(0.1, end - start)
        out = os.path.join(out_dir, f"{slug}_{idx:02d}.mp4")
        _run_ffmpeg(
            [_FFMPEG_EXE, "-y", "-accurate_seek",
             "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", video_path,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-c:a", "aac", "-movflags", "+faststart", out],
            expected_output=out,
        )
        manifest["matches"].append({
            **hit,
            "file": out,
            "clip_start": round(start, 3),
            "clip_duration": round(duration, 3),
        })

    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    return manifest
