"""Tests for commands.parse — assert-based, runs under pytest or standalone."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from commands import parse


def test_trailing_enter():
    r = parse("send the report press enter")
    assert r.text == "send the report"
    assert r.trailing_keys == ["enter"]
    assert r.drop is False


def test_trailing_enter_with_punctuation():
    # An LLM/ASR may capitalize and add a period; still recognised.
    r = parse("Tell them it's ready. Press enter.")
    assert r.text == "Tell them it's ready."
    assert r.trailing_keys == ["enter"]


def test_trailing_send_variants():
    assert parse("ship it now send it").trailing_keys == ["enter"]
    assert parse("okay submit").trailing_keys == ["enter"]
    assert parse("here you go send message").trailing_keys == ["enter"]
    assert parse("new line and send").trailing_keys == ["enter"]


def test_trailing_tab_and_escape():
    assert parse("first name press tab").trailing_keys == ["tab"]
    assert parse("done press escape").trailing_keys == ["esc"]
    assert parse("done press escape key").trailing_keys == ["esc"]


def test_multiple_trailing_keys_in_spoken_order():
    r = parse("username press tab press enter")
    assert r.text == "username"
    assert r.trailing_keys == ["tab", "enter"]


def test_inline_new_line():
    r = parse("line one new line line two")
    assert r.text == "line one \n line two"
    assert r.trailing_keys == []


def test_inline_new_paragraph():
    # "new paragraph" must win over the shorter "new line".
    assert parse("intro new paragraph body").text == "intro \n\n body"


def test_scratch_that_drops():
    for phrase in ["scratch that", "Scratch that.", "cancel that",
                   "delete that", "never mind", "Nevermind!"]:
        r = parse(phrase)
        assert r.drop is True, phrase
        assert r.text == ""


def test_mixed_text_and_trailing_send():
    r = parse("Hey team, deploy is live new line thanks send")
    assert r.text == "Hey team, deploy is live \n thanks"
    assert r.trailing_keys == ["enter"]


def test_send_midsentence_not_stripped():
    # "send" only counts as a command when it's the final word(s).
    r = parse("please send the invoice to accounting")
    assert r.text == "please send the invoice to accounting"
    assert r.trailing_keys == []


def test_send_inside_word_not_matched():
    r = parse("I will resend")
    assert r.text == "I will resend"
    assert r.trailing_keys == []


def test_disabled_config_passes_through():
    r = parse("do this press enter", {"enabled": False})
    assert r.text == "do this press enter"
    assert r.trailing_keys == []
    assert r.drop is False


def test_plain_text_unchanged():
    r = parse("just a normal sentence with nothing special")
    assert r.text == "just a normal sentence with nothing special"
    assert r.trailing_keys == []
    assert r.drop is False


def test_empty_and_whitespace_unchanged():
    assert parse("").text == ""
    assert parse("   ").text == "   "
    assert parse(None).text is None


def test_config_overrides_phrases():
    cfg = {"submit_phrases": ["go now"], "cancel_phrases": ["forget it"]}
    assert parse("email them go now", cfg).trailing_keys == ["enter"]
    assert parse("forget it", cfg).drop is True
    # Built-in "press enter" no longer submits once submit_phrases is overridden,
    # but "press tab" still works (it's a separate built-in map).
    assert parse("do it press enter", cfg).trailing_keys == []
    assert parse("field press tab", cfg).trailing_keys == ["tab"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK")
