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


def _default_profile() -> dict:
    return {"producer_context": "", **DEFAULT_STYLE}


def _ensure_profile_style(profile) -> dict:
    """Ensure a profile dict has all style keys (handles old string format and missing keys)."""
    if isinstance(profile, str):
        # Migrate old format where profile was just a context string
        return {"producer_context": profile, **DEFAULT_STYLE}
    return {**_default_profile(), **profile}


def load() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
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
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


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
