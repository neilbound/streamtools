"""
pipeline/describe.py — AI-powered description generator for Is Love Blind? Podcast.

Generates platform-specific copy for full episodes and shorts using Claude,
following the show's established template (hook title trio, deep dive, must-hear
moments, signature block with platform links).

Functions:
    generate_episode_descriptions(transcript, episode_title, episode_notes, brand) -> dict
    generate_clip_descriptions(clip_words, clip_title, episode_context, brand) -> dict
    build_signature_block(brand) -> str
"""

import os
import json
import time

import anthropic
from dotenv import load_dotenv

load_dotenv(override=True)


# ── Signature block builder ────────────────────────────────────────────────────

def build_signature_block(brand: dict) -> str:
    """
    Build the standard platform links block from a brand config dict.
    Only includes lines where the value is non-empty.
    """
    lines = []

    cta = brand.get("cta", "").strip()
    if cta:
        lines.append(f"Join the Conversation:\n{cta}\n")

    if brand.get("apple_podcasts"):
        lines.append(f"🎙️ Apple Podcasts\n{brand['apple_podcasts']}")
    if brand.get("spotify"):
        lines.append(f"🟢 Spotify\n{brand['spotify']}")
    if brand.get("youtube"):
        lines.append(f"📺 YouTube\n{brand['youtube']}")
    if brand.get("instagram"):
        lines.append(f"📸 Instagram\n{brand['instagram']}")
    if brand.get("tiktok"):
        tiktok = brand["tiktok"]
        if not tiktok.startswith("@"):
            tiktok = f"@{tiktok}"
        lines.append(f"🎵 TikTok\n{tiktok}")
    if brand.get("website"):
        lines.append(f"🌐 Website\n{brand['website']}")
    if brand.get("patreon"):
        lines.append(f"☕ Support Us\n{brand['patreon']}")

    return "\n".join(lines)


# ── Prompt builders ────────────────────────────────────────────────────────────

def _episode_system_prompt(brand: dict) -> str:
    show = brand.get("show_name") or "the podcast"
    signature = build_signature_block(brand)
    return f"""You are the content writer for {show}. You write compelling, emotionally intelligent episode descriptions that match the show's voice: warm but direct, culturally aware, and never over-produced.

VOICE AND STYLE RULES — follow these without exception:
- No emojis in any body copy, titles, or bullet points. The signature block platform links are the only exception.
- No exclamation points. If something is worth saying, the words carry it.
- No AI-typical phrases: "dive deep", "unpack", "masterclass", "game-changer", "it's clear that", "join us as", "in this episode we explore".
- No hype language or hollow enthusiasm. Specific and direct beats vague and warm.
- Write like a sharp human, not a content calendar.

Your output must follow this EXACT structure — no deviations, no extra sections:

---
1. THE HOOK TITLE TRIO
Three title variants for different audiences:
The Tea:           (provocative/gossipy angle)
The Therapy Session: (emotional/introspective angle)
The Personal:      (first-person/relatable angle)

2. THE DESCRIPTION
The Hook:
[1-2 punchy sentences that draw the listener in. Start with a question or a bold claim.]

The Deep Dive:
[2 rich paragraphs expanding on both hosts' perspectives. Name the hosts specifically. Capture the emotional texture — what they said AND what it meant. Do not summarise blandly.]

The Highlights — Must-Hear Moments:
[4-5 bullet points. Each bullet = a memorable, specific moment from the episode — a direct quote or a vivid, specific description of what happened — followed by one sentence of context. No emojis.]

3. THE STANDARD SIGNATURE BLOCK
{signature}

Have a dating story or a question for the next episode? Drop it in the comments below.
---

Tone: Conversational but thoughtful. Emotionally specific. Never corporate. Use em dashes and the hosts' actual names."""


