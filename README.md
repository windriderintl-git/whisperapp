# Whisper 2.0 — Realtime Dictation

Local push-to-talk dictation with LLM polish and context-aware tone, all offline. No subscriptions, no cloud calls.

## What it does

1. You hold (or double-tap) a hotkey, speak, release.
2. `faster-whisper` transcribes locally.
3. A local Ollama model (`qwen2.5:3b`) cleans the transcript — strips filler, fixes punctuation, breaks into paragraphs.
4. The active window decides the cleanup tone (formal for Outlook/Gmail, casual for Slack/Discord/Teams, concise/technical for VS Code/Cursor/JetBrains).
5. Text is pasted into the focused field.

If Ollama is unreachable, you get raw Whisper output — the app degrades gracefully.

## Hotkey

| Action | What it does |
|---|---|
| **Hold** `Ctrl + Win` | Push-to-talk. Record while held, transcribe on release. |
| **Double-tap** `Ctrl + Win` (within 400ms) | Toggle continuous mode. Double-tap again to stop. |
| **Hold** `Ctrl + Alt` | Edit mode. Highlight text first, then speak an instruction to rewrite it in place. |

A short single tap by itself does nothing. Thresholds and modifiers live in `config.yaml`.

## Edit mode

Highlight some text in any app, then **hold `Ctrl + Alt`** and speak an instruction — "make this more formal", "turn this into bullet points", "summarize", "fix the grammar". On release, a local LLM rewrites the selection and pastes the result over your highlight. Your previous clipboard is restored afterward, and if anything fails the original selection is left untouched.

Configure it under `edit_mode:` (enable/disable, modifier keys, hold threshold) or toggle it in **Settings → Edit mode**.

## Voice commands

Spoken control phrases are pulled out of the transcript before polish so they're never rewritten into prose:

- **"press enter" / "send" / "submit"** — pastes your text, then presses Enter (great for chat apps).
- **"new line" / "new paragraph"** — inserts a line break / blank line inline.
- **"press tab" / "press escape"** — presses that key after pasting.
- **"scratch that" / "never mind" / "cancel that"** — discards the whole utterance.

Toggle with `commands.enabled` in `config.yaml` or **Settings → Voice commands**.

## Snippets

Spoken trigger phrases expand to canned text after polish — say "my email" and get your address typed out. Add them under `snippets:` in `config.yaml` (`trigger: expansion`, one per line) or via the **Settings → Snippets** editor. Longer triggers win over shorter ones, and matching is case-insensitive on word boundaries. Toggle the whole feature with `snippets.enabled`.

## On-screen HUD

A small, always-on-top status pill mirrors the current state (recording / transcribing / polishing / editing). It never steals focus and is click-through, so it won't interfere with pasting. Control it with `ui.overlay` (on/off) and `ui.overlay_position` (`bottom`, `top`, or `cursor`), or in **Settings → On-screen HUD**.

## Quick start

1. Install Python from <https://www.python.org/downloads/> (check "Add Python to PATH").
2. Install Ollama from <https://ollama.com/>.
3. Double-click `run.bat`. First run installs Python deps, pulls `qwen2.5:3b` (~2 GB), and downloads the Whisper `small.en` model (~500 MB).

After that, `run.bat` starts instantly.

## Configuration

Everything is in `config.yaml`. Common changes:

- **Output mode** — `type` (default, pastes into focused window), `clipboard` (just copies), `terminal` (prints to console).
- **Whisper model** — `small.en` (default, balanced) → `medium.en` for higher accuracy at the cost of latency.
- **GPU** — leave `device: auto`; it picks CUDA if available.
- **Disable LLM polish** — `llm.enabled: false`, or run `python main.py --no-llm`.
- **Vocabulary** — add canonical spellings under `vocabulary:`. Variants are regex-escaped and replaced case-insensitively after polish.
- **Edit mode** — `edit_mode.enabled`, `edit_mode.modifiers` (default `[ctrl, alt]`), `edit_mode.hold_threshold_ms`.
- **Voice commands** — `commands.enabled` (say "press enter", "new line", "scratch that").
- **Snippets** — `snippets.enabled` plus `trigger: expansion` pairs; spoken triggers expand to canned text after polish.
- **On-screen HUD** — `ui.overlay` (on/off) and `ui.overlay_position` (`bottom` / `top` / `cursor`).

## Context-aware prompts

`prompts/` ships with four cleanup prompts:

| Prompt | Triggered by window title containing… |
|---|---|
| `cleanup_email` | Outlook, Gmail, Thunderbird, Mailbird, Spark, Proton/Fastmail, Apple Mail |
| `cleanup_chat` | Slack, Discord, Microsoft Teams, Telegram, WhatsApp, Signal, Messenger |
| `cleanup_code` | VS Code, Cursor, PyCharm, IntelliJ, WebStorm, GoLand, Rider, Sublime, Neovim, Vim, Zed, Xcode |
| `cleanup_default` | Anything else |

Edit the rules in `context.py` to add apps. Edit the `.md` files in `prompts/` to change voice. Force a single prompt via `context.override` in the config.

## Project layout

```
Whisper2.0/
  config.yaml          all tunables
  main.py              app entry, audio consumer, output routing
  hotkey.py            Ctrl+Win combo controller (hold + double-tap)
  audio.py             mic capture (single-shot PTT + silence-chunked continuous)
  transcribe.py        faster-whisper wrapper, auto CPU/CUDA
  llm.py               Ollama HTTP client + prompt loader (polish + edit)
  context.py           active-window -> prompt rule table
  commands.py          voice-command parser (press enter / new line / scratch that)
  snippets.py          spoken-trigger -> canned-text expansion
  overlay.py           on-screen status HUD (non-activating, click-through)
  prompts/             cleanup_*.md prompt files
  requirements.txt     pinned-loose Python deps
  run.bat / run-terminal.bat / setup.bat
```

## Transferring to another PC

It's still portable. Copy the folder, install Python + Ollama on the target, run `run.bat`. Dependencies and models auto-install on first run.

## Troubleshooting

- **Nothing happens when I press the hotkey** — the `keyboard` library needs to see global key events; on locked-down systems, run `run.bat` as administrator.
- **`[llm] Ollama unreachable`** — make sure `ollama serve` is running (the installer usually adds a tray app that starts it). The app still works, you just lose polish.
- **Slow LLM polish** — try `llama3.2:3b` (`config.yaml` → `llm.model`). On CPU, expect 300–800 ms; on a recent GPU, sub-200 ms.
- **Wrong tone** — edit `prompts/cleanup_*.md`, or `context.override: cleanup_default` to disable context routing.
