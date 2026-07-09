"""Model selection + usage/cost logging — pure, no network."""
import json

from pipeline import llm


def test_default_model_and_env_override(monkeypatch):
    monkeypatch.delenv("STREAMTOOLS_CLAUDE_MODEL", raising=False)
    assert llm.model() == "claude-opus-4-8"
    monkeypatch.setenv("STREAMTOOLS_CLAUDE_MODEL", "claude-opus-4-7")
    assert llm.model() == "claude-opus-4-7"
    monkeypatch.setenv("STREAMTOOLS_CLAUDE_MODEL", "")   # empty -> default
    assert llm.model() == "claude-opus-4-8"


def test_call_cost_math():
    # 1M in + 1M out at Opus rates = $5 + $25
    assert llm.call_cost("claude-opus-4-8", 1_000_000, 1_000_000) == 30.0
    # unknown model falls back to default (Opus) pricing
    assert llm.call_cost("claude-mystery-9", 1_000_000, 0) == 5.0
    assert llm.call_cost("claude-haiku-4-5", 2_000_000, 0) == 2.0


class _Usage:
    input_tokens = 3000
    output_tokens = 500


def test_log_usage_and_cost_report(tmp_path, monkeypatch):
    log = tmp_path / "llm_usage.jsonl"
    monkeypatch.setattr(llm, "USAGE_LOG", str(log))

    llm.log_usage("find_clips", "claude-opus-4-7", _Usage())
    llm.log_usage("find_clips", "claude-opus-4-8", _Usage())
    llm.log_usage("describe", "claude-opus-4-8", _Usage())

    recs = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert len(recs) == 3
    assert recs[0]["input_tokens"] == 3000 and recs[0]["cost_usd"] > 0

    report = llm.cost_report()
    assert report["claude-opus-4-7"]["calls"] == 1
    assert report["claude-opus-4-8"]["calls"] == 2
    assert set(report["claude-opus-4-8"]["by_call"]) == {"find_clips", "describe"}
    # totals add up
    m8 = report["claude-opus-4-8"]
    assert m8["input_tokens"] == 6000 and m8["output_tokens"] == 1000


def test_log_usage_never_raises(monkeypatch):
    # point the log somewhere unwritable — logging must swallow the failure
    monkeypatch.setattr(llm, "USAGE_LOG", "Z:\\no\\such\\dir\\log.jsonl")
    llm.log_usage("find_clips", "claude-opus-4-8", object())   # no usage attrs either