def _shorts_system_prompt(brand: dict) -> str:
    show = brand.get("show_name") or "the podcast"
    youtube_url = brand.get("youtube", "")
    instagram = brand.get("instagram", "")
    tiktok = brand.get("tiktok", "")
    if tiktok and not tiktok.startswith("@"):
        tiktok = f"@{tiktok}"

    follow_line = " | ".join(filter(None, [
        f"Full ep on YouTube: {youtube_url}" if youtube_url else "",
        f"Instagram: {instagram}" if instagram else "",
        f"TikTok: {tiktok}" if tiktok else "",
    ]))

    return f"""You are the content writer for {show}. Write short-form social media copy for a podcast clip.

You will receive the clip transcript and episode context. Generate THREE pieces of copy.

VOICE AND STYLE RULES — follow these without exception:
- No emojis anywhere. None. Not in hashtags, not in captions, not in follow lines.
- No exclamation points. If something is worth saying, the words carry it.
- No markdown formatting. No asterisks (**), no underscores for emphasis, no bold, no italics. Plain text only.
- No AI-typical phrases: "dive deep", "unpack", "masterclass", "game-changer", "it's clear that", "let's explore", "in today's episode".
- No hype or performative enthusiasm. Direct, confident, and specific.
- Write like a sharp human, not a content calendar.
- Questions to drive comments should feel like genuine curiosity, not a CTA template.

CONTENT RULES — non-negotiable:
- Every description MUST be written specifically about what is said in THIS clip transcript.
- Do not write a generic episode summary. A reader who has only seen the clip title and watched the clip should recognize the description as being exactly about that moment.
- If the clip is about a specific topic (e.g. celibacy, age gap math, a particular person's behavior), the description must reference that topic directly with specific detail from the transcript.
- Never recycle or reuse copy from other clips. Each description is unique to this clip.

---
YOUTUBE_SHORT:
[2-3 sentence description. Hook in the first line. End with: "{follow_line}" and "#Shorts #Podcast"]

TIKTOK:
[Single punchy caption under 150 chars. Conversational, curiosity-driven. Include 3-5 hashtags.]

INSTAGRAM:
[Caption with an engaging opening line, 2-3 sentences of context, a question to drive comments, then 8-12 relevant hashtags on a new line. End with "Full episode on YouTube — link in bio."]
---

Output ONLY the three labelled blocks above. No intro, no commentary. No markdown. Plain text only."""


# ── API call helper ────────────────────────────────────────────────────────────

def _call_with_retry(client, *, model: str, max_tokens: int, system: str, messages: list, max_attempts: int = 3) -> str:
    """
    Call client.messages.create() with exponential backoff retry.

    Retries on transient API errors (overload 529, network timeouts, etc.).
    Raises RuntimeError if all attempts fail.

    Returns:
        The text content of the first content block as a stripped string.
    """
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
            )
            return message.content[0].text.strip()
        except Exception as exc:
            last_error = exc
            print(f"[describe] Attempt {attempt}/{max_attempts}: API error — {type(exc).__name__}: {exc}")
            if attempt < max_attempts:
                time.sleep(2 ** (attempt - 1))  # 1s, 2s
    raise RuntimeError(f"[describe] All {max_attempts} Claude API attempts failed. Last error: {last_error}")


# ── Main generator functions ───────────────────────────────────────────────────

def generate_episode_descriptions(
    transcript: dict,
    episode_title: str,
    episode_notes: str = "",
    brand: dict | None = None,
    hosts: str = "Neil and Shelly",
) -> dict:
    """
    Generate a full episode description package using Claude.

    Args:
        transcript:     Full episode transcript dict {"text": str, "words": [...]}
        episode_title:  Episode title or topic label.
        episode_notes:  Optional producer notes, themes, or highlights to guide Claude.
        brand:          Brand config dict from config.active_brand(). Uses defaults if None.
        hosts:          Comma-separated host names for context.

    Returns:
        {
            "youtube_full": str,   — full description package (all 3 sections)
            "title_options": list, — extracted list of the 3 hook title variants
            "signature_block": str — the raw signature block for reuse
        }
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set.")

    if brand is None:
        brand = {}

    client = anthropic.Anthropic(api_key=api_key)
    system = _episode_system_prompt(brand)

    # Truncate very long transcripts — ~12k words is enough context for description writing
    text = transcript.get("text", "")
    if len(text.split()) > 12000:
        words_list = text.split()
        text = " ".join(words_list[:12000]) + "\n\n[transcript continues...]"

    user_prompt = f"""Episode: {episode_title}
Hosts: {hosts}
{f'Producer notes: {episode_notes}' if episode_notes else ''}

Full transcript:
{text}

