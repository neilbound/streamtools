"""Claude model selection + usage/cost logging for pipeline LLM calls.

One switch for every Claude call in the pipeline (clip finding, descriptions),
plus an append-only usage log so model costs can be compared with real numbers.

NOTE: Opus 4.7 and 4.8 share the same sticker price ($5/$25 per MTok) — a
"cheaper" model only materializes if it *uses* fewer tokens per call. That's
exactly what this log measures: run an episode on each model and compare.

Switch models per run via `STREAMTOOLS_CLAUDE_MODEL` (e.g. in .env):
    STREAMTOOLS_CLAUDE_MODEL=claude-opus-4-8
"""
import json
import os
import time

DEFAULT_MODEL = "claude-opus-4-7"

# $/MTok (input, output). Keep in sync with platform.claude.com/docs pricing.
PRICES = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

USAGE_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output", "analytics", "llm_usage.jsonl",
)


def model() -> str:
    """The Claude model for pipeline calls (env override → default)."""
    return os.environ.get("STREAMTOOLS_CLAUDE_MODEL") or DEFAULT_MODEL


def call_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost of one call at the model's published per-MTok rates."""
    pin, pout = PRICES.get(model_id, PRICES[DEFAULT_MODEL])
    return round(input_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6)


def log_usage(call: str, model_id: str, usage) -> None:
    """Append one usage record per API call. Logging must never break a run —
    any failure here is swallowed (the clip pipeline matters more than the log)."""
    try:
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "call": call,
            "model": model_id,
            "input_tokens": inp,
            "output_tokens": out,
            "cost_usd": call_cost(model_id, inp, out),
        }
        os.makedirs(os.path.dirname(USAGE_LOG), exist_ok=True)
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def cost_report() -> dict:
    """Aggregate the usage log by model: calls, tokens, cost. The side-by-side
    for 'did 4.8 actually cost more than 4.7 on a comparable run?'"""
    by_model: dict[str, dict] = {}
    if not os.path.exists(USAGE_LOG):
        return by_model
    with open(USAGE_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = by_model.setdefault(rec.get("model", "?"), {
                "calls": 0, "input_tokens": 0, "output_tokens": 0,
                "cost_usd": 0.0, "by_call": {},
            })
            m["calls"] += 1
            m["input_tokens"] += rec.get("input_tokens", 0)
            m["output_tokens"] += rec.get("output_tokens", 0)
            m["cost_usd"] = round(m["cost_usd"] + rec.get("cost_usd", 0.0), 6)
            c = m["by_call"].setdefault(rec.get("call", "?"), {"calls": 0, "cost_usd": 0.0})
            c["calls"] += 1
            c["cost_usd"] = round(c["cost_usd"] + rec.get("cost_usd", 0.0), 6)
    return by_model
