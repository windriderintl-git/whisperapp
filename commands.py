"""Voice commands: pull inline/trailing control phrases out of a transcript.

Runs on the RAW transcript, before LLM polish. Speech-to-text produces plain
words, so the only way to "press enter" or "scratch that" hands-free is to say
it out loud — this module recognises those phrases and turns them into structured
intent the integrator can act on (substitute text, then press keys, or drop).

Why before polish: an LLM would happily rewrite "press enter" into prose or add
punctuation, so command detection has to happen while the words are still literal.
We still tolerate stray punctuation/casing here because a re-run over polished
text (or a chatty ASR model) may sprinkle some in.

Pure logic, no I/O — safe to unit test in isolation.
"""
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("whisper2.commands")


@dataclass
class ParsedDictation:
    """Result of parsing one utterance.

    text:          transcript after inline substitution + trailing-command removal.
    trailing_keys: keys to press AFTER pasting text, in spoken order (e.g. ["enter"]).
    drop:          True when the whole utterance was a cancel command; text is "".
    """
    text: str
    trailing_keys: list[str] = field(default_factory=list)
    drop: bool = False


# --- Built-in phrase tables (overridable via config) ------------------------
# Kept as plain module-level constants so they're trivial to read and extend.

# Trailing phrases that mean "submit this" -> press Enter after paste.
DEFAULT_SUBMIT_PHRASES = [
    "press enter",
    "hit enter",
    "new line and send",
    "send message",
    "send it",
    "send",
    "submit",
]

# Other trailing keypress phrases -> key name. Enter is handled via submit list.
DEFAULT_TRAILING_KEY_PHRASES = {
    "press tab": "tab",
    "press escape key": "esc",
    "press escape": "esc",
}

# Whole-utterance cancels: if the entire (punctuation-stripped) utterance is one
# of these, discard everything.
DEFAULT_CANCEL_PHRASES = [
    "scratch that",
    "cancel that",
    "delete that",
    "never mind",
    "nevermind",
]

# Inline formatting: spoken phrase -> literal substitution, applied anywhere.
# Longest first so "new paragraph" wins before "new line" could interfere.
DEFAULT_INLINE_SUBSTITUTIONS = [
    ("new paragraph", "\n\n"),
    ("new line", "\n"),
]

# Trailing junk (punctuation/quotes/whitespace) an LLM or ASR may append after a
# command word. Stripped when matching so "Press enter." still counts.
_TRAILING_JUNK = r"[\s.!?,;:\"')\]]*"


def _phrase_body(phrase: str) -> str:
    """Regex for a spoken phrase with word boundaries and flexible whitespace."""
    return r"\b" + r"\s+".join(re.escape(w) for w in phrase.split()) + r"\b"


def _compile_trailing(phrase: str) -> re.Pattern:
    """Anchor a phrase to the END of the string, eating leading space + trailing junk.

    The leading \\s* lets us also remove the space that used to separate the
    command from real text ("hello send" -> "hello"), while any preceding
    punctuation ("hello. send") is intentionally left intact.
    """
    return re.compile(r"\s*" + _phrase_body(phrase) + _TRAILING_JUNK + r"$", re.IGNORECASE)


def _compile_inline(phrase: str) -> re.Pattern:
    return re.compile(_phrase_body(phrase), re.IGNORECASE)


# Precompile the built-ins; config overrides recompile lazily inside parse().
_BUILTIN_INLINE = [(_compile_inline(p), repl) for p, repl in DEFAULT_INLINE_SUBSTITUTIONS]


def _normalize(s: str) -> str:
    """Lowercase, drop non-alphanumerics, collapse whitespace — for cancel match."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip().lower()


def parse(text: str, config: dict | None = None) -> ParsedDictation:
    """Extract voice commands from a raw transcript.

    Order matters: whole-utterance cancel first, then trailing keypress commands
    (so "new line and send" is treated as submit, not an inline newline), then
    inline formatting substitutions. Never raises — odd input returns the text
    unchanged.
    """
    if text is None:
        return ParsedDictation(text=text)
    config = config or {}
    if not config.get("enabled", True):
        return ParsedDictation(text=text)
    if not text.strip():
        return ParsedDictation(text=text)

    try:
        return _parse(text, config)
    except Exception as e:  # dictation must never crash on a parsing quirk
        log.warning(f"[commands] parse failed, passing text through: {e}")
        return ParsedDictation(text=text)


def _parse(text: str, config: dict) -> ParsedDictation:
    submit_phrases = config.get("submit_phrases") or DEFAULT_SUBMIT_PHRASES
    cancel_phrases = config.get("cancel_phrases") or DEFAULT_CANCEL_PHRASES

    # 1) Whole-utterance cancel: the entire thing is a discard command.
    cancel_set = {_normalize(p) for p in cancel_phrases}
    if _normalize(text) in cancel_set:
        log.debug("[commands] cancel utterance dropped")
        return ParsedDictation(text="", drop=True)

    # 2) Trailing keypress commands. Submit phrases map to Enter; the rest carry
    #    their own key. Longest phrases first so "send message" beats "send".
    trailing_map: list[tuple[re.Pattern, str]] = []
    ordered = sorted(
        [(p, "enter") for p in submit_phrases]
        + list(DEFAULT_TRAILING_KEY_PHRASES.items()),
        key=lambda pk: len(pk[0]),
        reverse=True,
    )
    for phrase, key in ordered:
        trailing_map.append((_compile_trailing(phrase), key))

    keys: list[str] = []
    # Strip repeatedly so "press tab press enter" yields both, in spoken order.
    stripping = True
    while stripping and text.strip():
        stripping = False
        for pat, key in trailing_map:
            m = pat.search(text)
            if m:
                keys.append(key)
                text = text[: m.start()]
                stripping = True
                break
    keys.reverse()  # we peeled from the end; restore spoken (press) order

    # 3) Inline formatting substitutions anywhere in the remaining text.
    #    (inline set isn't config-overridable — the built-ins cover the cases.)
    for pat, repl in _BUILTIN_INLINE:
        text = pat.sub(repl, text)

    return ParsedDictation(text=text, trailing_keys=keys)
