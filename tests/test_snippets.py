"""Tests for snippets.expand — assert-based, runs under pytest or standalone."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from snippets import expand


def test_basic_expansion():
    assert expand("my email", {"my email": "robert@windrider.com"}) == "robert@windrider.com"
    assert expand("send to my email please",
                  {"my email": "robert@windrider.com"}) == "send to robert@windrider.com please"


def test_case_insensitive():
    assert expand("My Email", {"my email": "x@y.com"}) == "x@y.com"
    assert expand("MY EMAIL now", {"my email": "x@y.com"}) == "x@y.com now"


def test_word_boundary_no_partial_match():
    # "cat" must not fire inside "category".
    assert expand("category", {"cat": "DOG"}) == "category"
    # But a standalone word does fire.
    assert expand("the cat sat", {"cat": "DOG"}) == "the DOG sat"


def test_longest_first_precedence():
    snippets = {"my email": "SHORT", "my work email": "LONG"}
    assert expand("use my work email", snippets) == "use LONG"
    assert expand("use my email", snippets) == "use SHORT"


def test_empty_and_none_dict():
    assert expand("hello", None) == "hello"
    assert expand("hello", {}) == "hello"
    assert expand("", {"a": "b"}) == ""


def test_multiple_triggers_in_one_string():
    snippets = {"my email": "e@x.com", "my phone": "555-1234"}
    assert expand("email my email or call my phone",
                  snippets) == "email e@x.com or call 555-1234"


def test_replacement_with_punctuation_and_symbols():
    out = expand("insert sig", {"sig": "Best,\nRobert — CEO (100% real) $$$"})
    assert out == "insert Best,\nRobert — CEO (100% real) $$$"
    # Backslashes / group-ref-like text must be inserted verbatim, not interpreted.
    assert expand("code", {"code": r"path\to\file \1 \g<0>"}) == r"path\to\file \1 \g<0>"


def test_odd_input_does_not_raise():
    assert expand("value 42 here", {42: "NUM"}) == "value NUM here"  # non-str key coerced
    assert expand("say tag", {"tag": None}) == "say None"            # non-str value coerced


def test_non_word_boundary_triggers():
    # Trigger with leading symbol: no \b anchor, plain match.
    assert expand("go @home now", {"@home": "HOUSE"}) == "go HOUSE now"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK")
