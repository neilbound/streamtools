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

Your output must follow this EXACT structure — no deviations, no extra sections:

---
1. THE HOOK TITLE TRIO
Three title variants for different audiences:
☕ The Tea:        (provocative/gossipy angle)
🛋️ The Therapy Session: (emotional/introspective angle)
💬 The Personal:   (first-person/relatable angle)

2. THE DESCRIPTION
The Hook:
[1-2 punchy sentences that draw the listener in. Start with a question or a bold claim.]

The Deep Dive:
[2 rich paragraphs expanding on both hosts' perspectives. Name the hosts specifically. Capture the emotional texture — what they said AND what it meant. Do not summarise blandly.]

The Highlights — Must-Hear Moments:
[4-5 bullet points with emoji icons. Each bullet = a memorable, specific moment. Format: emoji "quoted pull-quote or vivid description" — 1 sentence of context.]

3. THE STANDARD SIGNATURE BLOCK
{signature}

Have a dating story or a question for our next therapy-inspired deep dive? Drop it in the comments below!
---

Tone: Conversational but thoughtful. Emotionally specific. Never corporate. Use em dashes, italics energy, and the hosts' actual names."""


def _shorts_system_prompt(brand: dict) -> str:
    show = brand.get("show_name") or "the podcast"
    youtube_url = brand.get("youtube", "")
    instagram = brand.get("instagram", "")
    tiktok = brand.get("tiktok", "")
    if tiktok and not tiktok.startswith("@"):
        tiktok = f"@{tiktok}"

    follow_line = " | ".join(filter(None, [
        f"Full ep on YouTube 👉 {youtube_url}" if youtube_url else "",
        f"Instagram: {instagram}" if instagram else "",
        f"TikTok: {tiktok}" if tiktok else "",
    ]))

    return f"""You are the content writer for {show}. Write short-form social media copy for a podcast clip.

You will receive the clip transcript and episode context. Generate THREE pieces of copy:

---
YOUTUBE_SHORT:
[2-3 sentence description. Hook in the first line. End with: "{follow_line}" and "#Shorts #Podcast"]

TIKTOK:
[Single punchy caption under 150 chars. Conversational, curiosity-driven. Include 3-5 hashtags.]

INSTAGRAM:
[Caption with an engaging opening line, 2-3 sentences of context, a question to drive comments, then 8-12 relevant hashtags on a new line. End with "Link in bio for the full episode."]
---

Output ONLY the three labelled blocks above. No intro, no commentary."""


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

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )

    full_text = message.content[0].text.strip()

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

    client = anthropic.Anthropic(api_key=api_key)
    system = _shorts_system_prompt(brand)

    clip_text = " ".join(w["word"] for w in clip_words).strip()

    user_prompt = f"""Clip title: {clip_title}
{f'Episode context: {episode_context}' if episode_context else ''}

Clip transcript:
{clip_text}

Write the YouTube Short, TikTok, and Instagram descriptions for this clip."""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=600,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = message.content[0].text.strip()
    return _parse_shorts_output(raw)


# ── Output parsers ─────────────────────────────────────────────────────────────

def _extract_title_options(description_text: str) -> list[str]:
    """Pull the three hook title lines from a generated episode description."""
    titles = []
    markers = ["☕ The Tea:", "🛋️ The Therapy Session:", "💬 The Personal:"]
    for marker in markers:
        if marker in description_text:
            after = description_text.split(marker, 1)[1]
            line = after.split("\n")[0].strip().strip('"')
            if line:
                titles.append(line)
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
        result[key] = after.strip()

    return result
