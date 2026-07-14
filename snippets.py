"""Voice snippets/macros: spoken trigger phrases expand to canned text.

Runs after LLM polish, alongside apply_vocabulary. A trigger like "my email"
becomes "robert@windrider.com" so users don't have to dictate boilerplate.
Pure logic, no I/O — safe to unit test in isolation.
"""
import logging
import re

log = logging.getLogger("whisper2.snippets")


def expand(text: str, snippets: dict[str, str] | None) -> str:
    """Replace whole-phrase trigger occurrences in `text` with their expansions.

    Matches case-insensitively on word boundaries so a trigger never fires
    inside a larger word. Triggers are applied longest-first so a specific
    phrase ("my work email") wins over a shorter prefix ("my email").
    Replacement text is inserted verbatim. Never raises on odd input.
    """
    if not text or not snippets:
        return text

    # Coerce/skip odd keys+values so a bad config entry can't crash dictation.
    clean: dict[str, str] = {}
    for trigger, replacement in snippets.items():
        try:
            trigger = str(trigger)
            replacement = str(replacement)
        except Exception:
            continue
        if trigger:
            clean[trigger] = replacement
    if not clean:
        return text

    # Longest-first: guarantees "my work email" is tried before "my email".
    for trigger in sorted(clean, key=len, reverse=True):
        # \b only anchors on word chars; for triggers that start/end with a
        # non-word char (e.g. "@home") fall back to a plain escaped match.
        left = r"\b" if trigger[0].isalnum() or trigger[0] == "_" else ""
        right = r"\b" if trigger[-1].isalnum() or trigger[-1] == "_" else ""
        pattern = re.compile(left + re.escape(trigger) + right, re.I)
        # re.sub treats backslashes/group refs in the replacement specially;
        # a lambda inserts it truly verbatim.
        new_text, n = pattern.subn(lambda _m: clean[trigger], text)
        if n:
            log.debug(f"[snippet] expanded {trigger!r} x{n}")
            text = new_text
    return text
