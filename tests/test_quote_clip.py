"""Quote-search logic — pure, no network/ffmpeg/GPU (imports stay light)."""
from pipeline.quote_clip import _norm_tokens, _slug, find_quote


def _transcript(sentence: str) -> dict:
    """Fake a transcribe() result: one word entry per token, 0.5s apart."""
    words = []
    for i, tok in enumerate(sentence.split()):
        words.append({"word": tok, "start": round(i * 0.5, 3), "end": round(i * 0.5 + 0.4, 3)})
    return {"text": sentence, "words": words}


TR = _transcript("well let me tell you the soup was absolutely disgusting folks")


def test_exact_match_spans_and_scores():
    hits = find_quote(TR, "the soup was absolutely disgusting")
    assert hits
    top = hits[0]
    assert top["exact"] is True
    assert top["score"] == 1.0
    assert top["text"] == "the soup was absolutely disgusting"
    assert top["start"] == 2.5           # "the" is index 5 -> 5*0.5
    assert top["end"] == 4.9             # "disgusting" is index 9 -> 9*0.5 + 0.4


def test_match_ignores_case_and_punctuation():
    tr = _transcript("Hey, the SOUP was... Disgusting!")
    hits = find_quote(tr, "the soup was disgusting")
    assert hits and hits[0]["exact"] is True


def test_fuzzy_fallback_on_small_misremember():
    # "the soup is absolutely disgusting" — "is" vs "was" — no exact hit
    hits = find_quote(TR, "the soup is absolutely disgusting")
    assert hits
    assert hits[0]["exact"] is False
    assert 0.6 <= hits[0]["score"] < 1.0
    assert "disgusting" in hits[0]["text"]


def test_no_match_returns_empty():
    assert find_quote(TR, "quantum chromodynamics lecture") == []
    assert find_quote(TR, "") == []
    assert find_quote({"words": []}, "anything") == []


def test_multiple_exact_matches_all_returned():
    tr = _transcript("cut it out cut it out please")
    hits = find_quote(tr, "cut it out")
    assert len(hits) == 2
    assert all(h["exact"] for h in hits)
    assert hits[0]["start"] < hits[1]["start"]


def test_max_results_caps_output():
    tr = _transcript(" ".join(["no"] * 20))
    hits = find_quote(tr, "no", max_results=3)
    assert len(hits) == 3


def test_fuzzy_windows_do_not_overlap():
    hits = find_quote(TR, "tell you the soup wa", min_score=0.5, max_results=5)
    spans = [(h["start"], h["end"]) for h in hits]
    for a in range(len(spans)):
        for b in range(a + 1, len(spans)):
            (s1, e1), (s2, e2) = spans[a], spans[b]
            assert e1 < s2 or e2 < s1, "returned windows overlap"


def test_norm_tokens_and_slug():
    assert _norm_tokens("Don't— you DARE!") == ["don't", "you", "dare"]
    assert _slug("The Soup Was Disgusting!") == "the-soup-was-disgusting"
    assert _slug("!!!") == "clip"
