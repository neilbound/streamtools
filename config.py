import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_STYLE = {
    "font_name": "Montserrat",
    "font_size": 108,
    "primary_color": "&H00FFFFFF",    # white — unspoken text (ASS format: &HAABBGGRR)
    "highlight_color": "&H0000C8FF",  # warm yellow — active word (RGB 255,200,0 → ASS BGR)
    "bold": True,
    "margin_v": 856,                  # vertical position; 960 = center of 1920px frame
}

_STYLE_KEYS = set(DEFAULT_STYLE.keys())


DEFAULT_BRAND = {
    "show_name":     "",
    "apple_podcasts": "",
    "spotify":       "",
    "youtube":       "",
    "instagram":     "",
    "tiktok":        "",
    "website":       "",
    "patreon":       "",
    "cta":           "Drop your thoughts in the comments — this conversation is just getting started.",
    # YouTube playlist IDs — auto-assigned on upload (optional, leave blank to skip)
    "youtube_playlist_shorts":  "",   # e.g. "PLrIMR0zBqvr2RTuW8wBUzFbpGIgl5Jlby"
    "youtube_playlist_podcast": "",   # e.g. "PLrIMR0zBqvr2YJal42NCH8kFzt3-t9aA7"
}

_BRAND_KEYS = set(DEFAULT_BRAND.keys())


DEFAULT_PIPELINE = {
    # Publishing channel used when a caller doesn't pass one explicitly.
    "default_channel": "neilbound",
    # Posting-time rotation (hours in UTC). The scheduler cycles through this
    # list so consecutive posts don't all land at the same time / look automated.
    # Defaults: 12pm, 6pm, 9am EST (EDT = UTC-4).
    "posting_slots_utc": [16, 22, 13],
    # Prefixes stripped from StreamYard segment filenames to produce clean
    # segment labels, e.g. "Age Of Attraction - Season 1 - ". Longest match wins.
    # Per-show — leave empty to use the raw filename (minus extension) as the label.
    "segment_label_prefixes": [],
}

_PIPELINE_KEYS = set(DEFAULT_PIPELINE.keys())


def _default_profile() -> dict:
    return {
        "producer_context": "",
        "brand": DEFAULT_BRAND.copy(),
        "pipeline": DEFAULT_PIPELINE.copy(),
        **DEFAULT_STYLE,
    }


def _ensure_profile_style(profile) -> dict:
    """Ensure a profile dict has all style, brand, and pipeline keys (handles old formats)."""
    if isinstance(profile, str):
        # Migrate old format where profile was just a context string
        return {
            "producer_context": profile,
            "brand": DEFAULT_BRAND.copy(),
            "pipeline": DEFAULT_PIPELINE.copy(),
            **DEFAULT_STYLE,
        }
    result = {**_default_profile(), **profile}
    # Ensure brand and pipeline sub-dicts exist and have all keys
    result["brand"]    = {**DEFAULT_BRAND,    **result.get("brand", {})}
    result["pipeline"] = {**DEFAULT_PIPELINE, **result.get("pipeline", {})}
    return result


def load() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)

        # Migrate legacy top-level producer_context
        if "producer_context" in saved and "profiles" not in saved:
            legacy_context = saved.pop("producer_context")
            # Collect any top-level style keys that were saved
            legacy_style = {k: saved.pop(k) for k in list(saved.keys()) if k in _STYLE_KEYS}
            saved["profiles"] = {"Default": {"producer_context": legacy_context, **DEFAULT_STYLE, **legacy_style}}
            saved["active_profile"] = "Default"

        if "profiles" not in saved:
            saved["profiles"] = {"Default": _default_profile()}
            saved["active_profile"] = "Default"

        # Ensure every profile has all style keys
        saved["profiles"] = {
            name: _ensure_profile_style(p)
            for name, p in saved["profiles"].items()
        }

        if "active_profile" not in saved:
            saved["active_profile"] = list(saved["profiles"].keys())[0]

        return saved

    return {"profiles": {"Default": _default_profile()}, "active_profile": "Default"}


def save(cfg: dict) -> None:
    # ensure_ascii=False keeps non-ASCII (curly quotes, etc.) readable; utf-8 so it
    # round-trips on Windows (default cp1252 can't decode them on the next load()).
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def active_profile(cfg: dict) -> dict:
    """Return the full profile dict for the active show."""
    return cfg["profiles"].get(cfg.get("active_profile", "Default"), _default_profile())


def active_context(cfg: dict) -> str:
    """Return the producer context string for the active profile."""
    return active_profile(cfg).get("producer_context", "")


def active_style(cfg: dict) -> dict:
    """Return caption style settings for the active profile."""
    p = active_profile(cfg)
    return {k: p.get(k, DEFAULT_STYLE[k]) for k in _STYLE_KEYS}


def active_brand(cfg: dict) -> dict:
    """Return brand/links settings for the active profile."""
    p = active_profile(cfg)
    return {**DEFAULT_BRAND, **p.get("brand", {})}


def active_pipeline(cfg: dict) -> dict:
    """Return pipeline settings (channel, posting slots, label prefixes) for the active profile."""
    p = active_profile(cfg)
    return {**DEFAULT_PIPELINE, **p.get("pipeline", {})}
