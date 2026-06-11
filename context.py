"""Active-window detection -> prompt selection.

Identifies the foreground window by BOTH its process executable name and its
window title, then matches against process rules first (most reliable) and
title rules as a fallback (for browser-hosted apps like Gmail-in-Chrome).

This catches terminals and CLIs reliably — PowerShell, cmd, Windows Terminal,
WSL etc. usually only show a path in the title bar, so title matching alone
defaulted them to `cleanup_default`.
"""
import os
import re
import sys

import logging
log = logging.getLogger("whisper2.context")

DEFAULT_PROMPT = "cleanup_default"

# Process-name rules — matched against the basename of the foreground window's
# exe (case-insensitive, .exe stripped). First match wins.
_PROCESS_RULES: list[tuple[re.Pattern, str]] = [
    # Chat / messaging desktop apps.
    (re.compile(
        r"^(slack|discord|teams|ms-teams|telegram|whatsapp|signal|messenger)\b",
        re.I), "cleanup_chat"),
    # Email desktop apps.
    (re.compile(
        r"^(outlook|hxoutlook|thunderbird|mailbird|spark|protonmail|mailspring|airmail)\b",
        re.I), "cleanup_email"),
    # Code editors / IDEs.
    (re.compile(
        r"^(code|cursor|windsurf|pycharm|idea|webstorm|goland|rider|clion|rustrover|"
        r"sublime_text|sublime|atom|nvim|vim|gvim|zed|xcode|notepad\+\+|"
        r"androidstudio|datagrip|phpstorm|rubymine)\b",
        re.I), "cleanup_code"),
    # Terminals + shells -> default cleanup. Terminals run too many things
    # (chat with AI agents, git commits, shell, prompts) to assume "code".
    # cleanup_default preserves voice without over-condensing.
    (re.compile(
        r"^(windowsterminal|wt|cmd|conhost|powershell|pwsh|wsl|bash|mintty|"
        r"conemu|conemu64|hyper|alacritty|kitty|tabby|fluent\s*terminal)\b",
        re.I), "cleanup_default"),
]

# Title rules — fallback for browser-hosted webapps (Gmail in Chrome, Slack web, etc.).
_TITLE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(
        r"\b(Slack|Discord|Microsoft Teams|Telegram|WhatsApp|Signal|Messenger)\b",
        re.I), "cleanup_chat"),
    (re.compile(
        r"\b(Outlook|Gmail|Inbox|Thunderbird|Mailbird|Spark|ProtonMail|Fastmail)\b",
        re.I), "cleanup_email"),
    (re.compile(
        r"\b(Visual Studio Code|VSCode|Cursor|Windsurf|PyCharm|IntelliJ IDEA|"
        r"WebStorm|GoLand|Rider|CLion|RustRover|Sublime Text|Atom|Neovim|Zed|Xcode)\b",
        re.I), "cleanup_code"),
    # Community / social platforms — browser-hosted. Long-form posts, comments,
    # DMs all benefit from conversational cleanup vs. strict default.
    # Circle: matches "... | Circle", "Circle | ...", "Circle Community", or
    # "Foo Circle -" style — boundary chars required to avoid matching
    # unrelated words like "Circle K" in random titles.
    (re.compile(
        r"\bCircle\b(\s*[-—|·]|\s+Community|\s*$)|[-—|·]\s*Circle\b",
        re.I), "cleanup_chat"),
    (re.compile(
        r"\b(LinkedIn|Reddit|Facebook|Substack|Mastodon|Threads)\b",
        re.I), "cleanup_chat"),
    # Twitter/X: bare "X" is too loose, so require explicit boundaries —
    # "Twitter", "X.com", "on X", or "| X" / "/ X" at end of title.
    (re.compile(
        r"\bTwitter\b|\bX\.com\b|/\s*X\s*$|\|\s*X\s*$|\bon X\b",
        re.I), "cleanup_chat"),
]


def get_active_window_info() -> tuple[str, str]:
    """Returns (process_basename_no_ext, window_title). Empty strings on failure."""
    if not sys.platform.startswith("win"):
        return "", ""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "", ""

        # Window title.
        title_len = user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(title_len + 1)
        user32.GetWindowTextW(hwnd, title_buf, title_len + 1)
        title = title_buf.value or ""

        # Process exe path.
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        proc = ""
        if h:
            try:
                buf = ctypes.create_unicode_buffer(512)
                size = wintypes.DWORD(512)
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    proc = os.path.basename(buf.value)
                    if proc.lower().endswith(".exe"):
                        proc = proc[:-4]
            finally:
                kernel32.CloseHandle(h)
        return proc, title
    except Exception as e:
        log.warning(f"[ctx] window query failed: {e}")
        return "", ""


# Title rules that should be checked BEFORE process rules.
# Use case: an AI CLI is running inside a terminal — the process is
# WindowsTerminal/pwsh/etc., but the user is having a conversation,
# not writing code, so we want cleanup_chat, not cleanup_default.
_TITLE_FIRST_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bClaude Code\b|\bCodex CLI\b|\bAider\b|\bGemini CLI\b", re.I),
     "cleanup_chat"),
]


def select_prompt_for(proc: str, title: str, override: str | None = None) -> str:
    """Pure rule matching on an already-captured (proc, title) pair, so the
    window can be sampled at recording start (when the user was focused on
    the target app) rather than at transcription time."""
    if override:
        return override
    for pattern, prompt in _TITLE_FIRST_RULES:
        if pattern.search(title):
            return prompt
    for pattern, prompt in _PROCESS_RULES:
        if pattern.search(proc):
            return prompt
    for pattern, prompt in _TITLE_RULES:
        if pattern.search(title):
            return prompt
    return DEFAULT_PROMPT


def select_prompt(override: str | None = None) -> tuple[str, str]:
    """Live-query convenience wrapper. Returns (prompt_name, source_description)."""
    if override:
        return override, "(override)"
    proc, title = get_active_window_info()
    src = f"{proc or '?'} | {title[:60]}" if (proc or title) else "(no window)"
    return select_prompt_for(proc, title), src