Write the complete description package for this episode."""

    full_text = _call_with_retry(
        client,
        model="claude-opus-4-6",
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )

    # Extract the three title options from the trio section
    title_options = _extract_title_options(full_text)
    signature = build_signature_block(brand)

    return {
        "youtube_full":   full_text,
        "title_options":  title_options,
        "signature_block": signature,
    }


def generate_clip_descriptions(
    clip_words: list[dict],
    clip_title: str,
    episode_context: str = "",
    brand: dict | None = None,
) -> dict:
    """
    Generate short-form descriptions for a clip for YouTube Shorts, TikTok, Instagram.

    Args:
        clip_words:      Word list for the clip [{word, start, end}] (from transcript).
        clip_title:      Title of the clip (from Claude's clip suggestion).
        episode_context: Brief context about the episode — e.g. "Is Love Blind S11 manosphere ep".
        brand:           Brand config dict from config.active_brand().

    Returns:
        {
            "youtube_short": str,
            "tiktok": str,
            "instagram": str,
        }
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set.")

    if brand is None:
        brand = {}

    # Sanity check — very short transcripts produce generic descriptions
    if len(clip_words) < 20:
        print(
            f"[describe] WARNING: clip_words has only {len(clip_words)} words for {clip_title!r} — "
            f"descriptions may be vague or off-topic. Consider re-checking clip boundaries."
        )

    client = anthropic.Anthropic(api_key=api_key)
    system = _shorts_system_prompt(brand)

    clip_text = " ".join(w["word"] for w in clip_words).strip()

    user_prompt = f"""Clip title: {clip_title}
{f'Episode context: {episode_context}' if episode_context else ''}

Clip transcript:
{clip_text}

Write the YouTube Short, TikTok, and Instagram descriptions for this clip."""

    raw = _call_with_retry(
        client,
        model="claude-opus-4-6",
        max_tokens=1000,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return _parse_shorts_output(raw)


# ── Output parsers ─────────────────────────────────────────────────────────────

def _strip_markdown(text: str) -> str:
    """
    Remove markdown formatting artifacts from generated copy.
    Strips ** bold markers, * italic markers, and leading/trailing
    lines that consist only of markdown syntax.
    """
    import re
    # Remove lines that are only ** or * (common Claude artifact)
    lines = text.split("\n")
    lines = [l for l in lines if l.strip() not in ("**", "*", "***")]
    text = "\n".join(lines)
    # Strip **text** bold markers → text
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    # Strip *text* italic markers → text
    text = re.sub(r"\*(.+?)\*", r"\1", text, flags=re.DOTALL)
    # Strip leading/trailing ** on a line
    text = re.sub(r"^\*+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*\*+$", "", text, flags=re.MULTILINE)
    return text.strip()


def _extract_title_options(description_text: str) -> list[str]:
    """Pull the three hook title lines from a generated episode description."""
    titles = []
    # Support both plain and emoji-prefixed markers
    marker_sets = [
        ["The Tea:", "The Therapy Session:", "The Personal:"],
        ["☕ The Tea:", "🛋️ The Therapy Session:", "💬 The Personal:"],
    ]
    for markers in marker_sets:
        found = []
        for marker in markers:
            if marker in description_text:
                after = description_text.split(marker, 1)[1]
                line = after.split("\n")[0].strip().strip('"')
                line = _strip_markdown(line)
                if line:
                    found.append(line)
        if found:
            return found
    return titles


def _parse_shorts_output(raw: str) -> dict:
    """Parse the three labelled blocks from the shorts generator output."""
    result = {"youtube_short": "", "tiktok": "", "instagram": ""}

    sections = {
        "youtube_short": "YOUTUBE_SHORT:",
        "tiktok":        "TIKTOK:",
        "instagram":     "INSTAGRAM:",
    }

    keys = list(sections.keys())
    for i, key in enumerate(keys):
        marker = sections[key]
        if marker not in raw:
            continue
        after = raw.split(marker, 1)[1].strip()
        # Content ends at the next section marker (or end of string)
        for j in range(i + 1, len(keys)):
            next_marker = sections[keys[j]]
            if next_marker in after:
                after = after.split(next_marker)[0]
                break
        result[key] = _strip_markdown(after.strip())

    return result
