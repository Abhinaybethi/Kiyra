"""Retrieval post-processing: dedup, per-file diversity, compression (§8).

The chunker deliberately overlaps windows, and a single large file can dominate
a keyword match, so raw top-K spends the context budget on repeated evidence.
These tests pin the behaviour that keeps top-K *distinct*.
"""
import pytest

from github_repo.retrieval import GitHubRetriever, ProjectChunkHit


def hit(content, path="a.py", score=1.0):
    return ProjectChunkHit(content=content,
                           metadata={"file_path": path},
                           score=score)


def process(hits, top_k=5):
    return GitHubRetriever._post_process(hits, top_k)


# ── dedup ────────────────────────────────────────────────────────────────────
def test_near_duplicate_chunks_are_dropped():
    body = ("def login(user, password): return db.query(user).first() "
            "session cookie signed with jwt secret key rotation enabled")
    hits = [hit(body, "auth.py", 0.9), hit(body + " ", "auth.py", 0.88)]
    out = process(hits)
    assert len(out) == 1, "an identical repeat must not consume budget"


def test_similar_but_distinct_chunks_are_both_kept():
    a = ("def login(user, password): return db.query(user).first() "
         "session cookie signed with jwt")
    b = ("def logout(session): session.clear() revoke refresh token in redis "
         "audit log entry written to postgres")
    out = process([hit(a, "auth.py"), hit(b, "auth.py")])
    assert len(out) == 2


def test_post_process_ranks_by_score_first():
    """Whatever order the caller uses, the best-scoring evidence is kept."""
    lo = hit("alpha beta gamma delta epsilon zeta eta theta", "a.py", 0.4)
    hi = hit("iota kappa lambda mu nu xi omicron pi", "b.py", 0.9)
    out = process([lo, hi])
    assert [h.score for h in out] == [0.9, 0.4]


def test_short_chunks_are_not_wrongly_deduped():
    """Two tiny chunks share stop-ish tokens; they must both survive."""
    out = process([hit("def a(): return 1", "a.py"), hit("def b(): return 2", "b.py")])
    assert len(out) == 2


# ── per-file diversity ───────────────────────────────────────────────────────
def test_one_file_cannot_monopolise_the_budget():
    hits = [hit(f"token{i} content body padding words here number {i}",
                "same.py", 1.0 - i / 100) for i in range(5)]
    hits.append(hit("completely different topic content elsewhere",
                    "other.py", 0.1))
    out = process(hits, top_k=5)
    paths = [h.metadata["file_path"] for h in out]
    assert paths.count("same.py") <= GitHubRetriever._MAX_PER_FILE
    assert "other.py" in paths, "a second file must still get budget"


def test_per_file_cap_is_two_by_default():
    assert GitHubRetriever._MAX_PER_FILE == 2


# ── compression ──────────────────────────────────────────────────────────────
def test_long_chunk_is_trimmed_to_budget():
    long_text = "\n".join(f"line {i} of a very long source file" for i in range(200))
    out = process([hit(long_text)])
    assert len(out[0].content) <= GitHubRetriever._CHUNK_CHAR_BUDGET + 8
    assert out[0].content.endswith("...")
    assert out[0].content.startswith("line 0"), "keeps the chunk signature (head)"


def test_short_chunk_is_untouched():
    out = process([hit("def f():\n    return 1")])
    assert out[0].content == "def f():\n    return 1"


def test_compression_never_exceeds_budget():
    text = "x" * 5000  # a single unbroken line
    out = process([hit(text)])
    assert len(out[0].content) <= GitHubRetriever._CHUNK_CHAR_BUDGET + 8


# ── invariants ───────────────────────────────────────────────────────────────
def test_empty_input_is_safe():
    assert process([]) == []


def test_top_k_is_respected_after_filtering():
    hits = [hit(f"unique{i} words that differ completely from each other "
                f"token{i} padding", f"f{i}.py") for i in range(10)]
    out = process(hits, top_k=3)
    assert len(out) == 3


def test_post_process_is_idempotent():
    """Re-processing already-cleaned hits must not drop good evidence."""
    hits = [hit("def a(): return 1", "a.py"), hit("def b(): return 2", "b.py")]
    once = process(hits)
    twice = process(once)
    assert len(once) == len(twice) == 2


def test_dedup_keeps_higher_scoring_duplicate():
    """When two chunks are duplicates, the better-scoring one survives."""
    body = ("shared signature body tokens for duplicate detection purposes "
            "with enough words to clear the overlap threshold reliably")
    out = process([hit(body, "a.py", 0.2), hit(body, "b.py", 0.9)])
    assert len(out) == 1
    assert out[0].score == 0.9