"""Tests for answer post-processing (markdown decoration -> speakable text).

The overlay renders the answer verbatim in a Tk Text widget, so any markdown
the model emits shows up as literal asterisks/hashes. Detailed mode makes the
model label its five parts ("**Thesis:**", "**Trade-off:**"), which is where
this came from: 204 bold labels across a 40-answer run.
"""
from __future__ import annotations

import pytest

from realtime.formatting import plain_answer


def test_bold_labels_keep_their_words_and_lose_the_asterisks():
    text = (
        "**Thesis:** Node.js runs JavaScript outside the browser.\n\n"
        "**How it actually works:** libuv hands I/O to the thread pool.\n\n"
        "**Trade-off:** CPU-bound work stalls the loop."
    )
    out = plain_answer(text)
    assert "**" not in out
    assert out.startswith("Thesis: Node.js runs JavaScript")
    assert "How it actually works: libuv" in out
    assert "Trade-off: CPU-bound work" in out


def test_headings_and_bullets_are_normalized():
    text = (
        "## Event loop\n"
        "- one call stack\n"
        "* a task queue\n"
        "__libuv__ handles I/O"
    )
    out = plain_answer(text)
    assert out == "Event loop\n- one call stack\n- a task queue\nlibuv handles I/O"


def test_inline_code_backticks_are_removed_but_words_kept():
    out = plain_answer("Call `res.json()` after `next()`.")
    assert out == "Call res.json() after next()."


def test_fenced_code_blocks_are_left_byte_for_byte():
    answer = (
        "Use a stream:\n\n"
        "```javascript\n"
        "# this is a comment, not a heading\n"
        "const s = fs.createReadStream('a'); // ** not bold **\n"
        "```\n\n"
        "**That** is the point."
    )
    out = plain_answer(answer)
    assert "```javascript" in out
    assert "# this is a comment, not a heading" in out
    assert "// ** not bold **" in out
    assert "**That**" not in out
    assert "That is the point." in out


def test_unterminated_fence_is_preserved_rather_than_eaten():
    out = plain_answer("```python\nprint('hi')\n# trailing comment")
    assert out.startswith("```python\nprint('hi')")
    assert "# trailing comment" in out


def test_stray_markers_and_blank_runs_are_collapsed():
    out = plain_answer("one\n\n\n\ntwo ** three")
    assert out == "one\n\ntwo three"


def test_plain_text_is_unchanged_and_function_is_idempotent():
    text = "Node.js uses a single-threaded event loop.\n\n- libuv\n- V8"
    assert plain_answer(text) == text
    assert plain_answer(plain_answer(text)) == plain_answer(text)


def test_hash_without_a_space_is_not_treated_as_a_heading():
    assert plain_answer("#1 reason to stream") == "#1 reason to stream"
    assert plain_answer("### Streams\nflow") == "Streams\nflow"


@pytest.mark.parametrize("value", ["", None])
def test_empty_input_is_passed_through(value):
    assert plain_answer(value) in ("", None)


def test_never_raises_on_odd_input():
    # A formatting helper must not be able to lose an answer.
    assert plain_answer("**unbalanced bold") == "unbalanced bold"
    assert plain_answer("__also unbalanced") == "also unbalanced"
