"""Unit tests for transcript aggregation (fragment merge / dedup / bounds)."""
from realtime.transcript import TranscriptBuffer, normalize


def test_normalize_whitespace():
    assert normalize("  Can   you   explain   caching?   ") == "Can you explain caching?"


def test_fragments_merge_into_utterance():
    tb = TranscriptBuffer()
    tb.add_fragment("Can you explain", ts=1.0)
    tb.add_fragment("how Redis caching works", ts=1.1)
    assert tb.current_utterance == "Can you explain how Redis caching works"
    assert len(tb.entries) == 2


def test_duplicate_fragment_dropped():
    tb = TranscriptBuffer()
    tb.add_fragment("Can you explain how Redis caching works", ts=1.0)
    entry = tb.add_fragment("Can you explain", ts=1.2)  # substring of current
    assert entry is None
    tb.add_fragment("how Redis caching works", ts=1.3)  # still contained
    assert tb.current_utterance == "Can you explain how Redis caching works"


def test_finalize_resets_utterance_scratch():
    tb = TranscriptBuffer()
    tb.add_fragment("Explain event loop", ts=1.0)
    finalized = tb.finalize_utterance()
    assert finalized == "Explain event loop"
    assert tb.current_utterance == ""


def test_recent_and_bounds():
    tb = TranscriptBuffer(max_entries=4)
    for i in range(10):
        tb.add_fragment(f"fragment number {i}", ts=float(i))
    recent = tb.recent(4)
    assert len(recent) == 4
    assert "fragment number 9" in recent[-1].text
    assert len(tb.entries) == 4  # bounded deque


def test_as_text_contains_latest():
    tb = TranscriptBuffer()
    for i in range(5):
        tb.add_fragment(f"utterance {i}", ts=float(i))
    text = tb.as_text(max_entries=3)
    lines = text.splitlines()
    assert len(lines) == 3
    assert "utterance 4" in lines[-1]
    assert lines[0] == "utterance 0 utterance 1 utterance 2"