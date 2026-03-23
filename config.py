import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_STYLE = {
    "font_name": "Arial",
    "font_size": 18,
    "primary_color": "&H00FFFFFF",    # white (ASS format: &HAABBGGRR)
    "highlight_color": "&H0000FFFF",  # yellow
    "bold": True,
    "margin_v": 40,                   # vertical margin from bottom edge (pixels)
    "producer_context": "",           # show/producer context used as Claude system prompt
}


def load() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            saved = json.load(f)
        # Merge with defaults so new keys are always present
        return {**DEFAULT_STYLE, **saved}
    return DEFAULT_STYLE.copy()


def save(style: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(style, f, indent=2)
